from flask import Flask, redirect, request
import subprocess
from datetime import datetime
from pathlib import Path
import os
import json
import time
import uuid
import urllib.request
import urllib.error
import html

app = Flask(__name__)

SERVICES = {
    "reader": {
        "service": "antinea-reader.service",
        "title": "Reader",
        "desc": "Legge dati continui da OctoPrint",
        "log": "octoprint_reader.ndjson"
    },
    "commands": {
        "service": "antinea-commands.service",
        "title": "Commands",
        "desc": "Invia comandi diagnostici M105 / M114 / M119",
        "log": "octoprint_commands.ndjson"
    },
    "serial": {
        "service": "antinea-serial-parser.service",
        "title": "Serial Parser",
        "desc": "Legge le risposte dal serial.log",
        "log": "serial_parser.ndjson"
    }
}

LOG_DIR = Path.home() / "antinea" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

AXIS_LOG = LOG_DIR / "axis_real_moves.ndjson"

OCTOPRINT_URL = os.environ.get("OCTOPRINT_URL", "http://127.0.0.1:5000").rstrip("/")
OCTOPRINT_API_KEY = os.environ.get("OCTOPRINT_API_KEY", "")
SERIAL_LOG = Path(os.environ.get("OCTOPRINT_SERIAL_LOG", str(Path.home() / ".octoprint" / "logs" / "serial.log")))

def systemctl(action, service):
    subprocess.run(["sudo", "systemctl", action, service], check=False)

def service_status(service):
    r = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True,
        text=True
    )
    state = r.stdout.strip()

    if state == "active":
        return "ATTIVO", "online"
    if state == "inactive":
        return "FERMO", "offline"
    if state == "failed":
        return "ERRORE", "error"

    return state.upper() if state else "SCONOSCIUTO", "unknown"

def file_info(filename):
    path = LOG_DIR / filename

    if not path.exists():
        return "nessun log ancora"

    size_kb = round(path.stat().st_size / 1024, 1)
    modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M:%S")

    return f"{size_kb} KB - ultimo update {modified}"

def api_status():
    if not OCTOPRINT_API_KEY:
        return "API MANCANTE", "error"
    return "API CONFIGURATA", "online"

def post_octoprint_commands(commands):
    if not OCTOPRINT_API_KEY:
        raise RuntimeError("API key OctoPrint mancante")

    payload = json.dumps({"commands": commands}).encode("utf-8")
    req = urllib.request.Request(
        f"{OCTOPRINT_URL}/api/printer/command",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": OCTOPRINT_API_KEY
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Errore OctoPrint HTTP {e.code}: {e.reason}")
    except Exception as e:
        raise RuntimeError(f"Errore collegamento OctoPrint: {e}")

def wait_real_time_from_serial(start_marker, end_marker, file_pos, timeout_s=60):
    """
    Misura tempo reale osservando serial.log:
    - START marker ricevuto dal firmware/seriale
    - END marker ricevuto dopo M400, quindi dopo fine movimento
    Il tempo reale è la differenza tra END e START osservati nel log.
    """
    if not SERIAL_LOG.exists():
        return {
            "ok": False,
            "error": f"serial.log non trovato: {SERIAL_LOG}",
            "real_seconds": None,
            "start_seen": False,
            "end_seen": False,
            "serial_log": str(SERIAL_LOG)
        }

    started_at = None
    ended_at = None
    start_line = None
    end_line = None
    deadline = time.monotonic() + timeout_s

    with SERIAL_LOG.open("r", errors="ignore") as f:
        f.seek(file_pos)

        while time.monotonic() < deadline:
            line = f.readline()

            if not line:
                time.sleep(0.05)
                continue

            now = time.monotonic()

            if start_marker in line and started_at is None:
                started_at = now
                start_line = line.strip()

            if end_marker in line and ended_at is None:
                ended_at = now
                end_line = line.strip()
                break

    if started_at is None or ended_at is None:
        return {
            "ok": False,
            "error": "marker START/END non trovati entro il timeout",
            "real_seconds": None,
            "start_seen": started_at is not None,
            "end_seen": ended_at is not None,
            "start_line": start_line,
            "end_line": end_line,
            "serial_log": str(SERIAL_LOG)
        }

    return {
        "ok": True,
        "error": None,
        "real_seconds": round(ended_at - started_at, 4),
        "start_seen": True,
        "end_seen": True,
        "start_line": start_line,
        "end_line": end_line,
        "serial_log": str(SERIAL_LOG)
    }

def append_axis_log(record):
    with AXIS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def read_axis_logs(limit=None, newest_first=False):
    if not AXIS_LOG.exists():
        return []

    lines = AXIS_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    if limit is not None:
        lines = lines[-limit:]

    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass

    if newest_first:
        rows.reverse()

    return rows

def read_last_axis_logs(limit=20):
    return read_axis_logs(limit=limit, newest_first=True)

def safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

def theoretical_seconds(distance_mm, feedrate):
    # Feedrate F = mm/min, quindi tempo_teorico_s = distanza_mm * 60 / feedrate.
    distance = safe_float(distance_mm)
    f = safe_float(feedrate)
    if distance is None or f is None or f <= 0:
        return None
    return abs(distance) * 60.0 / f

def row_theoretical_seconds(row):
    saved = safe_float(row.get("theoretical_seconds"))
    if saved is not None:
        return saved
    return theoretical_seconds(row.get("distance_mm"), row.get("feedrate"))

def row_delta_seconds(row):
    real_s = safe_float(row.get("real_seconds"))
    theory_s = row_theoretical_seconds(row)
    if real_s is None or theory_s is None:
        return None
    return real_s - theory_s

def row_delta_percent(row):
    theory_s = row_theoretical_seconds(row)
    delta_s = row_delta_seconds(row)
    if theory_s is None or theory_s == 0 or delta_s is None:
        return None
    return (delta_s / theory_s) * 100.0

def valid_real_rows(rows):
    valid = []
    for r in rows:
        real_s = safe_float(r.get("real_seconds"))
        theory_s = row_theoretical_seconds(r)
        if r.get("status") == "ok" and real_s is not None and theory_s is not None:
            valid.append(r)
    return valid

def fmt_seconds(value):
    value = safe_float(value)
    if value is None:
        return "—"
    return f"{value:.4f} s"

def fmt_delta(value):
    value = safe_float(value)
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.4f} s"

def fmt_percent(value):
    value = safe_float(value)
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"

def delta_badge(delta_s):
    delta_s = safe_float(delta_s)
    if delta_s is None:
        return '<span class="badge unknown">N/D</span>'
    if delta_s <= 0:
        return '<span class="badge online">OK</span>'
    if delta_s < 0.25:
        return '<span class="badge unknown">Lieve</span>'
    if delta_s < 1.0:
        return '<span class="badge offline">Attenzione</span>'
    return '<span class="badge error">Alto</span>'

def axis_stats(rows):
    valid = valid_real_rows(rows)
    real_values = [safe_float(r.get("real_seconds")) for r in valid]
    theory_values = [row_theoretical_seconds(r) for r in valid]
    delta_values = [row_delta_seconds(r) for r in valid]
    delta_pct_values = [row_delta_percent(r) for r in valid]
    real_values = [v for v in real_values if v is not None]
    theory_values = [v for v in theory_values if v is not None]
    delta_values = [v for v in delta_values if v is not None]
    delta_pct_values = [v for v in delta_pct_values if v is not None]

    stats = {
        "total_count": len(rows),
        "ok_count": len(valid),
        "error_count": max(0, len(rows) - len(valid)),
        "avg_real": None,
        "avg_theory": None,
        "avg_delta": None,
        "avg_delta_pct": None,
        "min_real": None,
        "max_real": None,
        "last_real": None,
        "last_theory": None,
        "last_delta": None,
        "groups": {}
    }

    if real_values:
        stats["avg_real"] = sum(real_values) / len(real_values)
        stats["avg_theory"] = sum(theory_values) / len(theory_values)
        stats["avg_delta"] = sum(delta_values) / len(delta_values)
        stats["avg_delta_pct"] = sum(delta_pct_values) / len(delta_pct_values) if delta_pct_values else None
        stats["min_real"] = min(real_values)
        stats["max_real"] = max(real_values)
        stats["last_real"] = float(valid[-1]["real_seconds"])
        stats["last_theory"] = row_theoretical_seconds(valid[-1])
        stats["last_delta"] = row_delta_seconds(valid[-1])

    for r in valid:
        distance = safe_float(r.get("distance_mm"))
        feedrate = safe_float(r.get("feedrate"))
        axis_key = f"{r.get('axis', '')}{r.get('direction', '')}"
        # Il confronto diagnostico deve raggruppare movimenti omogenei:
        # stesso asse/direzione, stessa distanza, stesso feedrate.
        group_key = (axis_key, distance, feedrate)
        stats["groups"].setdefault(group_key, []).append(r)

    return stats

