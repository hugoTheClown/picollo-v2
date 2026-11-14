// Picollo – spoušť: ESP32-C3 s integrovaným 0.42" OLED (SSD1306, 72x40, I2C) + WiFi
//
// Dvoufázové ovládání:
//   1. stisk  -> POST /liveview/start (kiosk ukáže živý obraz, LED svítí)
//   2. stisk  -> POST /capture        (vyfotí, liveview se na NUC vypne sám)
//   bez 2. stisku do ARMED_TIMEOUT_MS -> POST /liveview/stop
//
// Úlohy:
//   button_task – čte tlačítko, odfiltruje zákmity, posílá stisky do fronty
//   booth_task  – stavový automat + HTTP požadavky na NUC (blokující)
//   ui_task     – každých 100 ms překreslí OLED a nastaví LED, i když
//                 booth_task zrovna čeká na odpověď
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "wifi_secrets.h"

// ---- NUC (listener) ----
#ifndef NUC_HOST
#define NUC_HOST    "10.0.2.10"   // adresa NUC na booth AP – musí být statická/rezervovaná
#endif
#ifndef NUC_PORT
#define NUC_PORT    8080
#endif

// ---- piny ----  (C3 strapping piny 2, 8, 9 nepoužívat)
#define PIN_SDA     5
#define PIN_SCL     6
#define PIN_BUTTON  4             // tlačítko proti GND, interní pull-up
#define PIN_LED     7             // LED přes rezistor proti GND
#define OLED_ADDR   0x3C

// ---- časování ----
#define BTN_STABLE_MS       30      // jak dlouho musí být úroveň stabilní
#define BTN_LOCKOUT_MS      250     // min. odstup dvou stisků
#define ARMED_TIMEOUT_MS    20000   // auto-vypnutí liveview bez 2. stisku
#define RESULT_SHOW_MS      1500    // jak dlouho zůstane výsledek na displeji
#define HTTP_TIMEOUT_MS     5000    // /liveview/*
#define CAPTURE_TIMEOUT_MS  40000   // /capture: na NUC až 5 s zámek + 30 s gphoto2
#define WIFI_GIVE_UP_MS     120000  // restart, když WiFi nejde tak dlouho v kuse

#define OLED_W      72
#define OLED_H      40
#define OLED_PAGES  (OLED_H / 8)
#define OLED_XOFS   28   // viditelné okno 72x40 leží v RAM řadiče na sloupcích 28..99

static const char *TAG = "picollo";
static i2c_master_dev_handle_t oled;
static uint8_t fb[OLED_W * OLED_PAGES];

// ================= OLED =================

static void oled_cmds(const uint8_t *cmds, size_t n)
{
    uint8_t buf[32];
    buf[0] = 0x00; // Co=0, D/C=0 -> příkazy
    memcpy(buf + 1, cmds, n);
    ESP_ERROR_CHECK(i2c_master_transmit(oled, buf, n + 1, 100));
}

static void oled_init(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_SDA,
        .scl_io_num = PIN_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &bus));

    if (i2c_master_probe(bus, OLED_ADDR, 100) != ESP_OK) {
        ESP_LOGE(TAG, "OLED na adrese 0x%02X neodpovida (SDA=%d, SCL=%d)", OLED_ADDR, PIN_SDA, PIN_SCL);
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = OLED_ADDR,
        .scl_speed_hz = 400000,
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(bus, &dev_cfg, &oled));

    // inicializace pro SSD1306 72x40 (stejná sekvence jako u8g2 SSD1306_72X40_ER)
    static const uint8_t init[] = {
        0xAE,             // display off
        0xD5, 0x80,       // clock divide
        0xA8, 0x27,       // multiplex = 40
        0xD3, 0x00,       // display offset
        0x40,             // start line 0
        0x8D, 0x14,       // charge pump on
        0x20, 0x00,       // horizontal addressing
        0xA1,             // segment remap
        0xC8,             // COM scan dec
        0xDA, 0x12,       // COM pins
        0x81, 0xAF,       // kontrast
        0xD9, 0x22,       // precharge
        0xDB, 0x20,       // VCOMH
        0xAD, 0x30,       // interní IREF
        0xA4,             // výstup z RAM
        0xA6,             // normální (ne inverzní)
        0xAF,             // display on
    };
    oled_cmds(init, sizeof(init));
}

