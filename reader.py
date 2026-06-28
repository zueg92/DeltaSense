import json
import time
import requests
from pathlib import Path

BASE = Path.home() / "antinea"
CONFIG = BASE / "config" / "octoprint.json"
LOG = BASE / "logs" / "octoprint_reader.ndjson"

def load_config():
    with open(CONFIG, "r") as f:
        return json.load(f)

def write_log(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

def get_json(url, api_key, endpoint):
    r = requests.get(
        url + endpoint,
        headers={"X-Api-Key": api_key},
        timeout=5
    )
    r.raise_for_status()
    return r.json()

def main():
    cfg = load_config()
    url = cfg["url"]
    api_key = cfg["api_key"]
    interval = cfg.get("read_interval", 2)

    print("Antinea reader avviato")

    while True:
        try:
            entry = {
                "time": time.time(),
                "type": "octoprint_snapshot",
                "printer_id": cfg["printer_id"],
                "printer_name": cfg["name"],
                "printer": get_json(url, api_key, "/api/printer"),
                "job": get_json(url, api_key, "/api/job"),
                "connection": get_json(url, api_key, "/api/connection")
            }

            write_log(entry)
            print("reader OK")

        except Exception as e:
            write_log({
                "time": time.time(),
                "type": "reader_error",
                "printer_id": cfg["printer_id"],
                "error": str(e)
            })
            print("reader ERRORE:", e)

        time.sleep(interval)

if __name__ == "__main__":
    main()
