import time
import json
import re
from pathlib import Path

BASE = Path.home() / "antinea"
LOG_OUT = BASE / "logs" / "serial_parser.ndjson"

# log seriale OctoPrint
SERIAL_LOG = Path.home() / ".octoprint" / "logs" / "serial.log"

def write_log(entry):
    with open(LOG_OUT, "a") as f:
        f.write(json.dumps(entry) + "\n")

def parse_line(line):
    now = time.time()

    entry = {
        "time": now,
        "type": "serial_raw",
        "raw": line.strip()
    }

    # M105 temperatura
    if "T:" in line or "B:" in line:
        entry["detected"] = "temperature_response"

        t_match = re.search(r"T:([0-9.]+)", line)
        b_match = re.search(r"B:([0-9.]+)", line)

        if t_match:
            entry["hotend_actual"] = float(t_match.group(1))
        if b_match:
            entry["bed_actual"] = float(b_match.group(1))

    # M114 posizione
    if "X:" in line and "Y:" in line and "Z:" in line:
        entry["detected"] = "position_response"

        x = re.search(r"X:([-0-9.]+)", line)
        y = re.search(r"Y:([-0-9.]+)", line)
        z = re.search(r"Z:([-0-9.]+)", line)
        e = re.search(r"E:([-0-9.]+)", line)

        if x:
            entry["x"] = float(x.group(1))
        if y:
            entry["y"] = float(y.group(1))
        if z:
            entry["z"] = float(z.group(1))
        if e:
            entry["e"] = float(e.group(1))

    # M119 endstop
    if "Reporting endstop status" in line:
        entry["detected"] = "endstop_header"

    if "x_min" in line or "y_min" in line or "z_min" in line:
        entry["detected"] = "endstop_response"

        endstops = {}
        for axis in ["x_min", "y_min", "z_min", "x_max", "y_max", "z_max"]:
            m = re.search(axis + r":\s*(\w+)", line)
            if m:
                endstops[axis] = m.group(1)

        entry["endstops"] = endstops

    # M851 Z offset
    if "M851" in line or "Z Probe Offset" in line:
        entry["detected"] = "z_offset_response"

    # M420 / mesh
    if "Bed Topography" in line or "Mesh" in line or "G29" in line:
        entry["detected"] = "mesh_response"

    return entry

def follow_file(path):
    print("Serial parser avviato")
    print("Leggo:", path)

    while not path.exists():
        print("Aspetto serial.log...")
        time.sleep(2)

    with open(path, "r", errors="ignore") as f:
        f.seek(0, 2)  # vai a fine file

        while True:
            line = f.readline()

            if not line:
                time.sleep(0.2)
                continue

            entry = parse_line(line)
            write_log(entry)

            if entry.get("detected"):
                print("PARSED:", entry["detected"], entry)

if __name__ == "__main__":
    follow_file(SERIAL_LOG)