static void oled_flush(void)
{
    const uint8_t win[] = {
        0x21, OLED_XOFS, OLED_XOFS + OLED_W - 1, // sloupce
        0x22, 0, OLED_PAGES - 1,                 // stránky
    };
    oled_cmds(win, sizeof(win));

    uint8_t buf[1 + sizeof(fb)];
    buf[0] = 0x40; // data
    memcpy(buf + 1, fb, sizeof(fb));
    ESP_ERROR_CHECK(i2c_master_transmit(oled, buf, sizeof(buf), 100));
}

// ================= kreslení =================

static void px(int x, int y)
{
    if (x < 0 || x >= OLED_W || y < 0 || y >= OLED_H) return;
    fb[(y / 8) * OLED_W + x] |= 1 << (y & 7);
}

// Klasický font 5x7, ASCII 0x20..0x7E (sloupce, bit 0 = horní řádek).
static const uint8_t font5x7[][5] = {
    {0x00,0x00,0x00,0x00,0x00}, {0x00,0x00,0x5F,0x00,0x00}, {0x00,0x07,0x00,0x07,0x00}, // ' ' ! "
    {0x14,0x7F,0x14,0x7F,0x14}, {0x24,0x2A,0x7F,0x2A,0x12}, {0x23,0x13,0x08,0x64,0x62}, // # $ %
    {0x36,0x49,0x55,0x22,0x50}, {0x00,0x05,0x03,0x00,0x00}, {0x00,0x1C,0x22,0x41,0x00}, // & ' (
    {0x00,0x41,0x22,0x1C,0x00}, {0x14,0x08,0x3E,0x08,0x14}, {0x08,0x08,0x3E,0x08,0x08}, // ) * +
    {0x00,0x50,0x30,0x00,0x00}, {0x08,0x08,0x08,0x08,0x08}, {0x00,0x60,0x60,0x00,0x00}, // , - .
    {0x20,0x10,0x08,0x04,0x02}, {0x3E,0x51,0x49,0x45,0x3E}, {0x00,0x42,0x7F,0x40,0x00}, // / 0 1
    {0x42,0x61,0x51,0x49,0x46}, {0x21,0x41,0x45,0x4B,0x31}, {0x18,0x14,0x12,0x7F,0x10}, // 2 3 4
    {0x27,0x45,0x45,0x45,0x39}, {0x3C,0x4A,0x49,0x49,0x30}, {0x01,0x71,0x09,0x05,0x03}, // 5 6 7
    {0x36,0x49,0x49,0x49,0x36}, {0x06,0x49,0x49,0x29,0x1E}, {0x00,0x36,0x36,0x00,0x00}, // 8 9 :
    {0x00,0x56,0x36,0x00,0x00}, {0x08,0x14,0x22,0x41,0x00}, {0x14,0x14,0x14,0x14,0x14}, // ; < =
    {0x00,0x41,0x22,0x14,0x08}, {0x02,0x01,0x51,0x09,0x06}, {0x32,0x49,0x79,0x41,0x3E}, // > ? @
    {0x7E,0x11,0x11,0x11,0x7E}, {0x7F,0x49,0x49,0x49,0x36}, {0x3E,0x41,0x41,0x41,0x22}, // A B C
    {0x7F,0x41,0x41,0x22,0x1C}, {0x7F,0x49,0x49,0x49,0x41}, {0x7F,0x09,0x09,0x09,0x01}, // D E F
    {0x3E,0x41,0x49,0x49,0x7A}, {0x7F,0x08,0x08,0x08,0x7F}, {0x00,0x41,0x7F,0x41,0x00}, // G H I
    {0x20,0x40,0x41,0x3F,0x01}, {0x7F,0x08,0x14,0x22,0x41}, {0x7F,0x40,0x40,0x40,0x40}, // J K L
    {0x7F,0x02,0x0C,0x02,0x7F}, {0x7F,0x04,0x08,0x10,0x7F}, {0x3E,0x41,0x41,0x41,0x3E}, // M N O
    {0x7F,0x09,0x09,0x09,0x06}, {0x3E,0x41,0x51,0x21,0x5E}, {0x7F,0x09,0x19,0x29,0x46}, // P Q R
    {0x46,0x49,0x49,0x49,0x31}, {0x01,0x01,0x7F,0x01,0x01}, {0x3F,0x40,0x40,0x40,0x3F}, // S T U
    {0x1F,0x20,0x40,0x20,0x1F}, {0x3F,0x40,0x38,0x40,0x3F}, {0x63,0x14,0x08,0x14,0x63}, // V W X
    {0x07,0x08,0x70,0x08,0x07}, {0x61,0x51,0x49,0x45,0x43}, {0x00,0x7F,0x41,0x41,0x00}, // Y Z [
    {0x02,0x04,0x08,0x10,0x20}, {0x00,0x41,0x41,0x7F,0x00}, {0x04,0x02,0x01,0x02,0x04}, // \ ] ^
    {0x40,0x40,0x40,0x40,0x40}, {0x00,0x01,0x02,0x04,0x00}, {0x20,0x54,0x54,0x54,0x78}, // _ ` a
    {0x7F,0x48,0x44,0x44,0x38}, {0x38,0x44,0x44,0x44,0x20}, {0x38,0x44,0x44,0x48,0x7F}, // b c d
    {0x38,0x54,0x54,0x54,0x18}, {0x08,0x7E,0x09,0x01,0x02}, {0x0C,0x52,0x52,0x52,0x3E}, // e f g
    {0x7F,0x08,0x04,0x04,0x78}, {0x00,0x44,0x7D,0x40,0x00}, {0x20,0x40,0x44,0x3D,0x00}, // h i j
    {0x7F,0x10,0x28,0x44,0x00}, {0x00,0x41,0x7F,0x40,0x00}, {0x7C,0x04,0x18,0x04,0x78}, // k l m
    {0x7C,0x08,0x04,0x04,0x78}, {0x38,0x44,0x44,0x44,0x38}, {0x7C,0x14,0x14,0x14,0x08}, // n o p
    {0x08,0x14,0x14,0x18,0x7C}, {0x7C,0x08,0x04,0x04,0x08}, {0x48,0x54,0x54,0x54,0x20}, // q r s
    {0x04,0x3F,0x44,0x40,0x20}, {0x3C,0x40,0x40,0x20,0x7C}, {0x1C,0x20,0x40,0x20,0x1C}, // t u v
    {0x3C,0x40,0x30,0x40,0x3C}, {0x44,0x28,0x10,0x28,0x44}, {0x0C,0x50,0x50,0x50,0x3C}, // w x y
    {0x44,0x64,0x54,0x4C,0x44}, {0x00,0x08,0x36,0x41,0x00}, {0x00,0x00,0x7F,0x00,0x00}, // z { |
    {0x00,0x41,0x36,0x08,0x00}, {0x10,0x08,0x08,0x10,0x08},                             // } ~
};

