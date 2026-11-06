# Photobooth Project — Architecture & Decisions

Context for Claude Code (or anyone picking this up). This summarizes decisions made
in planning before the NUC was set up. Update this file as the build progresses.

## Goal

A standalone event photobooth: button press triggers a photo, image saved locally,
uploaded and printed later. Future addition: green-screen background replacement,
selectable via a tablet touchscreen.

## Hardware / OS

- **Main unit:** Intel NUC, connected to the network via **Ethernet** (not WiFi) —
  plugged into a dedicated access point that serves the whole booth ecosystem.
- **OS:** Debian 12 (Bookworm). Chosen over Ubuntu; the usual Debian caveat
  (Intel WiFi needing non-free firmware) doesn't apply since the NUC is wired.
- **Camera:** Canon EOS 100D (Rebel SL1 / Kiss X7). Well-supported by
  `gphoto2`/`libgphoto2` via standard PTP over USB — no known tethering
  quirks. **Confirmed working** (2026-08-18): visible via `lsusb` as
  `04a9:3270 Canon, Inc. EOS 100D`, detected by `gphoto2 --auto-detect`,
  and successfully captured/downloaded a real photo end-to-end through
  the listener service below.
  - **Power:** Use a DC/AC coupler (dummy battery) for continuous power
    during events — don't rely on the camera battery for all-day tethered
    shooting.
  - **Auto power-off:** Disable in-camera auto-sleep/auto-power-off so it
    doesn't miss triggers between photos.
  - **Focus:** Prefer manual focus, pre-set for the fixed standing spot,
    over relying on AF per shot.
  - **USB cable:** Keep it short and good quality — flaky USB cables are
    the most common source of `gphoto2` tethering issues in general.
  - **SD card:** Inserted — acts as a local backup/buffer on the camera
    itself in case USB/`gphoto2` transfer hiccups mid-event. Doesn't need
    to be large or fast, just present.
- **Network:** Dedicated private AP for the booth (NOT venue WiFi — avoids
  client isolation / congestion issues at events). NUC is wired to it via
  Ethernet; the ESP32 and (later) tablet join it over WiFi.

## Button (capture trigger)

- **ESP32** mounted at the button location itself (not near the NUC).
- Button wires directly to the ESP32 with short local wiring — no long noisy
  cable run needed, since the ESP32 is right there.
- ESP32 connects to the booth's private WiFi (via the AP) and sends an
  **HTTP POST** to a small listener service running on the NUC when pressed.
  (Chosen over MQTT/WebSocket for simplicity — fine for a single button.)