def build_stats_cards(stats):
    return f'''
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">Tempo medio reale</div>
            <div class="stat-value">{fmt_seconds(stats["avg_real"])}</div>
            <div class="stat-note">media dei tempi misurati</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Tempo teorico medio</div>
            <div class="stat-value">{fmt_seconds(stats["avg_theory"])}</div>
            <div class="stat-note">calcolato da distanza e feedrate</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Delta t medio</div>
            <div class="stat-value">{fmt_delta(stats["avg_delta"])}</div>
            <div class="stat-note">reale medio - teorico medio</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Scostamento medio</div>
            <div class="stat-value">{fmt_percent(stats["avg_delta_pct"])}</div>
            <div class="stat-note">delta t rispetto al teorico</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Ultimo delta</div>
            <div class="stat-value mini-value">{fmt_delta(stats["last_delta"])}</div>
            <div class="stat-note">ultimo movimento valido</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Min / Max reale</div>
            <div class="stat-value mini-value">{fmt_seconds(stats["min_real"])} / {fmt_seconds(stats["max_real"])}</div>
            <div class="stat-note">range reale osservato</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Test validi</div>
            <div class="stat-value">{stats["ok_count"]}/{stats["total_count"]}</div>
            <div class="stat-note">errori marker: {stats["error_count"]}</div>
        </div>
    </div>
    '''

def build_axis_group_table(stats):
    groups = stats.get("groups", {})
    if not groups:
        return '<tr><td colspan="8">Nessun dato medio per asse ancora.</td></tr>'

    rows = ""
    for group_key in sorted(groups.keys(), key=lambda x: (x[0], x[1] or 0, x[2] or 0)):
        axis_key, distance, feedrate = group_key
        items = groups[group_key]
        real_vals = [safe_float(r.get("real_seconds")) for r in items]
        theory_vals = [row_theoretical_seconds(r) for r in items]
        delta_vals = [row_delta_seconds(r) for r in items]
        pct_vals = [row_delta_percent(r) for r in items]
        avg_real = sum(real_vals) / len(real_vals)
        avg_theory = sum(theory_vals) / len(theory_vals)
        avg_delta = sum(delta_vals) / len(delta_vals)
        avg_pct = sum(pct_vals) / len(pct_vals)
        rows += f'''
        <tr>
            <td><b>{html.escape(axis_key)}</b></td>
            <td>{distance:g}</td>
            <td>{feedrate:g}</td>
            <td>{len(items)}</td>
            <td>{fmt_seconds(avg_real)}</td>
            <td>{fmt_seconds(avg_theory)}</td>
            <td><b>{fmt_delta(avg_delta)}</b><br><span class="muted">{fmt_percent(avg_pct)}</span></td>
            <td>{delta_badge(avg_delta)}</td>
        </tr>
        '''
    return rows


AXES = ("X", "Y", "Z")
TREND_WINDOW = 5

def average(values):
    values = [safe_float(v) for v in values]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)

def trend_badge(status):
    if status == "migliora":
        return '<span class="badge online">Migliora</span>'
    if status == "stabile":
        return '<span class="badge unknown">Stabile</span>'
    if status == "osservare":
        return '<span class="badge offline">Da osservare</span>'
    if status == "peggiora":
        return '<span class="badge error">Peggiora</span>'
    return '<span class="badge unknown">N/D</span>'