#define NARROW_DOT '\x01' // úzká tečka pro velký text (IP adresa)
static const uint8_t narrow_dot[5] = {0x60,0x00,0x00,0x00,0x00};

static const uint8_t *glyph(char c)
{
    if (c == NARROW_DOT) return narrow_dot;
    if (c < 0x20 || c > 0x7E) c = '?';
    return font5x7[c - 0x20];
}

// Proporcionální text: prázdné sloupce glyfu se ořežou.
// sx/sy = zvětšení vodorovně/svisle, gap = mezera mezi znaky v px.
// Vrací šířku v px (při draw=false jen měří).
static int text_ex(int x, int y, const char *str, int sx, int sy, int gap, bool draw)
{
    int x0 = x;
    for (; *str; str++) {
        const uint8_t *g = glyph(*str);
        int first = 0, last = 4;
        if (*str == ' ') { first = 0; last = 2; }  // mezera = 3 sloupce
        else {
            while (first < 4 && !g[first]) first++;
            while (last > first && !g[last]) last--;
        }
        for (int cx = first; cx <= last; cx++, x += sx) {
            if (!draw) continue;
            for (int cy = 0; cy < 7; cy++)
                if (g[cx] & (1 << cy))
                    for (int dx = 0; dx < sx; dx++)
                        for (int dy = 0; dy < sy; dy++)
                            px(x + dx, y + cy * sy + dy);
        }
        if (str[1]) x += gap; // mezera mezi znaky
    }
    return x - x0;
}

