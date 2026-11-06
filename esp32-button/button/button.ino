// Target: ESP32-C3 (e.g. "ESP32-C3 + OLED" combo boards). RISC-V single core,
// different pinout/strapping than classic ESP32 — see README before flashing.
//
// Two-stage trigger: first press arms liveview on the kiosk display (so the
// guest can see themselves), second press actually takes the photo. A
// timeout disarms liveview automatically if nobody follows through.

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <esp_wifi.h>
#include "wifi_config.h" // not in git — copy from wifi_config.h.example and fill in

const char *NUC_HOST = "10.0.2.10"; // NUC's address on the booth AP — confirm it's a static/reserved lease, not DHCP-drifting
const uint16_t NUC_PORT = 8080;

// Button: one leg to this pin, other leg to GND. ESP32-C3 strapping pins are
// 2, 8, 9 — avoid those for inputs that must be stable at boot.
const uint8_t BUTTON_PIN = 4;

// Status LED: one leg to this pin (through a resistor), other to GND.
// Confirm it's free on the specific board — not all combo boards expose it
// the same way.
const uint8_t LED_PIN = 7;

// OLED wiring/size vary by the exact board bought — CONFIRM against the
// actual module's schematic/listing before flashing.
const uint8_t OLED_SDA = 5;
const uint8_t OLED_SCL = 6;
const uint8_t SCREEN_WIDTH = 72;
const uint8_t SCREEN_HEIGHT = 40; // was 128x64 — I2C init ACKs either way,
                                   // but nothing showed on-screen at 128x64,
                                   // trying the common tiny-OLED size instead
const uint8_t OLED_I2C_ADDR = 0x3C;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

const unsigned long DEBOUNCE_MS = 250;
const unsigned long WIFI_RETRY_MS = 5000;
const unsigned long WIFI_GIVE_UP_MS = 120000; // restart if disconnected this long
const unsigned long RESULT_REVERT_MS = 1500;  // how long a result message stays up
const unsigned long ARMED_TIMEOUT_MS = 20000; // auto-disarm liveview if no 2nd press

enum State { IDLE, ARMED, CAPTURING, SHOWING_RESULT };
State state = IDLE;

unsigned long lastPress = 0;
unsigned long lastWifiAttempt = 0;
unsigned long disconnectedSince = 0;
unsigned long armedAt = 0;
unsigned long resultShownAt = 0;
unsigned long lastLedToggle = 0;
bool ledState = false;
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

void onWifiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    Serial.print("WiFi disconnected, reason=");
    Serial.println(info.wifi_sta_disconnected.reason);
  }
}

String wifiDots() {
  String s = "WiFi";
  int n = (millis() / 400) % 3 + 1;
  for (int i = 0; i < n; i++) s += ".";
  return s;
}

void connectWifi() {
  WiFi.mode(WIFI_STA);

  // Default ESP-IDF channel policy can miss APs on channels 12/13, which
  // many EU routers use — widen the scan range explicitly (CZ allows 1-13).
  wifi_country_t country = {
    .cc = "CZ",
    .schan = 1,
    .nchan = 13,
    .max_tx_power = 20,
    .policy = WIFI_COUNTRY_POLICY_MANUAL,
  };
  esp_wifi_set_country(&country);

  WiFi.onEvent(onWifiEvent);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

bool postTo(const char *path) {
  HTTPClient http;
  String url = "http://" + String(NUC_HOST) + ":" + String(NUC_PORT) + path;
  http.begin(url);
  http.setTimeout(5000);
  int code = http.POST("");
  http.end();
  return code == 200;
}

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.println("\nPicollo button booting...");

  Wire.begin(OLED_SDA, OLED_SCL);
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR)) {
    Serial.println("OLED init OK");
  } else {
    Serial.println("OLED init FAILED (check OLED_SDA/OLED_SCL/OLED_I2C_ADDR)");
  }
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  drawStatus("Picollo", "Booting...");

  Serial.print("Connecting to WiFi SSID: ");
  Serial.println(WIFI_SSID);
  connectWifi();
  disconnectedSince = millis();
}