def axis_trend_stats(rows, window=TREND_WINDOW):
    # Chicca predittiva: confronta il delta recente con il delta precedente, asse per asse.
    # Non cerca il guasto da un singolo movimento: cerca una deriva nel tempo.
    valid = valid_real_rows(rows)
    result = {}

    for axis in AXES:
        axis_rows = [r for r in valid if str(r.get("axis", "")).upper() == axis]
        deltas = [row_delta_seconds(r) for r in axis_rows]
        deltas = [v for v in deltas if v is not None]
        n = len(deltas)

        item = {
            "n": n,
            "previous_avg": None,
            "recent_avg": None,
            "trend_delta": None,
            "trend_percent": None,
            "status": "nd",
            "score": 0.0,
        }

        if n < 4:
            result[axis] = item
            continue

        w = min(window, max(2, n // 2))
        if n >= w * 2:
            previous = deltas[-2*w:-w]
            recent = deltas[-w:]
        else:
            split = n // 2
            previous = deltas[:split]
            recent = deltas[split:]

        previous_avg = average(previous)
        recent_avg = average(recent)
        if previous_avg is None or recent_avg is None:
            result[axis] = item
            continue

        trend_delta = recent_avg - previous_avg
        denominator = abs(previous_avg) if abs(previous_avg) > 0.0001 else max(abs(recent_avg), 0.0001)
        trend_percent = (trend_delta / denominator) * 100.0

        if trend_delta < -0.05:
            status = "migliora"
        elif trend_delta <= 0.05:
            status = "stabile"
        elif trend_delta < 0.25:
            status = "osservare"
        else:
            status = "peggiora"

        # Score semplice: pesa sia il delta recente assoluto sia la deriva recente.
        score = max(0.0, recent_avg) + max(0.0, trend_delta) * 2.0

        item.update({
            "previous_avg": previous_avg,
            "recent_avg": recent_avg,
            "trend_delta": trend_delta,
            "trend_percent": trend_percent,
            "status": status,
            "score": score,
        })
        result[axis] = item

    return result

def build_predictive_hint(rows):
    trends = axis_trend_stats(rows)
    candidates = [(axis, data) for axis, data in trends.items() if data.get("recent_avg") is not None]

    if not candidates:
        return '''
        <div class="note">
            Chicca predittiva pronta, ma servono almeno 4 test validi per asse per confrontare delta precedente e delta recente.
        </div>
        '''

    suspicious_axis, suspicious = max(candidates, key=lambda item: item[1].get("score", 0.0))
    if suspicious.get("score", 0.0) <= 0.05:
        headline = "Nessun asse chiaramente sospetto"
        advice = "I delta sono stabili o in miglioramento. Continua a usare sempre la stessa distanza e lo stesso feedrate per creare uno storico pulito."
        headline_badge = '<span class="badge online">OK</span>'
    else:
        headline = f"Asse più sospetto: {html.escape(suspicious_axis)}"
        advice = "Controlla questo asse per primo: polvere sulle guide, cinghia, puleggia, attrito meccanico, cavo o trascinamento. Il dato forte è la crescita del delta nel tempo."
        headline_badge = trend_badge(suspicious.get("status"))

    rows_html = ""
    for axis in AXES:
        data = trends[axis]
        rows_html += f'''
        <tr>
            <td><b>{axis}</b></td>
            <td>{data["n"]}</td>
            <td>{fmt_delta(data["previous_avg"])}</td>
            <td>{fmt_delta(data["recent_avg"])}</td>
            <td><b>{fmt_delta(data["trend_delta"])}</b><br><span class="muted">{fmt_percent(data["trend_percent"])}</span></td>
            <td>{trend_badge(data["status"])}</td>
        </tr>
        '''

    return f'''
    <div class="predictive-box">
        <div class="predictive-head">
            <div>
                <div class="predictive-title">{headline}</div>
                <p>{advice}</p>
            </div>
            {headline_badge}
        </div>
        <table>
            <tr>
                <th>Asse</th>
                <th>N</th>
                <th>Delta precedente</th>
                <th>Delta recente</th>
                <th>Deriva</th>
                <th>Lettura</th>
            </tr>
            {rows_html}
        </table>
        <div class="note">
            Lettura: confronta gli ultimi test con il blocco precedente, separatamente per X/Y/Z. È un indicatore di deriva, non una sentenza di guasto.
        </div>
    </div>
    '''

def axis_only_stats(rows):
    # Riepilogo separato per asse fisico X/Y/Z.
    valid = valid_real_rows(rows)
    result = {}
    for axis in AXES:
        axis_rows = [r for r in valid if str(r.get("axis", "")).upper() == axis]
        real_vals = [safe_float(r.get("real_seconds")) for r in axis_rows]
        theory_vals = [row_theoretical_seconds(r) for r in axis_rows]
        delta_vals = [row_delta_seconds(r) for r in axis_rows]
        pct_vals = [row_delta_percent(r) for r in axis_rows]
        real_vals = [v for v in real_vals if v is not None]
        theory_vals = [v for v in theory_vals if v is not None]
        delta_vals = [v for v in delta_vals if v is not None]
        pct_vals = [v for v in pct_vals if v is not None]

        axis_total = len([r for r in rows if str(r.get("axis", "")).upper() == axis])
        result[axis] = {
            "total_count": axis_total,
            "ok_count": len(axis_rows),
            "avg_real": sum(real_vals) / len(real_vals) if real_vals else None,
            "avg_theory": sum(theory_vals) / len(theory_vals) if theory_vals else None,
            "avg_delta": sum(delta_vals) / len(delta_vals) if delta_vals else None,
            "avg_delta_pct": sum(pct_vals) / len(pct_vals) if pct_vals else None,
            "min_real": min(real_vals) if real_vals else None,
            "max_real": max(real_vals) if real_vals else None,
            "last_delta": row_delta_seconds(axis_rows[-1]) if axis_rows else None,
        }
    return result

def build_axis_overview_cards(rows):
    per_axis = axis_only_stats(rows)
    cards = ""
    for axis in AXES:
        s = per_axis[axis]
        avg_delta = s["avg_delta"]
        cards += f'''
        <div class="axis-card">
            <div class="axis-card-head">
                <div class="axis-letter">Asse {axis}</div>
                {delta_badge(avg_delta)}
            </div>
            <div class="axis-metric"><span>Media reale</span><b>{fmt_seconds(s["avg_real"])}</b></div>
            <div class="axis-metric"><span>Teorico medio</span><b>{fmt_seconds(s["avg_theory"])}</b></div>
            <div class="axis-metric delta-row"><span>Delta t</span><b>{fmt_delta(avg_delta)}</b></div>
            <div class="axis-metric"><span>Scostamento</span><b>{fmt_percent(s["avg_delta_pct"])}</b></div>
            <div class="axis-foot">Test validi: {s["ok_count"]}/{s["total_count"]}</div>
        </div>
        '''
    return f'<div class="axis-overview">{cards}</div>'

def build_axis_detail_sections(stats):
    groups = stats.get("groups", {})
    sections = ""
    for axis in AXES:
        axis_groups = []
        for group_key, items in groups.items():
            axis_key, distance, feedrate = group_key
            if str(axis_key).startswith(axis):
                axis_groups.append((group_key, items))

        axis_groups.sort(key=lambda x: (x[0][0], x[0][1] or 0, x[0][2] or 0))

        table_rows = ""
        for group_key, items in axis_groups:
            axis_key, distance, feedrate = group_key
            direction = axis_key.replace(axis, "", 1) or "—"
            real_vals = [safe_float(r.get("real_seconds")) for r in items]
            theory_vals = [row_theoretical_seconds(r) for r in items]
            delta_vals = [row_delta_seconds(r) for r in items]
            pct_vals = [row_delta_percent(r) for r in items]
            real_vals = [v for v in real_vals if v is not None]
            theory_vals = [v for v in theory_vals if v is not None]
            delta_vals = [v for v in delta_vals if v is not None]
            pct_vals = [v for v in pct_vals if v is not None]
            avg_real = sum(real_vals) / len(real_vals) if real_vals else None
            avg_theory = sum(theory_vals) / len(theory_vals) if theory_vals else None
            avg_delta = sum(delta_vals) / len(delta_vals) if delta_vals else None
            avg_pct = sum(pct_vals) / len(pct_vals) if pct_vals else None
            table_rows += f'''
            <tr>
                <td><b>{html.escape(direction)}</b></td>
                <td>{distance:g}</td>
                <td>{feedrate:g}</td>
                <td>{len(items)}</td>
                <td>{fmt_seconds(avg_real)}</td>
                <td>{fmt_seconds(avg_theory)}</td>
                <td><b>{fmt_delta(avg_delta)}</b><br><span class="muted">{fmt_percent(avg_pct)}</span></td>
                <td>{delta_badge(avg_delta)}</td>
            </tr>
            '''

        if not table_rows:
            table_rows = '<tr><td colspan="8">Nessun dato valido per questo asse.</td></tr>'

        sections += f'''
        <div class="axis-section">
            <div class="axis-section-title">Asse {axis}</div>
            <table>
                <tr>
                    <th>Direzione</th>
                    <th>mm</th>
                    <th>F mm/min</th>
                    <th>N</th>
                    <th>Media reale</th>
                    <th>Tempo teorico</th>
                    <th>Delta t</th>
                    <th>Stato</th>
                </tr>
                {table_rows}
            </table>
        </div>
        '''
    return sections

def build_axis_time_chart(rows, limit=60, axis_filter=None):
    valid = valid_real_rows(rows)
    if axis_filter:
        valid = [r for r in valid if str(r.get("axis", "")).upper() == str(axis_filter).upper()]
    valid = valid[-limit:]
    if not valid:
        return f'<div class="note">Nessun dato valido da disegnare per {html.escape(str(axis_filter)) if axis_filter else "gli assi"}. Esegui almeno un test.</div>'

    real_values = [float(r["real_seconds"]) for r in valid]
    theory_values = [row_theoretical_seconds(r) for r in valid]
    delta_values = [row_delta_seconds(r) for r in valid]
    labels = [f"{r.get('axis','')}{r.get('direction','')} {safe_float(r.get('distance_mm')):g}mm F{safe_float(r.get('feedrate')):g}" for r in valid]
    avg_delta = sum(delta_values) / len(delta_values)

    width = 920
    height = 330
    left = 58
    right = 24
    top = 28
    bottom = 70
    plot_w = width - left - right
    plot_h = height - top - bottom

    all_values = real_values + theory_values
    y_min = min(all_values)
    y_max = max(all_values)
    if y_min == y_max:
        pad = max(0.05, y_min * 0.1)
        y_min -= pad
        y_max += pad
    else:
        pad = (y_max - y_min) * 0.15
        y_min = max(0, y_min - pad)
        y_max += pad

    def x_at(i):
        if len(real_values) == 1:
            return left + plot_w / 2
        return left + (plot_w * i / (len(real_values) - 1))

    def y_at(v):
        return top + plot_h - ((v - y_min) / (y_max - y_min) * plot_h)

    real_points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(real_values))
    theory_points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(theory_values))

    circles = ""
    for i, v in enumerate(real_values):
        circles += f'''<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3"><title>{html.escape(labels[i])}: reale {v:.4f}s, teorico {theory_values[i]:.4f}s, delta {delta_values[i]:+.4f}s</title></circle>'''

    grid = ""
    y_labels = ""
    for step in range(5):
        val = y_min + (y_max - y_min) * step / 4
        y = y_at(val)
        grid += f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" />'
        y_labels += f'<text class="axis-label" x="8" y="{y+4:.1f}">{val:.2f}s</text>'

    first_label = html.escape(labels[0])
    last_label = html.escape(labels[-1])

    return f'''
    <div class="chart-wrap">
        <svg viewBox="0 0 {width} {height}" role="img" aria-label="Grafico confronto tempo reale e tempo teorico">
            {grid}
            {y_labels}
            <line class="axis-line" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" />
            <polyline class="theory-line" points="{theory_points}" />
            <polyline class="time-line" points="{real_points}" />
            <g class="points">{circles}</g>
            <text class="legend real-legend" x="{left}" y="{height-42}">● reale misurato</text>
            <text class="legend theory-legend" x="{left+150}" y="{height-42}">– teorico da F</text>
            <text class="legend delta-legend" x="{left+315}" y="{height-42}">delta medio {avg_delta:+.4f}s</text>
            <text class="x-label" x="{left}" y="{height-16}">{first_label}</text>
            <text class="x-label" text-anchor="end" x="{width-right}" y="{height-16}">{last_label}</text>
        </svg>
    </div>
    <div class="note">{"Asse " + html.escape(str(axis_filter).upper()) + ": " if axis_filter else ""}Grafico degli ultimi {len(valid)} test validi: linea piena = tempo reale, linea tratteggiata = tempo teorico calcolato da distanza e feedrate. Delta t = reale - teorico.</div>
    '''