static int text(int x, int y, const char *str, int s) { return text_ex(x, y, str, s, s, s, true); }
static int text_width(const char *str, int s)        { return text_ex(0, 0, str, s, s, s, false); }
static void text_center(int y, const char *str, int s)
{
    text((OLED_W - text_width(str, s)) / 2, y, str, s);
}

// Velký text (14 px vysoký) vycentrovaný; když se nevejde na šířku, zúží se.
static void big_center(int y, const char *src)
{
    // tečky nahradit úzkou variantou, ať se IP vejde v plné šířce
    char str[24];
    size_t i = 0;
    for (; src[i] && i < sizeof(str) - 1; i++) str[i] = src[i] == '.' ? NARROW_DOT : src[i];
    str[i] = '\0';

    int sx = 2;
    int w = text_ex(0, 0, str, sx, 2, 1, false);
    if (w > OLED_W) { sx = 1; w = text_ex(0, 0, str, sx, 2, 1, false); }
    text_ex((OLED_W - w) / 2, y, str, sx, 2, 1, true);
}

// ================= sdílený stav =================

typedef enum {
    ST_IDLE,       // čeká na 1. stisk
    ST_ARMING,     // posílá /liveview/start
    ST_ARMED,      // liveview běží, čeká na 2. stisk
    ST_CAPTURING,  // posílá /capture
    ST_RESULT,     // ukazuje výsledek focení / chybu
} booth_state_t;

static portMUX_TYPE state_lock = portMUX_INITIALIZER_UNLOCKED;
static booth_state_t state = ST_IDLE;   // zapisuje jen booth_task
static char state_msg[16] = "";         // text pro ST_RESULT
static TickType_t state_since = 0;      // kdy se do stavu vstoupilo

static void set_state(booth_state_t st, const char *msg)
{
    portENTER_CRITICAL(&state_lock);
    state = st;
    state_since = xTaskGetTickCount();
    strlcpy(state_msg, msg ? msg : "", sizeof(state_msg));
    portEXIT_CRITICAL(&state_lock);
}

static booth_state_t get_state(char *msg, size_t len, TickType_t *since)
{
    portENTER_CRITICAL(&state_lock);
    booth_state_t st = state;
    if (since) *since = state_since;
    if (msg) strlcpy(msg, state_msg, len);
    portEXIT_CRITICAL(&state_lock);
    return st;
}

static QueueHandle_t press_queue;

// ================= WiFi =================

static volatile bool wifi_connected = false;
static char ip_str[16] = "";

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_connected = false;
        ip_str[0] = '\0';
        wifi_event_sta_disconnected_t *ev = data;
        ESP_LOGW(TAG, "WiFi odpojena (reason %d), zkousim znovu...", ev->reason);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        snprintf(ip_str, sizeof(ip_str), IPSTR, IP2STR(&ev->ip_info.ip));
        wifi_connected = true;
        ESP_LOGI(TAG, "WiFi pripojena, IP %s", ip_str);
    }
}

