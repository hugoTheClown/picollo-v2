#!/usr/bin/env python3
"""Compile and upload button.ino to an attached ESP32-C3 via arduino-cli.

Run from this directory (or anywhere — it locates button.ino next to itself):
    python3 flash.py [--watch [SECONDS]] [port]

If no port is given, it auto-detects a likely ESP32-C3 serial device
(matches /dev/ttyACM* or /dev/ttyUSB*). Pass --watch to stream the serial
monitor right after flashing (default 10s; add a number to change that),
useful for confirming the boot sequence without a second command.
"""

import glob
import os
import subprocess
import sys

# CDCOnBoot=cdc is required for Serial (USB CDC) output to actually work on
# the C3's native USB port — without it, Serial.print() silently goes nowhere.
FQBN = "esp32:esp32:esp32c3:CDCOnBoot=cdc"
SKETCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "button")
ARDUINO_CLI = os.path.expanduser("~/.local/bin/arduino-cli")


def find_arduino_cli():
    if os.path.isfile(ARDUINO_CLI):
        return ARDUINO_CLI
    found = subprocess.run(["which", "arduino-cli"], capture_output=True, text=True)
    if found.returncode == 0:
        return found.stdout.strip()
    sys.exit("arduino-cli not found. Expected it at ~/.local/bin/arduino-cli or on PATH.")


def find_port():
    candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not candidates:
        sys.exit(
            "No /dev/ttyACM* or /dev/ttyUSB* device found. "
            "Is the ESP32-C3 plugged in? Check with 'lsusb'."
        )
    if len(candidates) > 1:
        print(f"Multiple serial devices found: {candidates}, using {candidates[0]}")
    return candidates[0]


def run(cmd):
    print("+", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    args = sys.argv[1:]
    watch_seconds = None
    if "--watch" in args:
        i = args.index("--watch")
        args.pop(i)
        if i < len(args) and args[i].isdigit():
            watch_seconds = int(args.pop(i))
        else:
            watch_seconds = 10

    cli = find_arduino_cli()
    port = args[0] if args else find_port()

    if not os.access(port, os.R_OK | os.W_OK):
        sys.exit(
            f"No permission to access {port}. Either run this with sudo, "
            f"or add yourself to the 'dialout' group and reconnect:\n"
            f"  sudo usermod -aG dialout $USER   (then log out and back in)"
        )

    print(f"Compiling for {FQBN}...")
    run([cli, "compile", "--fqbn", FQBN, SKETCH_DIR])

    print(f"Uploading to {port}...")
    run([cli, "upload", "-p", port, "--fqbn", FQBN, SKETCH_DIR])

    print("Done. Watch the OLED/LED on the board for the boot sequence.")

    if watch_seconds is not None:
        print(f"Watching serial output for {watch_seconds}s (Ctrl+C to stop early)...")
        try:
            subprocess.run(
                ["timeout", str(watch_seconds), cli, "monitor", "-p", port, "-c", "baudrate=115200"]
            )
        except KeyboardInterrupt:
            pass
    else:
        print(f"Serial monitor: {cli} monitor -p {port} -c baudrate=115200")


if __name__ == "__main__":
    main()