# =========================
# ANTINEA MINI AI ENGINE
# =========================
def clean_numeric(values):
    out = []
    for v in values:
        v = safe_float(v)
        if v is not None:
            out.append(v)
    return out

def median(values):
    values = sorted(clean_numeric(values))
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0

def percentile(values, pct):
    values = sorted(clean_numeric(values))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * pct / 100.0
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[int(k)]
    return values[f] + (values[c] - values[f]) * (k - f)

def linear_regression_slope(values):
    values = clean_numeric(values)
    n = len(values)
    if n < 5:
        return None
    x = list(range(n))
    y = values
    x_mean = sum(x) / n
    y_mean = sum(y) / n
    num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
    den = sum((x[i] - x_mean) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den

def linear_regression_forecast(values, steps_ahead=5):
    values = clean_numeric(values)
    n = len(values)
    if n < 5:
        return None
    slope = linear_regression_slope(values)
    if slope is None:
        return None
    x_mean = sum(range(n)) / n
    y_mean = sum(values) / n
    intercept = y_mean - slope * x_mean
    future_x = n - 1 + steps_ahead
    return intercept + slope * future_x

def compute_baseline_stats(values):
    values = clean_numeric(values)
    n = len(values)
    if n < 5:
        return None
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    std = variance ** 0.5
    med = median(values)
    mad = median([abs(v - med) for v in values]) or 0.0
    q1 = percentile(values, 25)
    q3 = percentile(values, 75)
    iqr = (q3 - q1) if q1 is not None and q3 is not None else 0.0
    return {'n': n, 'mean': mean, 'std': std, 'median': med, 'mad': mad, 'q1': q1, 'q3': q3, 'iqr': iqr}

def z_score(value, baseline):
    value = safe_float(value)
    if value is None or not baseline:
        return None
    std = baseline.get('std') or 0.0
    if std < 0.0001:
        std = 0.0001
    return (value - baseline.get('mean', 0.0)) / std

def robust_outlier_score(value, baseline):
    # Isolation-lite: detector robusto su mediana, MAD e IQR.
    value = safe_float(value)
    if value is None or not baseline:
        return None
    med = baseline.get('median')
    mad = baseline.get('mad') or 0.0
    iqr = baseline.get('iqr') or 0.0
    scale = max(mad * 1.4826, iqr / 1.349 if iqr else 0.0, 0.0001)
    return abs(value - med) / scale

def anomaly_label(z_abs, robust_score, slope):
    z_abs = safe_float(z_abs) or 0.0
    robust_score = safe_float(robust_score) or 0.0
    slope = safe_float(slope) or 0.0
    votes = 0
    if z_abs >= 3.0: votes += 2
    elif z_abs >= 2.0: votes += 1
    if robust_score >= 3.5: votes += 2
    elif robust_score >= 2.5: votes += 1
    if slope > 0.04: votes += 1
    if slope > 0.10: votes += 1
    if votes >= 4: return 'ANOMALIA GRAVE'
    if votes >= 2: return 'ANOMALIA LIEVE'
    if votes == 1: return 'DA OSSERVARE'
    return 'NORMALE'

def health_index_from_models(last_delta, baseline, slope, robust_score):
    if last_delta is None or not baseline:
        return None
    z = z_score(last_delta, baseline)
    z_abs = abs(z) if z is not None else 0.0
    robust_score = safe_float(robust_score) or 0.0
    slope = safe_float(slope) or 0.0
    penalty = min(45.0, z_abs * 12.0) + min(25.0, robust_score * 6.0)
    if slope > 0:
        penalty += min(30.0, slope * 250.0)
    return round(max(0.0, min(100.0, 100.0 - penalty)), 1)

def mini_ai_engine(rows):
    valid = valid_real_rows(rows)
    engine = {'model': 'Antinea Mini AI Engine', 'models_active': ['baseline', 'z-score', 'linear-regression', 'health-index', 'isolation-lite'], 'total_tests': len(rows), 'valid_tests': len(valid), 'axes': {}, 'most_suspicious_axis': None, 'global_status': 'N/D'}
    best_axis = None
    best_risk = -1.0
    for axis in AXES:
        axis_rows = [r for r in valid if str(r.get('axis', '')).upper() == axis]
        deltas = clean_numeric([row_delta_seconds(r) for r in axis_rows])
        baseline = compute_baseline_stats(deltas)
        last_delta = deltas[-1] if deltas else None
        avg_delta = sum(deltas) / len(deltas) if deltas else None
        slope = linear_regression_slope(deltas)
        forecast_delta = linear_regression_forecast(deltas, steps_ahead=5)
        z = z_score(last_delta, baseline) if baseline else None
        robust = robust_outlier_score(last_delta, baseline) if baseline else None
        hi = health_index_from_models(last_delta, baseline, slope, robust) if baseline else None
        label = anomaly_label(abs(z) if z is not None else None, robust, slope) if baseline else 'DATI INSUFFICIENTI'
        trend = 'nd' if slope is None else ('peggiora' if slope > 0.02 else ('migliora' if slope < -0.02 else 'stabile'))
        risk = 0.0
        if hi is not None: risk += 100.0 - hi
        if slope and slope > 0: risk += min(40.0, slope * 300.0)
        if avg_delta and avg_delta > 0: risk += min(20.0, avg_delta * 10.0)
        if label == 'ANOMALIA GRAVE': risk += 40.0
        elif label == 'ANOMALIA LIEVE': risk += 20.0
        elif label == 'DA OSSERVARE': risk += 8.0
        if risk > best_risk and deltas:
            best_risk = risk; best_axis = axis
        engine['axes'][axis] = {'n': len(deltas), 'baseline': baseline, 'avg_delta': avg_delta, 'last_delta': last_delta, 'slope': slope, 'forecast_delta_5': forecast_delta, 'z_score': z, 'robust_score': robust, 'health_index': hi, 'anomaly': label, 'trend': trend, 'risk_score': round(risk, 2)}
    engine['most_suspicious_axis'] = best_axis
    labels = [d.get('anomaly') for d in engine['axes'].values()]
    if 'ANOMALIA GRAVE' in labels: engine['global_status'] = 'ANOMALIA GRAVE'
    elif 'ANOMALIA LIEVE' in labels: engine['global_status'] = 'ANOMALIA LIEVE'
    elif 'DA OSSERVARE' in labels: engine['global_status'] = 'DA OSSERVARE'
    elif any(d.get('n', 0) >= 5 for d in engine['axes'].values()): engine['global_status'] = 'NORMALE'
    else: engine['global_status'] = 'DATI INSUFFICIENTI'
    return engine

def ai_status_badge(label):
    if label == 'NORMALE': return '<span class="badge online">NORMALE</span>'
    if label == 'DA OSSERVARE': return '<span class="badge offline">DA OSSERVARE</span>'
    if label == 'ANOMALIA LIEVE': return '<span class="badge offline">ANOMALIA LIEVE</span>'
    if label == 'ANOMALIA GRAVE': return '<span class="badge error">ANOMALIA GRAVE</span>'
    return '<span class="badge unknown">DATI INSUFFICIENTI</span>'

def build_ai_engine_table(rows):
    engine = mini_ai_engine(rows)
    html_rows = ''
    for axis in AXES:
        d = engine['axes'][axis]
        z_txt = '—' if d.get('z_score') is None else round(d.get('z_score'), 2)
        hi_txt = '—' if d.get('health_index') is None else d.get('health_index')
        html_rows += f"""
        <tr>
            <td><b>{axis}</b></td><td>{d['n']}</td><td>{fmt_delta(d.get('avg_delta'))}</td><td>{fmt_delta(d.get('last_delta'))}</td>
            <td>{fmt_delta(d.get('slope'))}</td><td>{fmt_delta(d.get('forecast_delta_5'))}</td><td>{z_txt}</td><td><b>{hi_txt}</b></td><td>{ai_status_badge(d.get('anomaly'))}</td>
        </tr>
        """
    return f"""
    <div class="predictive-box">
        <div class="predictive-head"><div><div class="predictive-title">{html.escape(engine['model'])}</div><p>Modelli attivi: {', '.join(engine['models_active'])}. Asse più sospetto: <b>{engine.get('most_suspicious_axis') or 'N/D'}</b>.</p></div>{ai_status_badge(engine.get('global_status'))}</div>
        <table><tr><th>Asse</th><th>N</th><th>Delta medio</th><th>Ultimo delta</th><th>Slope LR</th><th>Forecast +5</th><th>Z</th><th>Health</th><th>AI status</th></tr>{html_rows}</table>
        <div class="note">Linear Regression: slope positivo = delta in crescita, quindi possibile degrado. Z-score e isolation-lite cercano anomalie rispetto alla baseline storica dell'asse.</div>
    </div>
    """

def build_axis_split_charts(rows, limit=60):
    blocks = ""
    for axis in AXES:
        blocks += f'''
        <div class="axis-chart-block">
            <h3>Grafico asse {axis}</h3>
            {build_axis_time_chart(rows, limit=limit, axis_filter=axis)}
        </div>
        '''
    return blocks



def build_ai_context(rows):
    engine = mini_ai_engine(rows)
    stats = axis_stats(rows)
    context = {'total_tests': engine.get('total_tests', 0), 'valid_tests': engine.get('valid_tests', 0), 'avg_delta': stats.get('avg_delta'), 'last_delta': stats.get('last_delta'), 'most_suspicious_axis': engine.get('most_suspicious_axis'), 'global_status': engine.get('global_status'), 'models_active': engine.get('models_active'), 'axes': {}}
    for axis in AXES:
        d = engine['axes'][axis]
        context['axes'][axis] = {'avg_delta': d.get('avg_delta'), 'last_delta': d.get('last_delta'), 'valid_tests': d.get('n'), 'trend_status': d.get('trend'), 'trend_delta': d.get('slope'), 'slope': d.get('slope'), 'forecast_delta_5': d.get('forecast_delta_5'), 'z_score': d.get('z_score'), 'health_index': d.get('health_index'), 'anomaly': d.get('anomaly'), 'risk_score': d.get('risk_score')}
    return context

def diagnostic_advice_for_axis(axis, data):
    n = data.get('valid_tests', 0)
    if not n:
        return f"Asse {axis}: non ho ancora dati validi. Esegui test omogenei con stessa distanza e feedrate."
    avg_delta = safe_float(data.get('avg_delta'))
    last_delta = safe_float(data.get('last_delta'))
    slope = safe_float(data.get('slope'))
    forecast = safe_float(data.get('forecast_delta_5'))
    health = data.get('health_index')
    anomaly = data.get('anomaly')
    z = safe_float(data.get('z_score'))
    text = f"Asse {axis}: {n} test validi. Delta medio {fmt_delta(avg_delta)}, ultimo delta {fmt_delta(last_delta)}. "
    text += f"Health Index: {'N/D' if health is None else health}/100. Stato AI: {anomaly}. "
    if slope is not None:
        text += f"Linear regression slope {fmt_delta(slope)}; forecast a 5 test {fmt_delta(forecast)}. "
    if z is not None:
        text += f"Z-score ultimo movimento: {z:.2f}. "
    if anomaly in ('ANOMALIA GRAVE', 'ANOMALIA LIEVE'):
        text += "Priorità manutenzione: controlla attrito guide, cinghia, puleggia, ruote eccentriche, cablaggio in trascinamento e ripeti lo stesso test."
    elif anomaly == 'DA OSSERVARE':
        text += "Non è ancora guasto certo: continua con test omogenei e guarda se lo slope resta positivo."
    elif anomaly == 'NORMALE':
        text += "Comportamento nella baseline: continua a costruire storico."
    else:
        text += "Servono almeno 5-10 test omogenei per asse per una lettura robusta."
    return text

def simple_antinea_chatbot(question, context):
    q = (question or '').lower().strip()
    if not q:
        return "Scrivi una domanda, per esempio: 'Quale asse è più sospetto?'"
    if context.get('valid_tests', 0) == 0:
        return "Non ho ancora test validi nei log. Prima esegui alcuni movimenti da /axis."
    axes = context.get('axes', {})
    if any(w in q for w in ['modelli', 'engine', 'motore', 'ai', 'algoritmi']):
        return "Motore attivo: baseline statistica, z-score, linear regression, health index e isolation-lite robusta. La AI numerica decide; il chatbot spiega."
    for axis in AXES:
        if f"asse {axis.lower()}" in q or q == axis.lower() or f" {axis.lower()} " in f" {q} ":
            return diagnostic_advice_for_axis(axis, axes.get(axis, {}))
    if any(w in q for w in ['sospetto', 'peggiore', 'peggiora', 'anomalia', 'problema', 'critico']):
        axis = context.get('most_suspicious_axis')
        return "Stato globale AI: " + str(context.get('global_status')) + ". Asse più sospetto: " + str(axis or 'N/D') + ". " + (diagnostic_advice_for_axis(axis, axes.get(axis, {})) if axis else '')
    if 'health' in q or 'salute' in q:
        return '; '.join([f"{axis}: Health {axes.get(axis, {}).get('health_index', 'N/D')}/100, stato {axes.get(axis, {}).get('anomaly', 'N/D')}" for axis in AXES])
    if 'regression' in q or 'regressione' in q or 'slope' in q or 'forecast' in q:
        return 'Linear regression sui delta_t: ' + '; '.join([f"{axis}: slope {fmt_delta(axes.get(axis, {}).get('slope'))}, forecast +5 {fmt_delta(axes.get(axis, {}).get('forecast_delta_5'))}" for axis in AXES])
    if 'z' in q or 'z-score' in q or 'score' in q:
        parts = []
        for axis in AXES:
            z = axes.get(axis, {}).get('z_score')
            parts.append(f"{axis}: z {'N/D' if z is None else round(z,2)}")
        return 'Z-score ultimo movimento rispetto alla baseline: ' + '; '.join(parts)
    if 'delta' in q or 'tempo' in q:
        return f"Delta t globale medio: {fmt_delta(context.get('avg_delta'))}. Ultimo delta globale: {fmt_delta(context.get('last_delta'))}. Delta positivo = reale più lento del teorico."
    if any(w in q for w in ['manutenzione', 'controllo', 'pulire', 'cinghia', 'polvere', 'attrito']):
        axis = context.get('most_suspicious_axis') or 'X/Y/Z'
        return f"Partirei dall'asse {axis}: pulizia guide, controllo cinghia/pulegge, ruote/carrelli, cavo trascinato, poi ripeti stesso asse/distanza/feedrate e guarda Health Index e slope."
    lines = [f"Ho letto {context.get('valid_tests')} test validi su {context.get('total_tests')} totali. Stato globale AI: {context.get('global_status')}. Asse più sospetto: {context.get('most_suspicious_axis') or 'N/D'}."]
    for axis in AXES:
        d = axes.get(axis, {})
        lines.append(f"Asse {axis}: delta medio {fmt_delta(d.get('avg_delta'))}, health {d.get('health_index', 'N/D')}, anomaly {d.get('anomaly', 'N/D')}, slope {fmt_delta(d.get('slope'))}.")
    return ' '.join(lines)

CHAT_LOG = LOG_DIR / 'antinea_chat.ndjson'

def append_chat_log(question, answer):
    with CHAT_LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'timestamp': datetime.now().isoformat(timespec='seconds'), 'question': question, 'answer': answer}, ensure_ascii=False) + '\n')