void armLiveview() {
  Serial.println("Button press 1: arming liveview...");
  drawStatus(line1, "Zapínám...");
  if (postTo("/liveview/start")) {
    state = ARMED;
    armedAt = millis();
    digitalWrite(LED_PIN, HIGH); // solid = armed, look at the screen
  } else {
    state = SHOWING_RESULT;
    line2 = "Chyba liveview";
    resultShownAt = millis();
  }
}

void disarmLiveview() {
  postTo("/liveview/stop");
  state = IDLE;
  digitalWrite(LED_PIN, LOW);
}

void triggerCapture() {
  Serial.println("Button press 2: capturing...");
  state = CAPTURING;

  HTTPClient http;
  String url = "http://" + String(NUC_HOST) + ":" + String(NUC_PORT) + "/capture";
  http.begin(url);
  http.setTimeout(10000);
  int code = http.POST("");
  http.end();
  Serial.print("Capture result code: ");
  Serial.println(code);

  if (code == 200) {
    line2 = "Vyfoceno!";
  } else if (code == 409) {
    line2 = "Obsazeno, zkus znovu";
  } else {
    line2 = "Chyba: " + String(code);
  }
  resultShownAt = millis();
  state = SHOWING_RESULT;
  digitalWrite(LED_PIN, LOW);
}

void updateLed(unsigned long now) {
  if (state == CAPTURING) {
    if (now - lastLedToggle > 100) { // fast blink while capturing
      lastLedToggle = now;
      ledState = !ledState;
      digitalWrite(LED_PIN, ledState);
    }
  } else if (state == ARMED) {
    digitalWrite(LED_PIN, HIGH);
  } else {
    digitalWrite(LED_PIN, LOW);
  }
}

void loop() {
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (disconnectedSince == 0) disconnectedSince = now;

    if (now - disconnectedSince > WIFI_GIVE_UP_MS - 3000) {
      drawStatus("Picollo", "WiFi fail");
    } else {
      drawStatus("Picollo", wifiDots());
    }
    digitalWrite(LED_PIN, (now / 500) % 2); // slow blink = disconnected

    if (now - lastWifiAttempt > WIFI_RETRY_MS) {
      lastWifiAttempt = now;
      Serial.print("WiFi not connected (status=");
      Serial.print(WiFi.status());
      Serial.println("), retrying...");
      WiFi.reconnect();
    }

    if (now - disconnectedSince > WIFI_GIVE_UP_MS) {
      Serial.println("WiFi still down after timeout, restarting...");
      ESP.restart();
    }
    return;
  }

  if (disconnectedSince != 0) {
    Serial.print("WiFi connected, IP: ");
    Serial.println(WiFi.localIP());
  }
  disconnectedSince = 0;
  updateLed(now);

  if (state == ARMED && now - armedAt > ARMED_TIMEOUT_MS) {
    disarmLiveview();
  }

  if (state == SHOWING_RESULT && now - resultShownAt > RESULT_REVERT_MS) {
    state = IDLE;
  }

  switch (state) {
    case IDLE:
      drawStatus("Picollo", "Ready " + WiFi.localIP().toString());
      break;
    case ARMED:
      drawStatus("Picollo", "Divej se!");
      break;
    case CAPTURING:
      drawStatus("Picollo", "Fotim...");
      break;
    case SHOWING_RESULT:
      drawStatus("Picollo", line2);
      break;
  }

  bool pressed = digitalRead(BUTTON_PIN) == LOW && (now - lastPress) > DEBOUNCE_MS;
  if (pressed && state == IDLE) {
    lastPress = now;
    armLiveview();
  } else if (pressed && state == ARMED) {
    lastPress = now;
    triggerCapture();
  }
}
