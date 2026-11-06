// Target: ESP32-C3 (e.g. "ESP32-C3 + OLED" combo boards). RISC-V single core,
// different pinout/strapping than classic ESP32 — see README before flashing.

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---- Fill these in before flashing ----
const char *WIFI_SSID = "CHANGE_ME";
const char *WIFI_PASSWORD = "CHANGE_ME";
const char *NUC_HOST = "10.0.2.10"; // NUC's address on the booth AP — confirm it's a static/reserved lease, not DHCP-drifting
const uint16_t NUC_PORT = 8080;
// ----------------------------------------

// Button: one leg to this pin, other leg to GND. ESP32-C3 strapping pins are
// 2, 8, 9 — avoid those for inputs that must be stable at boot.
const uint8_t BUTTON_PIN = 4;

// OLED wiring/size vary by the exact board bought — CONFIRM against the
// actual module's schematic/listing before flashing.
const uint8_t OLED_SDA = 5;
const uint8_t OLED_SCL = 6;
const uint8_t SCREEN_WIDTH = 128;
const uint8_t SCREEN_HEIGHT = 64; // many tiny "0.42in" C3+OLED boards are
                                   // actually 72x40 — check before flashing
const uint8_t OLED_I2C_ADDR = 0x3C;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

const unsigned long DEBOUNCE_MS = 250;
const unsigned long WIFI_RETRY_MS = 5000;
const unsigned long WIFI_GIVE_UP_MS = 120000; // restart if disconnected this long
const unsigned long RESULT_REVERT_MS = 1500;  // how long a result message stays up

unsigned long lastPress = 0;
unsigned long lastWifiAttempt = 0;
unsigned long disconnectedSince = 0;
unsigned long resultShownAt = 0;
bool capturing = false;
bool showingResult = false;
String line1 = "";
String line2 = "";

void drawStatus(const String &l1, const String &l2) {
  if (l1 == line1 && l2 == line2) return;
  line1 = l1;
  line2 = l2;
  display.clearDisplay();
  display.setCursor(0, 0);
  display.println(line1);
  display.println(line2);
  display.display();
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  Wire.begin(OLED_SDA, OLED_SCL);
  display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR);
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  drawStatus("Booting...", "");

  connectWifi();
  disconnectedSince = millis();
}

void triggerCapture() {
  capturing = true;
  drawStatus(line1, "Capturing...");

  HTTPClient http;
  String url = "http://" + String(NUC_HOST) + ":" + String(NUC_PORT) + "/capture";
  http.begin(url);
  http.setTimeout(10000);
  int code = http.POST("");
  http.end();

  if (code == 200) {
    drawStatus(line1, "Captured!");
  } else if (code == 409) {
    drawStatus(line1, "Busy, retry");
  } else {
    drawStatus(line1, "Failed: " + String(code));
  }
  resultShownAt = millis();
  showingResult = true;
  capturing = false;
}

void loop() {
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    drawStatus("WiFi: reconnecting", "");
    if (disconnectedSince == 0) disconnectedSince = now;

    if (now - lastWifiAttempt > WIFI_RETRY_MS) {
      lastWifiAttempt = now;
      WiFi.reconnect();
    }

    if (now - disconnectedSince > WIFI_GIVE_UP_MS) {
      ESP.restart();
    }
    return;
  }

  disconnectedSince = 0;

  if (showingResult && now - resultShownAt > RESULT_REVERT_MS) {
    showingResult = false;
  }

  if (!capturing && !showingResult) {
    drawStatus("Ready  " + WiFi.localIP().toString(), "");
  } else if (!capturing) {
    // keep the result message on screen until it reverts
    drawStatus("Ready  " + WiFi.localIP().toString(), line2);
  }

  if (!capturing && digitalRead(BUTTON_PIN) == LOW && (now - lastPress) > DEBOUNCE_MS) {
    lastPress = now;
    triggerCapture();
  }
}