def read_chat_history(limit=20):
    if not CHAT_LOG.exists():
        return []
    rows = []
    for line in CHAT_LOG.read_text(encoding='utf-8', errors='ignore').splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def build_chat_history_html(history):
    if not history:
        return '<div class="note">Nessuna domanda ancora. Prova: <b>Quale asse è più sospetto?</b></div>'
    out = ''
    for item in history[-10:][::-1]:
        out += '<div class="chat-message"><div class="chat-question">Tu: ' + html.escape(item.get('question','')) + '</div><div class="chat-answer">Antinea: ' + html.escape(item.get('answer','')) + '</div></div>'
    return out

BASE_STYLE = """
<style>
    body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #0f172a;
        color: #e5e7eb;
    }

    header {
        padding: 24px;
        background: linear-gradient(135deg, #111827, #1e293b);
        border-bottom: 1px solid #334155;
    }

    h1 {
        margin: 0;
        font-size: 32px;
    }

    .subtitle {
        margin-top: 8px;
        color: #94a3b8;
    }

    .container {
        padding: 24px;
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 20px;
    }

    .page {
        padding: 24px;
        max-width: 1100px;
    }

    .card {
        background: #111827;
        border: 1px solid #334155;
        border-radius: 18px;
        padding: 20px;
        box-shadow: 0 10px 25px rgba(0,0,0,0.25);
    }

    .card-top {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: flex-start;
    }

    h2 {
        margin: 0;
        font-size: 22px;
    }

    p {
        color: #94a3b8;
        margin: 8px 0 0;
        line-height: 1.4;
    }

    .badge {
        padding: 6px 10px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: bold;
        white-space: nowrap;
    }

    .online {
        background: #064e3b;
        color: #6ee7b7;
    }

    .offline {
        background: #450a0a;
        color: #fca5a5;
    }

    .error {
        background: #7f1d1d;
        color: #fecaca;
    }

    .unknown {
        background: #374151;
        color: #d1d5db;
    }

    .service-name, .logline, .note {
        margin-top: 12px;
        padding: 10px;
        background: #020617;
        border-radius: 10px;
        color: #cbd5e1;
        font-size: 14px;
    }

    .service-name {
        color: #93c5fd;
        font-size: 13px;
        word-break: break-word;
    }

    .warning {
        margin-top: 18px;
        padding: 12px;
        background: #7c2d12;
        border-radius: 10px;
        color: #fff7ed;
    }

    .buttons {
        display: flex;
        gap: 10px;
        margin-top: 18px;
        flex-wrap: wrap;
    }

    .btn, button {
        border: 0;
        cursor: pointer;
        text-decoration: none;
        color: white;
        padding: 10px 14px;
        border-radius: 10px;
        font-weight: bold;
        font-size: 14px;
    }

    .start { background: #15803d; }
    .stop { background: #b91c1c; }
    .restart { background: #2563eb; }
    .secondary { background: #475569; }

    .danger { background: #991b1b; }

    .stats-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
        margin-top: 16px;
    }

    .axis-overview {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
        gap: 14px;
        margin-top: 16px;
    }

    .axis-card {
        background: #020617;
        border: 1px solid #334155;
        border-radius: 16px;
        padding: 16px;
    }

    .axis-card-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 12px;
    }

    .axis-letter {
        font-size: 22px;
        font-weight: bold;
        color: #e5e7eb;
    }

    .axis-metric {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 7px 0;
        border-top: 1px solid #1f2937;
        color: #94a3b8;
        font-size: 14px;
    }

    .axis-metric b {
        color: #e5e7eb;
        font-size: 15px;
    }

    .delta-row b {
        color: #fde68a;
        font-size: 17px;
    }

    .axis-foot {
        margin-top: 10px;
        color: #64748b;
        font-size: 12px;
    }

    .axis-section {
        margin-top: 18px;
        padding-top: 14px;
        border-top: 1px solid #334155;
    }

    .axis-section-title {
        font-size: 22px;
        font-weight: bold;
        color: #93c5fd;
    }

    .axis-chart-block {
        margin-top: 22px;
        padding-top: 12px;
        border-top: 1px solid #334155;
    }

    .axis-chart-block h3 {
        margin: 0;
        color: #93c5fd;
    }

    .predictive-box {
        margin-top: 16px;
        background: #020617;
        border: 1px solid #334155;
        border-radius: 16px;
        padding: 16px;
    }

    .predictive-head {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 14px;
        margin-bottom: 12px;
    }

    .predictive-title {
        font-size: 21px;
        font-weight: bold;
        color: #fde68a;
    }

    .stat-card {
        background: #020617;
        border: 1px solid #334155;
        border-radius: 14px;
        padding: 14px;
    }

    .stat-label {
        color: #94a3b8;
        font-size: 13px;
        font-weight: bold;
    }

    .stat-value {
        margin-top: 8px;
        font-size: 26px;
        font-weight: bold;
        color: #e5e7eb;
    }

    .mini-value {
        font-size: 18px;
    }

    .stat-note {
        margin-top: 6px;
        color: #64748b;
        font-size: 12px;
    }

    .chart-wrap {
        margin-top: 16px;
        width: 100%;
        overflow-x: auto;
        background: #020617;
        border: 1px solid #334155;
        border-radius: 14px;
        padding: 10px;
        box-sizing: border-box;
    }

    svg {
        width: 100%;
        min-width: 620px;
        height: auto;
    }

    .grid {
        stroke: #1f2937;
        stroke-width: 1;
    }

    .axis-line {
        stroke: #475569;
        stroke-width: 1.4;
    }

    .time-line {
        fill: none;
        stroke: #38bdf8;
        stroke-width: 3;
        stroke-linecap: round;
        stroke-linejoin: round;
    }

    .theory-line {
        fill: none;
        stroke: #fbbf24;
        stroke-width: 2.4;
        stroke-dasharray: 8 6;
        stroke-linecap: round;
        stroke-linejoin: round;
    }

    .points circle {
        fill: #e5e7eb;
        stroke: #38bdf8;
        stroke-width: 2;
    }

    .axis-label, .x-label, .legend {
        fill: #94a3b8;
        font-size: 12px;
    }

    .real-legend { fill: #7dd3fc; }
    .theory-legend { fill: #fde68a; }
    .delta-legend { fill: #cbd5e1; font-weight: bold; }

    .muted {
        color: #94a3b8;
        font-size: 12px;
    }

    label {
        display: block;
        margin-top: 14px;
        font-weight: bold;
    }

    input, select {
        width: 100%;
        box-sizing: border-box;
        margin-top: 6px;
        padding: 10px;
        border-radius: 10px;
        border: 1px solid #334155;
        background: #020617;
        color: #e5e7eb;
        font-size: 15px;
    }

    table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 14px;
        overflow: hidden;
        border-radius: 12px;
    }

    th, td {
        border-bottom: 1px solid #334155;
        padding: 10px;
        text-align: left;
        font-size: 14px;
        vertical-align: top;
    }

    th {
        color: #93c5fd;
        background: #020617;
    }

    code {
        color: #93c5fd;
    }

    footer {
        padding: 0 24px 24px;
        color: #64748b;
        font-size: 13px;
    }


    .chat-box { margin-top: 16px; background: #020617; border: 1px solid #334155; border-radius: 16px; padding: 16px; }
    .chat-message { margin-top: 12px; padding: 14px; background: #020617; border: 1px solid #334155; border-radius: 14px; }
    .chat-question { color: #93c5fd; font-weight: bold; margin-bottom: 8px; }
    .chat-answer { color: #e5e7eb; line-height: 1.45; }
    textarea { width: 100%; min-height: 90px; box-sizing: border-box; margin-top: 6px; padding: 10px; border-radius: 10px; border: 1px solid #334155; background: #020617; color: #e5e7eb; font-size: 15px; font-family: Arial, sans-serif; }

</style>
"""