- NUC listener receives the trigger → calls `gphoto2` to capture → saves file.
- Power to the ESP32: either a local USB power source at the button stand, or
  a 5V power cable run to it (thicker gauge if run over distance — this is
  just power, not a sensitive signal line, so noise isn't a concern here).
- Nice-to-haves for reliability: onboard LED for WiFi-connected status,
  retry/reconnect logic in firmware so a brief WiFi drop doesn't need a
  manual reboot.
- **Board (2026-11-06, provisional — "zatím"): ESP32-C3 with onboard OLED.**
- **Final trigger behavior (decided 2026-11-06): two-stage.** First press
  arms liveview (kiosk display switches from idle screen to the live camera
  feed, so the guest can see themselves); second press takes the actual
  photo (liveview auto-stops as part of that). Auto-disarms after ~20s if
  nobody follows through with the second press.
- **Implemented** (2026-11-06): `~/photobooth/esp32-button/button.ino`.
  Button on `INPUT_PULLUP` (default GPIO4), separate status **LED**
  (default GPIO7: solid while armed/liveview, fast blink while capturing,
  off when idle) — both avoid the C3's strapping pins 2/8/9. First press →
  `POST /liveview/start`; second press → `POST /capture`. OLED shows
  `Ready <ip>`, then `Divej se!` while armed, `Fotim...` while capturing,
  then the result for ~1.5s. Auto-reconnects on WiFi drop; restarts itself
  only if disconnected for 2+ minutes straight.
  Wiring also includes a ~100nF capacitor in parallel across the button
  for hardware debounce, on top of the firmware's 250ms software debounce.
  See `esp32-button/README.md` (now includes a wiring diagram, `wiring.jpg`)
  for wiring and flashing steps.
- **Not yet done:** WIFI_SSID/PASSWORD and NUC_HOST placeholders need real
  values; the OLED's I2C pins/resolution in the sketch are unconfirmed
  guesses (GPIO5/6, 128x64) pending the exact board/listing; untested on
  real hardware — no ESP32-C3 attached to this NUC session and no Arduino
  toolchain installed here.

## Listener service (NUC)

- **Implemented** (2026-08-18): `~/photobooth/listener/app.py`, a small
  Flask app. Built to handle multiple input sources from the start, per
  the original design intent — not bolted on later.
- Endpoints:
  - `POST /capture` — triggers `gphoto2 --capture-image-and-download`,
    saves to `~/photobooth/photos/YYYY-MM-DD/HHMMSS.jpg`. Guarded by a
    lock so overlapping triggers don't collide on the single USB camera
    (returns `409` if the lock can't be acquired within 5s). Also signals
    any in-progress `/preview` stream to stop, so a second button press
    can preempt liveview rather than queue behind it.
  - `GET /liveview/status`, `POST /liveview/start`, `POST /liveview/stop`
    (2026-11-06) — tracks whether the kiosk should be showing the live
    camera feed or its idle screen. Drives the two-stage button flow (see
    Button section) and the `/kiosk` page, which polls `/liveview/status`
    to decide what to render.
  - `GET /gallery` (2026-11-06) — browsable grid of captured photos,
    newest day first, newest photo first; links to `/photos/<date>/<file>`
    for the full-size image. No real thumbnail generation yet — the
    browser just downsizes the full JPEG via CSS, fine on the local booth
    LAN but would need actual resizing if the photo count/size grows a lot.
  - `POST /background` — placeholder for the tablet's background-selection
    input (future green-screen phase). Currently just echoes the
    selection back; no chroma-key pipeline wired up yet.
  - `GET /health` — basic liveness check. `GET /status` — photos-today
    count + last capture result, as JSON.
- `/` is an operator control panel (status, manual "Vyfotit" button,
  how-it-works, links) — not what the kiosk display shows. The kiosk's
  fullscreen guest-facing live preview lives at `/kiosk`.
- Runs on `0.0.0.0:8080`, `threaded=True` (needed once `/preview`'s
  long-lived streaming connection and other requests like `/capture` can
  be in flight at the same time) — private booth AP only, no auth (matches
  the network's closed/trusted design elsewhere in this doc).
- **Bug found & fixed (2026-11-06):** this camera's `gphoto2` can exit `0`
  even after a failed capture (observed: PTP Timeout logged to stderr, no
  file written, exit code still 0) — `/capture` now also checks the output
  file actually exists, not just the return code.
- **Storage location note:** photos currently save under
  `~/photobooth/photos/`, not the top-level `/photos/` originally
  sketched in the Storage section — avoids needing root to create/own
  a top-level directory. Structure (`YYYY-MM-DD/HHMMSS.jpg`) matches the
  original plan; move to the USB SSD mount point once that's set up.
- **Runs as a systemd `--user` service** (`~/.config/systemd/user/
  photobooth-listener.service`), not a system service — avoids needing
  root entirely. Relies on the user session existing at boot, which the
  planned kiosk auto-login already provides; enabled with
  `systemctl --user enable --now photobooth-listener.service`.
- Verified end-to-end on 2026-08-18: POSTing to `/capture` fired the
  shutter and downloaded a real photo to disk. **Currently flaky
  (2026-11-06):** the camera started timing out on every PTP command,
  including non-capture ones (`gphoto2 --summary`) — looks like a
  hardware/USB issue (loose cable, sleep state), not a listener bug; see
  camera entry in Hardware/OS section. Needs a physical check.
- ESP32 firmware POSTing to `/capture` and the new `/liveview/*` endpoints
  now written (see Button section below) but untested on real hardware.
  Still not yet done: the green-screen/chroma-key pipeline behind
  `/background`.

## Storage

- Photos saved to local disk (prefer USB SSD over SD card if applicable —
  more reliable for repeated write cycles at events).
- Suggested structure: `/photos/YYYY-MM-DD/HHMMSS.jpg` (+ RAW if desired) so
  batch upload/printing later is straightforward.
- Upload and printing are future work — not yet implemented.

## Future: green screen / background replacement

- Plan: physical green screen backdrop + classic chroma keying (OpenCV,
  color-range removal + replacement) — no ML/GPU needed for a well-lit,
  even green backdrop. Even, diffuse lighting on the backdrop matters more
  for clean keying than software cleverness.
- **Tablet with touchscreen** will be added as another client on the same
  private booth WiFi network — runs a simple local webpage for background
  selection, sends the choice to the same NUC listener service via HTTP.
- Design the NUC-side listener service to handle multiple input sources
  (button presses AND tablet background selections) from the start, rather
  than bolting the tablet on later as a separate service.

## Lighting control (for green screen phase)

- Lights (green screen backdrop lighting) need to switch on/off as part of
  the booth sequence, not stay on permanently.
- **Decided: Shelly smart plug**, on the booth's private AP network,
  controlled via its local HTTP API directly from the NUC orchestrator
  script — same self-contained pattern as the ESP32 button, no cloud/
  external dependency, small failure surface. Disable Shelly cloud
  connectivity to keep it fully local.
  - Gen1 devices: `http://<shelly-ip>/relay/0?turn=on` / `?turn=off`
  - Gen2+ devices: `http://<shelly-ip>/rpc/Switch.Set?id=0&on=true`
  - Give it a static IP or DHCP reservation on the AP so the address
    doesn't drift.
- Not yet decided: specific Shelly model/generation, light fixtures.

## Network access point

- **Decided (updated 2026-11-06): TP-Link TL-WR820N** — self-contained
  router/AP, no separate controller hardware needed. Chosen for simplicity
  in a portable, event-based deployment (travels between venues rather
  than a fixed installation). Admin UI at `http://10.0.2.1/` (login-protected).
- NUC connects to it via Ethernet; ESP32 button and (later) tablet join it
  over WiFi.

## Kiosk setup (NUC)

- **Decided: no desktop environment (no KDE/GNOME).** Debian installed with
  desktop environment tasks unchecked at `tasksel` (keep "standard system
  utilities" and "SSH server" checked). A full DE is unnecessary overhead
  and extra failure surface for a single-app unattended kiosk.
- Minimal stack instead: `xserver-xorg` + `xinit`, launching the booth app
  directly (fullscreen Python/Qt app, or Chromium in `--kiosk` mode if the
  UI is a local webpage). Add a lightweight window manager (e.g. `openbox`)
  only if the app actually needs window management.
  - Auto-login for the booth user (no login screen on boot).
  - Autostart the booth app fullscreen on login via `.xinitrc` + systemd.
  - Disable screen blanking/sleep (`xset s off`, disable suspend) so it
    doesn't go dark mid-event.

### Kiosk setup status (2026-08-18)

- **Implemented:** UI is a local webpage (the listener service's `/`
  page — currently just the live preview), so went with the Chromium
  `--kiosk` path rather than a standalone Python/Qt app.
- Installed: `xserver-xorg`, `xinit`, `openbox`, `chromium`, `unclutter`
  (hides the mouse cursor), `x11-xserver-utils` (for `xset`).
- `~/.profile` — guarded auto-`startx`, fires only when `$DISPLAY` is
  unset AND the tty is exactly `/dev/tty1` (the physical console), so
  SSH/pts sessions are never affected.
- `~/.xinitrc` — disables screen blanking/DPMS, starts `unclutter` +
  `openbox-session`, waits (up to 10s) for the listener's `/health` to
  respond so the browser doesn't launch before it's ready, then execs
  `chromium --kiosk` pointed at `http://localhost:8080/`.
- **Not yet done — needs root, couldn't run from this session (no TTY
  for the sudo password):** the `tty1` auto-login override at
  `/etc/systemd/system/getty@tty1.service.d/autologin.conf` (autologin
  as `clown` via `agetty --autologin`). Without this, `.profile`'s
  guard never fires because login still requires a manual username/
  password at the console.
- **Not yet verified on real hardware** — needs a monitor on the NUC
  and the autologin piece above; couldn't be confirmed over SSH.

## Open / not yet decided

- Printer selection and CUPS setup (planned for later, not yet started).
- Exact ESP32 board and physical button hardware.
- Whether the tablet talks HTTP, or something else, once that phase starts.
- Camera model confirmation and any gphoto2-specific quirks.

## Tooling

- Claude Code will run locally on the NUC (project directory, not home root)
  for hands-on setup, debugging, and iteration against real hardware —
  more effective than remote guidance for things like gphoto2 tethering
  quirks and kiosk config, which benefit from real error output.
