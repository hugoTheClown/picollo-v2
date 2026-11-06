"""One-off script that generated wiring.jpg. Rerun if the pinout changes."""

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 780
BG = (20, 20, 24)
BOARD = (40, 70, 110)
BOARD_EDGE = (90, 140, 200)
WIRE = (220, 190, 60)
GND_WIRE = (150, 150, 150)
TEXT = (235, 235, 235)
DIM = (170, 170, 170)
LED_COLOR = (225, 80, 80)
BTN_COLOR = (230, 230, 230)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def font(size, bold=False):
    path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


title_f = font(26, bold=True)
label_f = font(17)
small_f = font(14)

d.text((24, 18), "Picollo — zapojení spouště (ESP32-C3)", font=title_f, fill=TEXT)

# ---- Board ----
bx, by, bw, bh = 230, 110, 260, 450
d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=14, fill=BOARD, outline=BOARD_EDGE, width=3)
d.text((bx + bw / 2, by + 34), "ESP32-C3", font=font(20, bold=True), fill=TEXT, anchor="mm")
d.text((bx + bw / 2, by + 60), "+ OLED combo board", font=small_f, fill=DIM, anchor="mm")
d.text(
    (bx + bw / 2, by + 100),
    "(OLED napevno zapájený\nna desce, zapojuje se\njen tlačítko + LED)",
    font=small_f,
    fill=DIM,
    anchor="mm",
    align="center",
)

y_5v = by + 170
y_gnd = by + 220
y_btn = by + 290
y_led = by + 410

# Pin dots + inline labels at board's right edge
for name, y in [("5V", y_5v), ("GND", y_gnd), ("GPIO4", y_btn), ("GPIO7", y_led)]:
    d.ellipse([bx + bw - 4, y - 4, bx + bw + 4, y + 4], fill=BOARD_EDGE)

# ---- Power in (left side) ----
d.text((20, y_5v), "USB-C / 5V zdroj", font=label_f, fill=TEXT, anchor="lm")
d.line([200, y_5v, bx, y_5v], fill=WIRE, width=3)

# ---- Shared GND rail (right side) ----
rail_x = 900
d.line([bx + bw, y_gnd, rail_x, y_gnd], fill=GND_WIRE, width=3)
d.line([rail_x, y_gnd, rail_x, y_led], fill=GND_WIRE, width=3)
d.text((rail_x + 12, y_gnd), "GND", font=small_f, fill=DIM, anchor="lm")

# ---- Button branch ----
btn_x = 650
d.line([bx + bw, y_btn, btn_x - 30, y_btn], fill=WIRE, width=3)
d.rectangle([btn_x - 30, y_btn - 22, btn_x + 30, y_btn + 22], outline=BTN_COLOR, width=3)
d.ellipse([btn_x - 9, y_btn - 9, btn_x + 9, y_btn + 9], fill=BTN_COLOR)
d.text((btn_x, y_btn - 42), "TLAČÍTKO (GPIO4)", font=label_f, fill=TEXT, anchor="mm")
d.text((btn_x, y_btn + 42), "momentary, NO", font=small_f, fill=DIM, anchor="mm")
d.line([btn_x + 30, y_btn, rail_x, y_btn], fill=WIRE, width=3)

# ---- LED branch ----
led_x = 650
d.line([bx + bw, y_led, led_x - 70, y_led], fill=WIRE, width=3)
d.rectangle([led_x - 70, y_led - 10, led_x - 40, y_led + 10], outline=DIM, width=2)
d.text((led_x - 55, y_led - 28), "R ~ 220–330 Ω", font=small_f, fill=DIM, anchor="mm")
d.line([led_x - 40, y_led, led_x - 25, y_led], fill=WIRE, width=3)
d.ellipse([led_x - 25, y_led - 15, led_x + 5, y_led + 15], outline=LED_COLOR, width=3)
d.line([led_x - 18, y_led - 8, led_x - 2, y_led + 8], fill=LED_COLOR, width=2)
d.line([led_x - 2, y_led - 8, led_x - 18, y_led + 8], fill=LED_COLOR, width=2)
d.text((led_x - 10, y_led + 32), "LED (GPIO7)", font=label_f, fill=TEXT, anchor="mm")
d.line([led_x + 5, y_led, rail_x, y_led], fill=WIRE, width=3)

# ---- Legend ----
ly = 600
d.text((24, ly), "Legenda", font=font(18, bold=True), fill=TEXT)
d.line([24, ly + 34, 64, ly + 34], fill=WIRE, width=3)
d.text((74, ly + 34), "signál / napájení", font=small_f, fill=DIM, anchor="lm")
d.line([24, ly + 60, 64, ly + 60], fill=GND_WIRE, width=3)
d.text((74, ly + 60), "GND (zem, sdílená pro tlačítko i LED)", font=small_f, fill=DIM, anchor="lm")

lines = [
    "Tlačítko: GPIO4 ↔ GND. Firmware používá INPUT_PULLUP — žádný externí rezistor není potřeba.",
    "LED: GPIO7 → rezistor (~220–330 Ω) → LED → GND.",
    "Piny (GPIO4, GPIO7, I2C pro OLED) potvrď podle konkrétní koupené desky — viz esp32-button/README.md.",
]
for i, line in enumerate(lines):
    d.text((24, ly + 96 + i * 26), line, font=small_f, fill=DIM)

img.save("wiring.jpg", quality=92)
print("Saved wiring.jpg")