@app.route("/")
def home():
    cards = ""

    for key, item in SERVICES.items():
        label, badge = service_status(item["service"])
        logline = file_info(item["log"])

        cards += f"""
        <div class="card">
            <div class="card-top">
                <div>
                    <h2>{item["title"]}</h2>
                    <p>{item["desc"]}</p>
                </div>
                <span class="badge {badge}">{label}</span>
            </div>

            <div class="service-name">
                {item["service"]}
            </div>

            <div class="logline">
                Log: {logline}
            </div>

            <div class="buttons">
                <a class="btn start" href="/start/{key}">Start</a>
                <a class="btn stop" href="/stop/{key}">Stop</a>
                <a class="btn restart" href="/restart/{key}">Restart</a>
            </div>
        </div>
        """

    api_label, api_badge = api_status()
    serial_state = "presente" if SERIAL_LOG.exists() else "non trovato"

    cards += f"""
    <div class="card">
        <div class="card-top">
            <div>
                <h2>Axis Real Test</h2>
                <p>Muove X/Y/Z, misura il tempo reale e calcola delta t rispetto al teorico.</p>
            </div>
            <span class="badge {api_badge}">{api_label}</span>
        </div>

        <div class="service-name">
            Log assi: {AXIS_LOG}
        </div>

        <div class="logline">
            serial.log: {serial_state}<br>
            path: <code>{SERIAL_LOG}</code>
        </div>

        <div class="buttons">
            <a class="btn restart" href="/axis">Apri test assi</a>
            <a class="btn secondary" href="/ai-engine">AI Engine</a><a class="btn secondary" href="/chat">Chatbot Antinea</a>
        </div>
    </div>
    """

    return f"""
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <title>Antinea Control</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="5">
        {BASE_STYLE}
    </head>

    <body>
        <header>
            <h1>Antinea Control</h1>
            <div class="subtitle">
                Dashboard locale per avviare e controllare i moduli dati
            </div>
        </header>

        <div class="container">
            {cards}
        </div>

        <footer>
            Aggiornamento automatico ogni 5 secondi - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        </footer>
    </body>
    </html>
    """

