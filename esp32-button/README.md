# ESP32-C3 button firmware

Sends `POST /capture` to the NUC listener when the physical button is pressed.
Target board: **ESP32-C3 with onboard OLED** (provisional choice — "zatím").
See `../PROJECT.md` for the overall design.

## Before flashing

Edit the top of `button.ino`:

- `WIFI_SSID` / `WIFI_PASSWORD` — the booth's private AP credentials.
- `NUC_HOST` — the NUC's IP address on that AP (static/reserved, not DHCP-drifting).
- `BUTTON_PIN` (default GPIO4) — fine for most C3 boards, but confirm it's
  free on the specific module (OLED wiring sometimes claims nearby pins).
- `OLED_SDA` / `OLED_SCL` and `SCREEN_WIDTH` / `SCREEN_HEIGHT` — **must
  confirm against the exact board bought**. These are guesses (GPIO5/6,
  128x64). Many tiny "ESP32-C3 + 0.42in OLED" combo boards are actually
  72x40 pixels with display pins hardwired on the PCB, not the display's
  I2C address/size varying by vendor — check the product page/schematic.

## Why no separate status LED

The original plan was an onboard LED for WiFi-connected status. With an
OLED now in the picture, the display shows richer status instead (WiFi
state + IP, capture result) — the LED requirement is superseded, not
dropped.

## Wiring

- Button: one leg to `BUTTON_PIN`, other leg to GND. Firmware uses
  `INPUT_PULLUP`, so no external resistor needed.
- OLED: I2C (SDA/SCL as configured above) — likely already wired on-board
  if it's an integrated combo module.
- Power: USB or a 5V line to the ESP32 at the button stand (see
  `PROJECT.md`).
- ESP32-C3 strapping pins are **2, 8, 9** — avoid reusing these for the
  button or anything that must read stable at boot.

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
3. Watch the display: `Booting...` → `WiFi: reconnecting` (while
   joining) → `Ready <ip>` (idle) → `Capturing...` → `Captured!` /
   `Busy, retry` / `Failed: <code>` (reverts to `Ready` after ~1.5s).

## Status

Implemented but **untested on real hardware** — board not yet in hand,
and the OLED pinout/resolution values are unconfirmed placeholders.
