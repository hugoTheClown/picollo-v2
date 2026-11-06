# Picollo — event photobooth

Standalone photobooth for events: press a button, a DSLR fires and the photo
lands on disk, ready for later upload/printing. Planned next step is a
green-screen background swap, picked on a tablet touchscreen.

Full design decisions, rationale, and current build status live in
[`PROJECT.md`](PROJECT.md) — this file covers the OS/software stack and how
to run things.

## How it works

```
[ESP32-C3 button] --WiFi/HTTP POST--> [NUC: Flask listener] --gphoto2--> [DSLR]
                                             |
                                             v
                                     photos/YYYY-MM-DD/HHMMSS.jpg
```

- A button wired to an ESP32-C3 (with onboard OLED + status LED) triggers
  a **two-stage** flow: 1st press arms liveview (`POST /liveview/start`),
  2nd press takes the photo (`POST /capture`, which also stops liveview).
  Auto-disarms if nobody follows through.
- The NUC's listener service shells out to `gphoto2` to stream the preview
  or trigger/download a still capture.
- A kiosk-mode browser on the NUC (`/kiosk`) shows an idle branding screen
  that switches to the live camera feed while armed, guest-facing.
- A separate operator control panel (`/`) and a photo gallery (`/gallery`)
  are also served by the same listener — see Usage below.

## Hardware

- **NUC:** Intel NUC, wired via Ethernet to a dedicated private access
  point (not venue WiFi).
- **Camera:** Canon EOS 100D, tethered via USB (`gphoto2`/`libgphoto2`).
- **Button:** ESP32-C3 + OLED display, joins the booth WiFi, triggers
  captures over HTTP.
- **AP:** TP-Link TL-WR820N (`http://10.0.2.1/`).
- Planned but not yet built: tablet (background picker), Shelly smart plug
  (green-screen lighting), printer.

## OS

**Debian 12 (Bookworm)**, installed with no desktop-environment tasks
selected in `tasksel` (only "standard system utilities" + "SSH server").
No KDE/GNOME — the NUC runs a single kiosk app, so a full desktop is
unneeded overhead and attack/failure surface. Chosen over Ubuntu; the usual
Debian non-free-firmware caveat for Intel WiFi doesn't apply since the NUC
is wired via Ethernet.

## Software stack & installation

Everything below is installed directly on the NUC (not in a container).
Run `sudo apt-get update` once before the installs.

### Camera tethering

```sh
sudo apt-get install -y gphoto2
```

Provides `gphoto2`, used by the listener service to capture and download
photos over USB. Confirm the camera is detected with
`gphoto2 --auto-detect`.

### Listener service (Flask)

```sh
sudo apt-get install -y python3-flask
```

Installed via `apt`, not `pip` — keeps it in sync with the system Python
and avoids managing a virtualenv for a single small service. Runs as a
systemd **user** service (no root needed):

```sh
systemctl --user daemon-reload
systemctl --user enable --now photobooth-listener.service
```

Unit file: `~/.config/systemd/user/photobooth-listener.service`, runs
`python3 ~/photobooth/listener/app.py`. Check it with:

```sh
systemctl --user status photobooth-listener.service --no-pager
curl -s http://localhost:8080/health
```

### Kiosk display

```sh
sudo apt-get install -y xserver-xorg xinit openbox chromium unclutter x11-xserver-utils
```

- `xserver-xorg` + `xinit` — minimal X11, no display manager.
- `openbox` — lightweight window manager (Chromium runs fullscreen anyway,
  but something needs to manage the X session).
- `chromium` — shows the listener's `/kiosk` page in `--kiosk` mode.
- `unclutter` — hides the mouse cursor.
- `x11-xserver-utils` — provides `xset`, used to disable screen
  blanking/DPMS.

Autostart wiring: `~/.profile` runs `startx` only when logged in on the
physical console (`tty1`) with no `$DISPLAY` set, so SSH sessions are
never affected. `~/.xinitrc` disables screen blanking, starts `unclutter` +
`openbox-session`, waits for the listener's `/health` endpoint, then execs
`chromium --kiosk http://localhost:8080/kiosk`.