@app.route("/axis")
def axis_page(message=""):
    rows = read_last_axis_logs(30)
    all_rows = read_axis_logs()
    stats = axis_stats(all_rows)
    stats_html = build_stats_cards(stats)
    axis_overview_html = build_axis_overview_cards(all_rows)
    predictive_html = build_predictive_hint(all_rows)
    axis_detail_html = build_axis_detail_sections(stats)
    chart_html = build_axis_time_chart(all_rows, limit=60)
    axis_charts_html = build_axis_split_charts(all_rows, limit=60)
    api_label, api_badge = api_status()

    table_rows = ""
    for r in rows:
        status = r.get("status", "")
        real_s = safe_float(r.get("real_seconds"))
        theory_s = row_theoretical_seconds(r)
        delta_s = row_delta_seconds(r)
        real_txt = "" if real_s is None else f"{real_s:.4f}"
        theory_txt = "" if theory_s is None else f"{theory_s:.4f}"
        delta_txt = "" if delta_s is None else f"{delta_s:+.4f}"
        table_rows += f"""
        <tr>
            <td>{r.get("timestamp", "")}</td>
            <td>{r.get("axis", "")}{r.get("direction", "")}</td>
            <td>{r.get("distance_mm", "")}</td>
            <td>{r.get("feedrate", "")}</td>
            <td>{r.get("rep", "")}/{r.get("reps", "")}</td>
            <td><b>{real_txt}</b></td>
            <td>{theory_txt}</td>
            <td><b>{delta_txt}</b></td>
            <td>{status}</td>
        </tr>
        """

    if not table_rows:
        table_rows = '<tr><td colspan="9">Nessun test salvato ancora.</td></tr>'

    serial_warning = ""
    if not SERIAL_LOG.exists():
        serial_warning = f"""
        <div class="warning">
            Attenzione: non trovo <code>{SERIAL_LOG}</code>. 
            Per misurare il tempo reale devi abilitare il serial.log in OctoPrint oppure indicare il path corretto con
            <code>OCTOPRINT_SERIAL_LOG</code>.
        </div>
        """

    message_html = ""
    if message:
        message_html = f'<div class="warning">{message}</div>'

    return f"""
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <title>Axis Real Test - Antinea</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        {BASE_STYLE}
    </head>
    <body>
        <header>
            <h1>Axis Real Test</h1>
            <div class="subtitle">
                Misura il tempo reale dello spostamento tramite marker <code>M118</code> nel <code>serial.log</code>.
            </div>
        </header>

        <div class="page">
            <div class="card">
                <div class="card-top">
                    <div>
                        <h2>Nuovo test movimento</h2>
                        <p>Qui il valore principale è <b>delta t</b>: confronto tra tempo reale medio e tempo teorico da feedrate.</p>
                    </div>
                    <span class="badge {api_badge}">{api_label}</span>
                </div>

                <div class="warning">
                    Prima del test: assi liberi, estrusore lontano dal piatto, homing fatto se necessario.
                    Limiti prudenziali: X/Y max 50 mm, Z max 5 mm.
                </div>

                {serial_warning}
                {message_html}

                <form method="POST" action="/axis/run">
                    <label>Asse</label>
                    <select name="axis">
                        <option value="X">X</option>
                        <option value="Y">Y</option>
                        <option value="Z">Z</option>
                    </select>

                    <label>Direzione</label>
                    <select name="direction">
                        <option value="+">+</option>
                        <option value="-">-</option>
                    </select>

                    <label>Distanza mm</label>
                    <input name="distance" type="number" step="0.1" value="10">

                    <label>Feedrate mm/min</label>
                    <input name="feedrate" type="number" step="1" value="600">

                    <label>Ripetizioni</label>
                    <input name="reps" type="number" step="1" value="1">

                    <div class="buttons">
                        <button class="start" type="submit">Esegui e salva tempo reale</button>
                        <a class="btn secondary" href="/">Torna alla dashboard</a>
                        <a class="btn restart" href="/ai-engine">AI Engine</a><a class="btn restart" href="/chat">Apri chatbot</a>
                    </div>
                </form>

                <form method="POST" action="/axis/reset" onsubmit="return confirm('Vuoi davvero azzerare tutti i dati dei test assi?');">
                    <div class="buttons">
                        <button class="danger" type="submit">Reset tutti i dati assi</button>
                    </div>
                </form>

                <div class="note">
                    Reset selettivo: cancella solo i dati dell'asse scelto, lasciando intatti gli altri assi.
                    <div class="buttons">
                        <form method="POST" action="/axis/reset_axis/X" onsubmit="return confirm('Azzerare solo i dati asse X?');"><button class="danger" type="submit">Reset X</button></form>
                        <form method="POST" action="/axis/reset_axis/Y" onsubmit="return confirm('Azzerare solo i dati asse Y?');"><button class="danger" type="submit">Reset Y</button></form>
                        <form method="POST" action="/axis/reset_axis/Z" onsubmit="return confirm('Azzerare solo i dati asse Z?');"><button class="danger" type="submit">Reset Z</button></form>
                    </div>
                </div>

                <div class="note">
                    Metodo: invio <code>M118 ANTINEA_AXIS_START</code>, poi movimento relativo, poi <code>M400</code>, poi
                    <code>M118 ANTINEA_AXIS_END</code>. Il tempo reale è calcolato tra START e END osservati nel serial.log.
                    Formula teorica: <code>tempo_teorico_s = distanza_mm × 60 / feedrate</code>. Formula diagnostica: <code>delta_t = tempo_medio_reale - tempo_teorico</code>.
                </div>
            </div>

            <div class="card" style="margin-top:20px;">
                <h2>Statistiche globali e delta t</h2>
                <p>Calcolo automatico sul file <code>{AXIS_LOG.name}</code>: vengono considerati solo i movimenti con stato <b>ok</b>. Il tempo teorico usa <code>distanza_mm × 60 / feedrate</code>.</p>
                {stats_html}

                <h2 style="margin-top:22px;">Riepilogo separato per asse</h2>
                <p>Qui il delta non è più unico: viene separato fisicamente in X, Y e Z.</p>
                {axis_overview_html}
            </div>

            <div class="card" style="margin-top:20px;">
                <h2>Chicca predittiva: asse più sospetto</h2>
                <p>Confronta il delta recente con quello precedente per capire se un asse sta peggiorando nel tempo.</p>
                {predictive_html}
                <h2 style="margin-top:22px;">AI Engine numerico</h2>
                {build_ai_engine_table(all_rows)}
            </div>

            <div class="card" style="margin-top:20px;">
                <h2>Dettaglio per asse, direzione, distanza e feedrate</h2>
                <p>Questa è la parte più utile per la diagnostica: confronta solo movimenti omogenei, quindi stesso asse, stessa direzione, stessa distanza e stesso feedrate.</p>
                {axis_detail_html}
            </div>

            <div class="card" style="margin-top:20px;">
                <h2>Grafico globale reale vs teorico</h2>
                {chart_html}
            </div>

            <div class="card" style="margin-top:20px;">
                <h2>Grafici separati per asse</h2>
                <p>Ogni asse ha il suo grafico, così vedi subito se il delta cresce solo su X, solo su Y o solo su Z.</p>
                {axis_charts_html}
            </div>

            <div class="card" style="margin-top:20px;">
                <h2>Ultimi test salvati</h2>
                <table>
                    <tr>
                        <th>Timestamp</th>
                        <th>Asse</th>
                        <th>mm</th>
                        <th>F</th>
                        <th>Rep</th>
                        <th>Tempo reale s</th>
                        <th>Tempo teorico s</th>
                        <th>Delta t s</th>
                        <th>Stato</th>
                    </tr>
                    {table_rows}
                </table>
            </div>
        </div>
    </body>
    </html>
    """





@app.route('/ai-engine')
def ai_engine_page():
    rows = read_axis_logs()
    engine_html = build_ai_engine_table(rows)
    return f"""
    <!DOCTYPE html>
    <html lang="it">
    <head><title>Antinea AI Engine</title><meta name="viewport" content="width=device-width, initial-scale=1">{BASE_STYLE}</head>
    <body>
        <header><h1>Antinea AI Engine</h1><div class="subtitle">Motore numerico leggero: baseline, z-score, linear regression, health index e isolation-lite.</div></header>
        <div class="page"><div class="card"><h2>Motore AI numerico</h2><p>Questa pagina non comanda la stampante: legge solo i log assi e calcola lo stato predittivo.</p>{engine_html}<div class="buttons"><a class="btn secondary" href="/axis">Test assi</a><a class="btn secondary" href="/chat">Chatbot</a><a class="btn secondary" href="/">Dashboard</a></div></div></div>
    </body></html>
    """


