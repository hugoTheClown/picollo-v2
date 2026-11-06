# ESP32-C3 button firmware

Target board: **ESP32-C3 with onboard OLED** (provisional choice — "zatím").
See `../PROJECT.md` for the overall design.

## How the trigger works (two-stage)

1. **First press** → arms liveview: POSTs `/liveview/start` to the NUC, so
   the kiosk display switches from its idle screen to a live camera feed
   (guest sees themselves). OLED shows `Divej se!`, status LED turns on solid.
2. **Second press** → POSTs `/capture`, which takes the actual photo (the
   NUC also stops liveview automatically as part of handling this). OLED
   shows `Fotim...` then the result (`Vyfoceno!` / `Obsazeno, zkus znovu` /
   `Chyba: <code>`), LED blinks fast during the shot then turns off.
3. If nobody follows through with a second press within ~20s, the armed
   state times out automatically (`/liveview/stop`), kiosk reverts to idle,
   LED turns off.

## Before flashing

Edit the top of `button.ino`:

- `WIFI_SSID` / `WIFI_PASSWORD` — the booth's private AP credentials.
- `NUC_HOST` — the NUC's IP address on that AP (static/reserved, not DHCP-drifting).
- `BUTTON_PIN` (default GPIO4) and `LED_PIN` (default GPIO7) — fine for most
  C3 boards, but confirm they're free on the specific module (OLED wiring
  sometimes claims nearby pins).
- `OLED_SDA` / `OLED_SCL` and `SCREEN_WIDTH` / `SCREEN_HEIGHT` — **must
  confirm against the exact board bought**. These are guesses (GPIO5/6,
  128x64). Many tiny "ESP32-C3 + 0.42in OLED" combo boards are actually
  72x40 pixels with display pins hardwired on the PCB, not the display's
  I2C address/size varying by vendor — check the product page/schematic.

## Wiring

- Button: one leg to `BUTTON_PIN`, other leg to GND. Firmware uses
  `INPUT_PULLUP`, so no external resistor needed.
- LED: `LED_PIN` → series resistor (~220–330 Ω) → LED → GND.
- OLED: I2C (SDA/SCL as configured above) — likely already wired on-board
  if it's an integrated combo module.
- Power: USB or a 5V line to the ESP32 at the button stand (see
  `PROJECT.md`).
- ESP32-C3 strapping pins are **2, 8, 9** — avoid reusing these for the
  button, LED, or anything that must read stable at boot.

### Trigger wiring diagram

![Zapojení spouště](wiring.jpg)

The button and LED are the only things that need wiring by hand on most
combo boards — the OLED is normally already soldered to the same PCB.
(Diagram source: `generate_wiring_diagram.py`, not part of the firmware —
rerun it if the pinout above changes.)

## Flashing

Needs the Arduino IDE (or `arduino-cli`) with the ESP32 board package
installed, targeting "ESP32C3 Dev Module" (or the board's specific variant
if listed). Not done from this NUC session — no ESP32-C3 attached here and
no Arduino toolchain installed.

Library Manager dependencies: **Adafruit SSD1306** and **Adafruit GFX
Library**.

Board-settings gotcha specific to C3: if Serial output over the native USB
port looks empty, enable "USB CDC On Boot" in Tools → USB CDC On Boot.

1. Install the ESP32 core in Arduino IDE (Boards Manager → "esp32" by
   Espressif Systems) and the two libraries above.
2. Select the correct board/port, open `button.ino`, fill in the config
   (including the OLED values above), upload.
3. Watch the display/LED through the sequence in "How the trigger works"
   above.

## Status

Implemented but **untested on real hardware** — board not yet in hand, and
the OLED pinout/resolution values are unconfirmed placeholders.