**Not yet done:** tty1 auto-login (needs a root-owned systemd override
and a console password prompt to set up — see `PROJECT.md`), so the kiosk
doesn't yet start fully unattended on boot.

### ESP32-C3 button firmware

Flashed from a separate machine (Arduino IDE), not from the NUC:

1. Arduino IDE → Boards Manager → install **esp32** (by Espressif Systems).
2. Library Manager → install **Adafruit SSD1306** and **Adafruit GFX
   Library** (OLED status display).
3. Board: "ESP32C3 Dev Module" (or the specific board variant).
4. Open `esp32-button/button/button.ino`, fill in WiFi/NUC config, upload.

Full wiring, pinout, and flashing details: `esp32-button/README.md`.

### Version control

```sh
sudo apt-get install -y git gh
```

Used to back this project up to GitHub. Auth is via an SSH deploy key
(`~/.ssh/id_ed25519_github`), not `gh auth login` / tokens — avoids ever
having a secret pass through a remote session's scrollback.

## Repo layout

```
photobooth/
├── listener/app.py       Flask service: capture, preview, liveview, gallery
├── esp32-button/          ESP32-C3 firmware + its own README (wiring/flashing)
├── photos/                Captured images, YYYY-MM-DD/HHMMSS.jpg (gitignored)
└── PROJECT.md             Full design log: decisions, status, open questions
```

## Usage

**Control panel** — `http://<nuc-ip>:8080/` (e.g. `http://10.0.2.10:8080/`).
Operator-facing page: listener/camera status, photos-today count, last
capture result, a "Vyfotit" button to trigger a capture manually, and a
short how-it-works summary. This is what you'd open from a laptop/phone on
the booth network to check on things during an event.

**Kiosk display** — `http://<nuc-ip>:8080/kiosk`. Guest-facing fullscreen:
idle "📸 Picollo" screen, switches to the live camera feed while liveview
is armed. This is what the kiosk browser on the NUC itself shows
(`~/.xinitrc` points `chromium --kiosk` here).

**Gallery** — `http://<nuc-ip>:8080/gallery`. Grid of captured photos,
newest first, grouped by day; click a thumbnail to open the full-size
image.

**Drive the two-stage flow from the command line** (same effect as the
physical button):

```sh
curl -s -X POST http://localhost:8080/liveview/start   # 1st press: arm liveview
curl -s -X POST http://localhost:8080/capture           # 2nd press: take the photo
```

**Check status programmatically:**

```sh
curl -s http://localhost:8080/health            # liveness
curl -s http://localhost:8080/status             # photos today + last capture, as JSON
curl -s http://localhost:8080/liveview/status    # is the kiosk currently showing live feed?
```

**Restart the listener after editing `app.py`:**

```sh
systemctl --user restart photobooth-listener.service
```

**Button hardware:** once wired and flashed (see `esp32-button/README.md`),
the physical button drives the same two-stage flow — OLED and status LED
reflect each stage (idle / armed / capturing / result).

Photos land in `photos/YYYY-MM-DD/HHMMSS.jpg`; upload/printing from there
is future work, not yet implemented.

## Status

Implemented and verified end-to-end on real hardware (2026-08-18): camera
capture via the listener. **Currently flaky (2026-11-06):** the camera is
timing out on all `gphoto2` commands, including non-capture ones — looks
like a hardware/USB issue, needs a physical check (see `PROJECT.md`).
Also found and fixed: this camera's `gphoto2` can report success (exit 0)
on a failed capture, so `/capture` now verifies the file actually landed
on disk.

Written but not yet hardware-verified: the ESP32-C3 button firmware (now
with the two-stage liveview/capture flow + status LED), the kiosk autostart
chain. Not started: green-screen pipeline, tablet client, lighting
control, printing. See `PROJECT.md` for the detailed, dated log.
