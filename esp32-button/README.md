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

**WiFi credentials** (not in git — the password shouldn't live in history):

```sh
cp button/wifi_config.h.example button/wifi_config.h
# then edit button/wifi_config.h with the real SSID/password
```

**Copy the SSID exactly from the router's admin page, not from memory —
SSIDs are case-sensitive, and a case mismatch produces a confusing
"NO_AP_FOUND" (reason 201) that looks like a range/channel problem.**

Edit the top of `button/button.ino` for everything else:

- `NUC_HOST` — the NUC's IP address on that AP (static/reserved, not DHCP-drifting).
- `BUTTON_PIN` (default GPIO4) and `LED_PIN` (default GPIO7) — fine for most
  C3 boards, but confirm they're free on the specific module (OLED wiring
  sometimes claims nearby pins).
- `OLED_SDA` / `OLED_SCL` and `SCREEN_WIDTH` / `SCREEN_HEIGHT` — **must
  confirm against the exact board bought**. Currently set to 72x40 (GPIO5/6
  I2C pins confirmed correct on real hardware, but 72x40 renders garbled —
  see Status below, still unresolved).

## Wiring

- Button: one leg to `BUTTON_PIN`, other leg to GND. Firmware uses
  `INPUT_PULLUP`, so no external resistor needed.
- Debounce capacitor: ~100 nF ceramic, wired in parallel across the
  button's two legs. The firmware already does a 250ms software debounce,
  so this is extra insurance against contact bounce/electrical noise on a
  button that'll get mashed by strangers all day — not strictly required,
  but cheap and worth it.
- LED: `LED_PIN` → series resistor (~220–330 Ω) → LED → GND.
- OLED: I2C (SDA/SCL as configured above) — likely already wired on-board
  if it's an integrated combo module.
- Power: USB or a 5V line to the ESP32 at the button stand (see
  `PROJECT.md`).
- ESP32-C3 strapping pins are **2, 8, 9** — avoid reusing these for the
  button, LED, or anything that must read stable at boot.

### Trigger wiring diagram

![Trigger wiring](wiring.jpg)

The button and LED are the only things that need wiring by hand on most
combo boards — the OLED is normally already soldered to the same PCB.
(Diagram source: `generate_wiring_diagram.py`, not part of the firmware —
rerun it if the pinout above changes.)

## Flashing

### From the NUC (`flash.py`) — if the board is plugged into the NUC itself

```sh
python3 flash.py            # auto-detects /dev/ttyACM*/ttyUSB*, compiles, uploads
python3 flash.py --watch    # same, then streams serial output for 10s after
```

Needs `arduino-cli` (`~/.local/bin/arduino-cli`), the `esp32:esp32` core,
and the two libraries below — one-time setup:

```sh
arduino-cli core install esp32:esp32
arduino-cli lib install "Adafruit SSD1306" "Adafruit GFX Library"
```

Serial-port access needs the `dialout` group (`sudo usermod -aG dialout
$USER`, then log out and back in — or prefix commands with `sg dialout -c
'...'` to pick it up immediately without a relogin).

`flash.py` always builds with `CDCOnBoot=cdc` — **required** on the C3 for
`Serial.print()` to actually reach the USB port; without it the firmware's
own debug output silently goes nowhere (confirmed on real hardware: only
ESP-IDF's internal Wi-Fi log lines showed up until this was set).

### From the Arduino IDE (a separate machine with a GUI)

1. Boards Manager → install **esp32** (Espressif Systems). Library Manager
   → install **Adafruit SSD1306** and **Adafruit GFX Library**.
2. Board: "ESP32C3 Dev Module" (or the board's specific variant if listed).
   **Tools → USB CDC On Boot → Enabled** (see note above).
3. Open `button/button.ino`, fill in the config, select the port, upload.

Either way, watch the display/LED through the sequence in "How the trigger
works" above; `flash.py --watch` (or `arduino-cli monitor -p <port> -c
baudrate=115200`) also shows boot/Wi-Fi/capture logging over serial.

## Status

**Flashed, booted, and on Wi-Fi on real hardware (2026-11-06):** compiles,
uploads, and connects via `flash.py` from the NUC. Confirmed on serial:
`OLED init OK` (I2C pins/address correct) and `WiFi connected, IP:
10.0.2.101`. Getting Wi-Fi working took three real fixes — see the dated
log in `../PROJECT.md` (disconnect-reason event logging, explicit country
code for the EU channel range, and a case-sensitive SSID typo as the
actual root cause).

**Still open:**
- OLED renders at 72x40 but garbled/offset (e.g. `"Picollo"` showed as
  `"Lo    ectir"`) — likely needs a column-offset fix for this specific
  cheap-clone panel; not yet solved.
- Button/LED/capacitor wiring not confirmed tested — no `/liveview/start`
  or `/capture` request has been seen from the board in the NUC listener
  logs yet, so unclear if the button itself is wired per `wiring.jpg`.
