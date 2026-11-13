// Picollo – ESP32-C3 s integrovaným 0.42" OLED (SSD1306, 72x40, I2C) + WiFi
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "wifi_secrets.h"

#define PIN_SDA     5
#define PIN_SCL     6
#define OLED_ADDR   0x3C

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

// minimální font 5x7 – jen znaky, které potřebujeme
typedef struct { char c; uint8_t col[5]; } glyph_t;
static const glyph_t font[] = {
    {' ', {0x00,0x00,0x00,0x00,0x00}}, {'.', {0x00,0x60,0x60,0x00,0x00}},
    {'\x01', {0x60,0x00,0x00,0x00,0x00}}, // úzká tečka pro velký text
    {'0', {0x3E,0x51,0x49,0x45,0x3E}}, {'1', {0x00,0x42,0x7F,0x40,0x00}},
    {'2', {0x42,0x61,0x51,0x49,0x46}}, {'3', {0x21,0x41,0x45,0x4B,0x31}},
    {'4', {0x18,0x14,0x12,0x7F,0x10}}, {'5', {0x27,0x45,0x45,0x45,0x39}},
    {'6', {0x3C,0x4A,0x49,0x49,0x30}}, {'7', {0x01,0x71,0x09,0x05,0x03}},
    {'8', {0x36,0x49,0x49,0x49,0x36}}, {'9', {0x06,0x49,0x49,0x29,0x1E}},
    {'F', {0x7F,0x09,0x09,0x09,0x01}}, {'K', {0x7F,0x08,0x14,0x22,0x41}},
    {'O', {0x3E,0x41,0x41,0x41,0x3E}}, {'P', {0x7F,0x09,0x09,0x09,0x06}},
    {'W', {0x3F,0x40,0x38,0x40,0x3F}}, {'c', {0x38,0x44,0x44,0x44,0x20}},
    {'i', {0x00,0x44,0x7D,0x40,0x00}}, {'l', {0x00,0x41,0x7F,0x40,0x00}},
    {'o', {0x38,0x44,0x44,0x44,0x38}},
};

static const uint8_t *glyph(char c)
{
    for (size_t i = 0; i < sizeof(font) / sizeof(font[0]); i++)
        if (font[i].c == c) return font[i].col;
    return font[0].col;
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
    for (; src[i] && i < sizeof(str) - 1; i++) str[i] = src[i] == '.' ? '\x01' : src[i];
    str[i] = '\0';

    int sx = 2;
    int w = text_ex(0, 0, str, sx, 2, 1, false);
    if (w > OLED_W) { sx = 1; w = text_ex(0, 0, str, sx, 2, 1, false); }
    text_ex((OLED_W - w) / 2, y, str, sx, 2, 1, true);
}

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

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid));
    strncpy((char *)wc.sta.password, WIFI_PASSWORD, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
}

// ================= main =================

void app_main(void)
{
    oled_init();
    wifi_start();

    static const char *const dots[] = { ".", "..", "...", "" };
    for (unsigned phase = 0;; phase = (phase + 1) % 4) {
        memset(fb, 0, sizeof(fb));
        text_center(1, "Picollo", 1);

        if (wifi_connected) {
            text_center(11, "WiFi OK", 1);
            big_center(23, ip_str);
        } else {
            // "WiFi" + tečky naskakují postupně (1, 2, 3, žádná); šířka se počítá
            // se všemi třemi, aby nápis neposkakoval
            int w = text_width("WiFi...", 1);
            int x = (OLED_W - w) / 2;
            x += text(x, 11, "WiFi", 1) + 1;
            text(x, 11, dots[phase], 1);
        }

        oled_flush();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}