@app.route('/chat', methods=['GET'])
def chat_page(message=''):
    context = build_ai_context(read_axis_logs())
    history_html = build_chat_history_html(read_chat_history(limit=20))
    message_html = f'<div class="warning">{html.escape(message)}</div>' if message else ''
    axis_summary = ''
    for axis in AXES:
        d = context['axes'].get(axis, {})
        axis_summary += f"""
        <tr><td><b>{axis}</b></td><td>{d.get('valid_tests', 0)}</td><td>{fmt_delta(d.get('avg_delta'))}</td><td>{fmt_delta(d.get('last_delta'))}</td><td>{html.escape(str(d.get('trend_status', 'N/D')))}</td></tr>
        """
    return f"""
    <!DOCTYPE html><html lang="it"><head><title>Antinea Chatbot</title><meta name="viewport" content="width=device-width, initial-scale=1">{BASE_STYLE}</head>
    <body><header><h1>Antinea Chatbot</h1><div class="subtitle">Chat diagnostica locale: legge i dati assi e spiega delta t, trend e manutenzione. Non invia comandi alla stampante.</div></header>
    <div class="page">
        <div class="card"><div class="card-top"><div><h2>Domanda diagnostica</h2><p>Chiedi ad Antinea cosa sta succedendo agli assi usando i dati già salvati.</p></div><span class="badge online">LOCALE</span></div>
        {message_html}
        <form method="POST" action="/chat/ask"><label>Domanda</label><textarea name="question" placeholder="Esempio: quale asse è più sospetto? Perché X peggiora? Che manutenzione faccio prima?"></textarea><div class="buttons"><button class="start" type="submit">Chiedi ad Antinea</button><a class="btn secondary" href="/axis">Torna ai test assi</a><a class="btn secondary" href="/">Dashboard</a></div></form>
        <div class="note">Versione leggera per Raspberry Pi 3: regole diagnostiche + dati reali. TinyLlama può essere aggiunto dopo come interprete testuale opzionale.</div></div>
        <div class="card" style="margin-top:20px;"><h2>Contesto letto dal chatbot</h2><table><tr><th>Asse</th><th>Test validi</th><th>Delta medio</th><th>Ultimo delta</th><th>Trend</th></tr>{axis_summary}</table><div class="note">Test validi globali: {context.get('valid_tests')}/{context.get('total_tests')}. Asse più sospetto: <b>{context.get('most_suspicious_axis') or 'N/D'}</b>.</div></div>
        <div class="card" style="margin-top:20px;"><h2>Storico chat</h2><div class="chat-box">{history_html}</div></div>
    </div></body></html>
    """


@app.route('/chat/ask', methods=['POST'])
def chat_ask():
    try:
        question = request.form.get('question', '').strip()
        answer = simple_antinea_chatbot(question, build_ai_context(read_axis_logs()))
        append_chat_log(question, answer)
        return redirect('/chat')
    except Exception as e:
        return chat_page(message=f'Errore chatbot: {e}')

@app.route("/axis/reset", methods=["POST"])
def axis_reset():
    try:
        if AXIS_LOG.exists():
            AXIS_LOG.unlink()
        return axis_page(message="Dati test assi azzerati correttamente.")
    except Exception as e:
        return axis_page(message=f"Errore reset dati assi: {e}")

@app.route("/axis/reset_axis/<axis_name>", methods=["POST"])
def axis_reset_single(axis_name):
    try:
        axis = str(axis_name).upper().strip()
        if axis not in AXES:
            raise ValueError("asse non valido")
        if not AXIS_LOG.exists():
            return axis_page(message=f"Nessun dato da azzerare per asse {axis}.")

        kept_lines = []
        removed = 0
        for line in AXIS_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
                if str(row.get("axis", "")).upper() == axis:
                    removed += 1
                    continue
            except Exception:
                # Se una riga è corrotta, la mantengo per non perdere dati grezzi.
                pass
            kept_lines.append(line)

        AXIS_LOG.write_text(("\n".join(kept_lines) + "\n") if kept_lines else "", encoding="utf-8")
        return axis_page(message=f"Dati asse {axis} azzerati: {removed} righe rimosse.")
    except Exception as e:
        return axis_page(message=f"Errore reset asse: {e}")

@app.route("/axis/run", methods=["POST"])
def axis_run():
    try:
        axis = request.form.get("axis", "X").upper().strip()
        direction = request.form.get("direction", "+").strip()
        distance = float(request.form.get("distance", "10"))
        feedrate = int(float(request.form.get("feedrate", "600")))
        reps = int(float(request.form.get("reps", "1")))

        if axis not in ("X", "Y", "Z"):
            raise ValueError("asse non valido")
        if direction not in ("+", "-"):
            raise ValueError("direzione non valida")
        if distance <= 0:
            raise ValueError("distanza deve essere positiva")
        if feedrate <= 0:
            raise ValueError("feedrate deve essere positivo")
        if reps < 1 or reps > 20:
            raise ValueError("ripetizioni consentite: 1-20")

        max_distance = 5 if axis == "Z" else 50
        if distance > max_distance:
            raise ValueError(f"distanza troppo alta per {axis}: max {max_distance} mm")

        signed_distance = distance if direction == "+" else -distance

        if not OCTOPRINT_API_KEY:
            raise RuntimeError("API key OctoPrint mancante: imposta OCTOPRINT_API_KEY nel servizio dashboard")

        if not SERIAL_LOG.exists():
            raise RuntimeError(f"serial.log non trovato: {SERIAL_LOG}")

        ok_count = 0

        for rep in range(1, reps + 1):
            run_id = uuid.uuid4().hex[:10]
            start_marker = f"ANTINEA_AXIS_START {run_id}"
            end_marker = f"ANTINEA_AXIS_END {run_id}"

            file_pos = SERIAL_LOG.stat().st_size

            commands = [
                f"M118 {start_marker}",
                "G91",
                f"G1 {axis}{signed_distance:.3f} F{feedrate}",
                "M400",
                "G90",
                f"M118 {end_marker}"
            ]

            api_start = time.monotonic()
            post_octoprint_commands(commands)
            api_elapsed = round(time.monotonic() - api_start, 4)

            result = wait_real_time_from_serial(start_marker, end_marker, file_pos, timeout_s=90)
            theory_s = theoretical_seconds(distance, feedrate)
            real_s = result.get("real_seconds")
            delta_s = (real_s - theory_s) if result.get("ok") and real_s is not None and theory_s is not None else None
            delta_pct = (delta_s / theory_s * 100.0) if delta_s is not None and theory_s else None

            record = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "run_id": run_id,
                "axis": axis,
                "direction": direction,
                "distance_mm": distance,
                "signed_distance_mm": signed_distance,
                "feedrate": feedrate,
                "rep": rep,
                "reps": reps,
                "real_seconds": real_s,
                "theoretical_seconds": round(theory_s, 4) if theory_s is not None else None,
                "delta_seconds": round(delta_s, 4) if delta_s is not None else None,
                "delta_percent": round(delta_pct, 2) if delta_pct is not None else None,
                "api_elapsed_seconds": api_elapsed,
                "status": "ok" if result.get("ok") else "marker_error",
                "error": result.get("error"),
                "start_seen": result.get("start_seen"),
                "end_seen": result.get("end_seen"),
                "start_line": result.get("start_line"),
                "end_line": result.get("end_line"),
                "serial_log": str(SERIAL_LOG),
                "commands": commands
            }

            append_axis_log(record)

            if result.get("ok"):
                ok_count += 1

        return redirect("/axis")

    except Exception as e:
        return axis_page(message=f"Errore test assi: {e}")

@app.route("/start/<name>")
def start(name):
    if name in SERVICES:
        systemctl("start", SERVICES[name]["service"])
    return redirect("/")

@app.route("/stop/<name>")
def stop(name):
    if name in SERVICES:
        systemctl("stop", SERVICES[name]["service"])
    return redirect("/")

@app.route("/restart/<name>")
def restart(name):
    if name in SERVICES:
        systemctl("restart", SERVICES[name]["service"])
    return redirect("/")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
