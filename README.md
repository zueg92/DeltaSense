# DeltaSense

**Lightweight predictive maintenance for 3D printers using Raspberry Pi, OctoPrint and delta-time analysis.**

DeltaSense measures the real movement time of printer axes, compares it with the theoretical movement time calculated from feedrate, and uses lightweight AI/statistical methods to detect early mechanical degradation.

> Core idea: `delta_t = real_time - theoretical_time`

## Features

- Flask web dashboard for Raspberry Pi
- Axis movement test for X/Y/Z
- Real movement time measured through OctoPrint serial markers
- Theoretical time from distance and feedrate
- Delta-t diagnostics
- Per-axis charts
- Reset per axis
- Mini AI Engine
  - baseline
  - z-score
  - linear regression
  - health index
  - isolation-lite
  - short degradation forecast
- Local diagnostic chatbot page
- Designed for Raspberry Pi 3 and future multi-printer architecture

## Architecture

```text
3D Printer ── OctoPrint ── Raspberry Pi ── DeltaSense Dashboard
                                      │
                                      ├── Axis tests
                                      ├── Delta-t logs
                                      ├── AI Engine
                                      └── Chatbot diagnostics
```

Future distributed mode:

```text
Printer Node 1 ─┐
Printer Node 2 ─┼── DeltaSense Central Server
Printer Node 3 ─┘
```

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/DeltaSense.git
cd DeltaSense
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 dashboard/dashboard.py
```

Open:

```text
http://RASPBERRY_IP:8080
```

## OctoPrint configuration

DeltaSense needs:

```text
OCTOPRINT_URL
OCTOPRINT_API_KEY
OCTOPRINT_SERIAL_LOG
```

Example:

```bash
export OCTOPRINT_URL="http://127.0.0.1:5000"
export OCTOPRINT_API_KEY="your_api_key_here"
export OCTOPRINT_SERIAL_LOG="$HOME/.octoprint/logs/serial.log"
```

## Safety note

DeltaSense sends controlled diagnostic movements to the printer. Use conservative distances and make sure axes are free before testing.

Recommended limits:

```text
X/Y: max 50 mm
Z: max 5 mm
```

## Roadmap

### v0.1
- [x] Flask dashboard
- [x] Axis movement test
- [x] Real vs theoretical time
- [x] Delta-t logging
- [x] Per-axis charts

### v0.2
- [x] Health Index
- [x] z-score anomaly detection
- [x] Linear regression trend
- [x] Forecast degradation
- [x] Diagnostic chatbot page

### v0.3
- [ ] SQLite database
- [ ] Multi-printer config file
- [ ] Printer fleet page
- [ ] Export CSV/JSON

### v0.4
- [ ] Central server mode
- [ ] Multiple OctoPrint nodes
- [ ] Advanced AI models
- [ ] Optional LLM explanation layer

## License

MIT License.