static void wifi_start(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));

    // CZ = kanály 1–13; výchozí politika by mohla minout AP na 12/13
    ESP_ERROR_CHECK(esp_wifi_set_country_code("CZ", true));

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid));
    strncpy((char *)wc.sta.password, WIFI_PASSWORD, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ================= HTTP =================

// POST bez těla na NUC. Vrací HTTP status, nebo -1 při chybě spojení/timeoutu.
static int nuc_post(const char *path, int timeout_ms)
{
    char url[64];
    snprintf(url, sizeof(url), "http://%s:%d%s", NUC_HOST, NUC_PORT, path);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = timeout_ms,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;

    esp_err_t err = esp_http_client_perform(c);
    int code = err == ESP_OK ? esp_http_client_get_status_code(c) : -1;
    esp_http_client_cleanup(c);

    if (err != ESP_OK) ESP_LOGW(TAG, "POST %s selhal: %s", path, esp_err_to_name(err));
    else               ESP_LOGI(TAG, "POST %s -> %d", path, code);
    return code;
}

// ================= tlačítko =================

static void button_task(void *arg)
{
    int stable = 1, last_raw = 1;          // 1 = uvolněno (pull-up)
    TickType_t changed_at = 0, last_press = 0;
    bool first = true;

    for (;;) {
        int raw = gpio_get_level(PIN_BUTTON);
        TickType_t now = xTaskGetTickCount();

        if (raw != last_raw) {
            last_raw = raw;
            changed_at = now;
        } else if (raw != stable && now - changed_at >= pdMS_TO_TICKS(BTN_STABLE_MS)) {
            stable = raw;
            if (stable == 0 && (first || now - last_press >= pdMS_TO_TICKS(BTN_LOCKOUT_MS))) {
                first = false;
                last_press = now;
                uint8_t ev = 1;
                xQueueSend(press_queue, &ev, 0);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// ================= stavový automat =================

static void result(const char *msg)
{
    set_state(ST_RESULT, msg);
}

static void do_arm(void)
{
    ESP_LOGI(TAG, "1. stisk: zapinam liveview");
    set_state(ST_ARMING, NULL);
    int code = nuc_post("/liveview/start", HTTP_TIMEOUT_MS);
    if (code == 200) {
        set_state(ST_ARMED, NULL);
    } else {
        char msg[16];
        snprintf(msg, sizeof(msg), code < 0 ? "Bez NUC" : "Chyba %d", code);
        result(msg);
    }
}

static void do_capture(void)
{
    ESP_LOGI(TAG, "2. stisk: fotim");
    set_state(ST_CAPTURING, NULL);
    int code = nuc_post("/capture", CAPTURE_TIMEOUT_MS);
    if (code == 200)       result("Vyfoceno!");
    else if (code == 409)  result("Obsazeno");
    else if (code < 0)     result("Bez NUC");
    else {
        char msg[16];
        snprintf(msg, sizeof(msg), "Chyba %d", code);
        result(msg);
    }
}

static void booth_task(void *arg)
{
    for (;;) {
        TickType_t entered;
        booth_state_t st = get_state(NULL, 0, &entered);

        // ARMED a RESULT mají časový limit – na stisk čekáme jen do něj
        TickType_t wait = portMAX_DELAY;
        TickType_t limit = st == ST_ARMED  ? pdMS_TO_TICKS(ARMED_TIMEOUT_MS)
                         : st == ST_RESULT ? pdMS_TO_TICKS(RESULT_SHOW_MS) : 0;
        if (limit) {
            TickType_t elapsed = xTaskGetTickCount() - entered;
            wait = elapsed >= limit ? 0 : limit - elapsed;
        }

        uint8_t ev;
        if (xQueueReceive(press_queue, &ev, wait) != pdTRUE) {
            // vypršel limit
            if (st == ST_ARMED) {
                ESP_LOGI(TAG, "Nikdo nezmackl podruhe, vypinam liveview");
                nuc_post("/liveview/stop", HTTP_TIMEOUT_MS);
            }
            set_state(ST_IDLE, NULL);
            continue;
        }

        if (!wifi_connected) {
            ESP_LOGW(TAG, "Stisk ignorovan – WiFi neni pripojena");
            continue;
        }

        switch (st) {
        case ST_IDLE:
        case ST_RESULT:   do_arm();     break;  // z výsledku lze rovnou začít znovu
        case ST_ARMED:    do_capture(); break;
        default:          break;
        }

        // stisky během čekání na NUC zahodit, ať se nevykonají dodatečně
        xQueueReset(press_queue);
    }
}

// ================= displej + LED =================

static void ui_task(void *arg)
{
    static const char *const dots[] = { ".", "..", "...", "" };
    TickType_t down_since = xTaskGetTickCount();
    char msg[16];

    for (unsigned tick = 0;; tick++) {
        TickType_t now = xTaskGetTickCount();
        bool online = wifi_connected;
        booth_state_t st = get_state(msg, sizeof(msg), NULL);

        // příliš dlouho bez WiFi -> restart (pomůže i při zaseknutém WiFi stacku)
        if (online) down_since = now;
        else if (now - down_since > pdMS_TO_TICKS(WIFI_GIVE_UP_MS)) {
            ESP_LOGE(TAG, "WiFi nejde %d s, restartuji", WIFI_GIVE_UP_MS / 1000);
            esp_restart();
        }

        // LED: bez WiFi pomalu bliká, ARMED svítí, při focení rychle bliká
        int led = 0;
        if (!online)                                     led = (tick / 5) & 1;
        else if (st == ST_ARMED)                         led = 1;
        else if (st == ST_ARMING || st == ST_CAPTURING)  led = tick & 1;
        gpio_set_level(PIN_LED, led);

        memset(fb, 0, sizeof(fb));
        text_center(1, "Picollo", 1);

        if (!online) {
            // "WiFi" + tečky naskakují postupně (1, 2, 3, žádná); šířka se počítá
            // se všemi třemi, aby nápis neposkakoval
            int w = text_width("WiFi...", 1);
            int x = (OLED_W - w) / 2;
            x += text(x, 11, "WiFi", 1) + 1;
            text(x, 11, dots[(tick / 5) % 4], 1);
        } else {
            switch (st) {
            case ST_IDLE:
                text_center(11, "WiFi OK", 1);
                big_center(23, ip_str);
                break;
            case ST_ARMING:
                text_center(11, "Liveview", 1);
                big_center(23, "...");
                break;
            case ST_ARMED:
                text_center(11, "Zmackni znovu", 1);
                big_center(23, "Divej se!");
                break;
            case ST_CAPTURING:
                big_center(17, "Fotim...");
                break;
            case ST_RESULT:
                big_center(17, msg);
                break;
            }
        }

        oled_flush();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

// ================= main =================

void app_main(void)
{
    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn));

    gpio_config_t led = {
        .pin_bit_mask = 1ULL << PIN_LED,
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&led));
    gpio_set_level(PIN_LED, 0);

    press_queue = xQueueCreate(4, sizeof(uint8_t));

    oled_init();
    wifi_start();

    ESP_LOGI(TAG, "NUC: http://%s:%d", NUC_HOST, NUC_PORT);

    xTaskCreate(ui_task,     "ui",     4096, NULL, 4, NULL);
    xTaskCreate(button_task, "button", 2048, NULL, 6, NULL);
    xTaskCreate(booth_task,  "booth",  6144, NULL, 5, NULL);
}
