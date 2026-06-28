import json
import time
import requests
from pathlib import Path

BASE = Path.home() / "antinea"
CONFIG = BASE / "config" / "octoprint.json"
LOG = BASE / "logs" / "octoprint_commands.ndjson"

def load_config():
    with open(CONFIG, "r") as f:
        return json.load(f)

def write_log(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

def send_command(url, api_key, command, purpose):
    r = requests.post(
        url + "/api/printer/command",
        headers={
            "X-Api-Key": api_key,
            "Content-Type": "application/json"
        },
        json={"command": command},
        timeout=5
    )
    r.raise_for_status()

    write_log({
        "time": time.time(),
        "type": "command_sent",
        "command": command,
        "purpose": purpose
    })

    print("sent:", command)

def main():
    cfg = load_config()
    url = cfg["url"]
    api_key = cfg["api_key"]

    print("Antinea commands avviato")

    # snapshot iniziale macchina
    startup = [
        ("M503", "firmware_config_snapshot"),
        ("M851", "z_offset_snapshot"),
        ("M420 V", "mesh_bed_leveling_snapshot")
    ]

    for cmd, purpose in startup:
        try:
            send_command(url, api_key, cmd, purpose)
            time.sleep(2)
        except Exception as e:
            print("errore startup:", cmd, e)

    counter = 0

    while True:
        try:
            # ogni 30 sec circa
            if counter % 6 == 0:
                send_command(url, api_key, "M105", "temperature_check")

            # ogni 60 sec circa
            if counter % 12 == 0:
                send_command(url, api_key, "M114", "position_check")

            # ogni 5 min circa
            if counter % 60 == 0:
                send_command(url, api_key, "M119", "endstop_check")

        except Exception as e:
            write_log({
                "time": time.time(),
                "type": "command_error",
                "error": str(e)
            })
            print("commands ERRORE:", e)

        counter += 1
        time.sleep(2)

if __name__ == "__main__":
    main()
