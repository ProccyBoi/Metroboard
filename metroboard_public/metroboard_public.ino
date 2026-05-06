#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_ota_ops.h>
#include <time.h>

// =========================================
// ===== Optional defaults (leave blank) ===
// =========================================
#define DEFAULT_WIFI_SSID ""
#define DEFAULT_WIFI_PASS ""
#define DEFAULT_BOARD_ID ""
// =========================================

static const char *kPayloadBase =
    "https://damp-catlin-metroboard-7be2a3b3.koyeb.app/board_payload";

static const char *kFirmwareVersion = "1.4.2";
static const char *kBuildLabel = "ORIGINAL+debuglogs";

static const uint32_t POLL_MS_MIN = 1000, POLL_MS_MAX = 5000;
static const uint32_t SETTINGS_FETCH_MS = 30000;
static const uint32_t OTA_CHECK_MS = 15UL * 60UL * 1000UL;
static const uint32_t OTA_STARTUP_DELAY_MS = 60UL * 1000UL;
static const uint32_t DEBUG_PRINT_MS = 5000;

struct DeviceConfig {
  String ssid;
  String pass;
  String boardId;

  bool isValid() const { return ssid.length() > 0 && boardId.length() > 0; }
};

Preferences gPrefs;
DeviceConfig gConfig = {DEFAULT_WIFI_SSID, DEFAULT_WIFI_PASS, DEFAULT_BOARD_ID};
WebServer gConfigServer(80);
DNSServer gDnsServer;

// ===== LEDs =====
#define STATUS_PIN 25
uint8_t gBaseBrightness = 16;
uint8_t gEffectiveBrightness = 16;
static const uint8_t kStatusBrightness = 16;
bool gBoardIdValid = false;

enum Line : uint8_t {
  L_T9, L_T5, L_T2, L_T6, L_T3, L_T4, L_T7, L_T8, L_M1, L_T1, LINE_COUNT
};
const uint8_t STRIP_PINS[LINE_COUNT] = {4, 13, 14, 16, 17, 18, 19, 21, 22, 23};
const uint16_t STRIP_LEN[LINE_COUNT] = {31, 31, 38, 6, 30, 33, 2, 38, 31, 56};
bool REVERSE[LINE_COUNT] = {0, 0, 0, 0, 0, 0, 1, 0, 0, 0};

const uint32_t LINE_COLOR_HEX[LINE_COUNT] = {
    0xE53935, 0xD81B60, 0x00ACC1, 0xA0522D, 0xFB8C00,
    0x0D47A1, 0x9E9E9E, 0x006400, 0x26C6DA, 0xF39C12};

Adafruit_NeoPixel strips[LINE_COUNT] = {
    Adafruit_NeoPixel(STRIP_LEN[0], STRIP_PINS[0], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[1], STRIP_PINS[1], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[2], STRIP_PINS[2], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[3], STRIP_PINS[3], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[4], STRIP_PINS[4], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[5], STRIP_PINS[5], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[6], STRIP_PINS[6], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[7], STRIP_PINS[7], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[8], STRIP_PINS[8], NEO_GRB + NEO_KHZ800),
    Adafruit_NeoPixel(STRIP_LEN[9], STRIP_PINS[9], NEO_GRB + NEO_KHZ800)};

Adafruit_NeoPixel statusStrip(1, STATUS_PIN, NEO_GRB + NEO_KHZ800);
DynamicJsonDocument gDoc(8192);

String gMode = "live";

struct NightModeCfg {
  bool enabled; bool active; String start; String end; String action; uint8_t dimLevel;
} gNightMode = {false, false, "23:00", "06:00", "off", 20};

struct AnimCfg {
  String name; int speed; uint8_t r, g, b;
} gAnim = {"pulse", 120, 255, 80, 0};

uint32_t lastSettingsFetch = 0;
uint32_t lastSettingsVersion = 0;
uint32_t lastOtaCheck = 0;
volatile bool gOtaInProgress = false;

// OTA telemetry (last attempt)
String gLastOtaResult = "none";
String gLastOtaTargetVersion = "";
uint32_t gLastOtaAttemptMs = 0;

// Forward decls for poll/blink globals used in debug print
uint32_t lastPoll = 0, pollInterval = POLL_MS_MIN;

// ===== Binding model =====
struct LedBinding {
  const char *station;
  Line line;
  uint16_t led;
};
#define MAX_BINDINGS 500
LedBinding B[MAX_BINDINGS];
size_t NB = 0;

uint8_t *stateBuf = nullptr;
uint8_t *ttlBuf = nullptr;
static const uint8_t GRACE_POLLS = 3;

// ===== status enum =====
enum StatusCode : uint8_t {
  STATUS_POWER_ON,
  STATUS_WIFI_CONNECTED_VALID,
  STATUS_SERVER_ACTIVE,
  STATUS_WIFI_CONNECTING,
  STATUS_WIFI_DISCONNECTED,
  STATUS_BOARD_INVALID,
  STATUS_OTA_UPDATING
};

void setStatus(StatusCode code) {
  uint8_t r = 0, g = 0, b = 0;
  switch (code) {
    case STATUS_POWER_ON:             r=160; g=160; b=160; break;
    case STATUS_WIFI_CONNECTED_VALID: r=0;   g=160; b=0;   break;
    case STATUS_SERVER_ACTIVE:        r=80;  g=180; b=255; break;
    case STATUS_WIFI_CONNECTING:      r=200; g=80;  b=0;   break;
    case STATUS_WIFI_DISCONNECTED:    r=200; g=0;   b=0;   break;
    case STATUS_BOARD_INVALID:        r=200; g=0;   b=120; break;
    case STATUS_OTA_UPDATING:         r=180; g=180; b=40;  break;
  }
  statusStrip.setBrightness(kStatusBrightness);
  statusStrip.setPixelColor(0, statusStrip.Color(r, g, b));
  statusStrip.show();
}

// ===== Debug status print =====
void printDebugStatus() {
  uint32_t now = millis();
  uint32_t uptime_s = now / 1000;

  Serial.println();
  Serial.println("======== DEBUG STATUS ========");
  Serial.printf("build    : %s\n", kBuildLabel);
  Serial.printf("fw ver   : %s\n", kFirmwareVersion);
  Serial.printf("uptime   : %lu s\n", (unsigned long)uptime_s);
  Serial.printf("heap     : free=%u min=%u\n",
                ESP.getFreeHeap(), ESP.getMinFreeHeap());

  const esp_partition_t *running = esp_ota_get_running_partition();
  const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
  if (running) {
    Serial.printf("part run : %s @ 0x%06x size=%u\n",
                  running->label, running->address, running->size);
  }
  if (next) {
    Serial.printf("part next: %s @ 0x%06x size=%u\n",
                  next->label, next->address, next->size);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("wifi     : connected ssid=%s ip=%s rssi=%d\n",
                  WiFi.SSID().c_str(),
                  WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
  } else {
    Serial.printf("wifi     : DISCONNECTED status=%d\n", (int)WiFi.status());
  }
  Serial.printf("mac      : %s\n", WiFi.macAddress().c_str());
  Serial.printf("board    : id=\"%s\" validated=%d\n",
                gConfig.boardId.c_str(), gBoardIdValid);
  Serial.printf("mode     : %s bright=%u eff=%u\n",
                gMode.c_str(), gBaseBrightness, gEffectiveBrightness);
  Serial.printf("anim     : %s speed=%d rgb=(%u,%u,%u)\n",
                gAnim.name.c_str(), gAnim.speed, gAnim.r, gAnim.g, gAnim.b);

  uint32_t sinceLastPoll = (lastPoll > 0) ? (now - lastPoll) : 0;
  Serial.printf("poll     : interval=%ums sinceLast=%ums\n",
                (unsigned)pollInterval, (unsigned)sinceLastPoll);

  uint32_t sinceLastOta = (lastOtaCheck > 0) ? (now - lastOtaCheck) : 0;
  long untilNextOta;
  if (now < OTA_STARTUP_DELAY_MS) {
    untilNextOta = (long)(OTA_STARTUP_DELAY_MS - now);
  } else if (lastOtaCheck == 0) {
    untilNextOta = 0;
  } else {
    untilNextOta = (long)OTA_CHECK_MS - (long)sinceLastOta;
    if (untilNextOta < 0) untilNextOta = 0;
  }
  Serial.printf("ota      : inProgress=%d sinceLastCheck=%ums nextIn=%ldms\n",
                gOtaInProgress, (unsigned)sinceLastOta, untilNextOta);
  Serial.printf("ota last : result=\"%s\" target=\"%s\" attemptMs=%u\n",
                gLastOtaResult.c_str(), gLastOtaTargetVersion.c_str(),
                (unsigned)gLastOtaAttemptMs);
  Serial.println("==============================");
  Serial.println();
}

// ===== misc helpers =====
static const char *routeSuffix(Line ln) {
  switch (ln) {
    case L_T9: return "9"; case L_T5: return "5"; case L_T2: return "2";
    case L_T6: return "6"; case L_T3: return "3"; case L_T4: return "4";
    case L_T7: return "7"; case L_T8: return "8"; case L_M1: return "M";
    case L_T1: return "1"; default:    return "?";
  }
}
static char *makeKey(const char *base, Line ln) {
  const char *suf = routeSuffix(ln);
  size_t lb = strlen(base), ls = strlen(suf);
  char *out = (char *)malloc(lb + ls + 1);
  if (!out) return nullptr;
  memcpy(out, base, lb);
  memcpy(out + lb, suf, ls);
  out[lb + ls] = '\0';
  return out;
}
static inline uint16_t physIdx(Line ln, uint16_t led) {
  return REVERSE[ln] ? uint16_t(STRIP_LEN[ln] - 1 - led) : led;
}
void addBind(const char *base, Line ln, uint16_t led) {
  if (NB >= MAX_BINDINGS) return;
  uint16_t idx = physIdx(ln, led);
  if (idx >= STRIP_LEN[ln]) return;
  char *key = makeKey(base, ln);
  if (!key) return;
  B[NB++] = {key, ln, idx};
}
void bindSeq(Line ln, const char *const *seq, size_t n, uint16_t start = 0) {
  for (size_t i = 0; i < n; i++) addBind(seq[i], ln, start + (uint16_t)i);
}
static inline bool unres(char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
         (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~';
}
static String enc(const char *s) {
  String o;
  while (*s) {
    char c = *s++;
    if (unres(c)) o += c;
    else { char b[4]; snprintf(b, sizeof(b), "%%%02X", (unsigned char)c); o += b; }
  }
  return o;
}
static String serviceBaseUrl() {
  String url = String(kPayloadBase);
  int slash = url.lastIndexOf('/');
  if (slash > 0) return url.substring(0, slash);
  return url;
}
static uint32_t lineColor(Line ln) {
  uint32_t h = LINE_COLOR_HEX[ln];
  uint8_t r = (h >> 16) & 0xFF, g = (h >> 8) & 0xFF, b = h & 0xFF;
  return strips[ln].Color(r, g, b);
}
static uint32_t wheel(uint8_t pos) {
  pos = 255 - pos;
  if (pos < 85) return strips[0].Color(255 - pos * 3, 0, pos * 3);
  if (pos < 170) { pos -= 85; return strips[0].Color(0, pos * 3, 255 - pos * 3); }
  pos -= 170;
  return strips[0].Color(pos * 3, 255 - pos * 3, 0);
}

// ===== sequences =====
const char *const T9_SEQ[] = {
    "Hornsby","Normanhurst","Thornleigh","Pennant Hills","Beecroft","Cheltenham",
    "Epping","Eastwood","Denistone","West Ryde","Meadowbank","Rhodes",
    "Concord West","North Strathfield","Strathfield","Burwood","Redfern","Central",
    "Town Hall","Wynyard","Milsons Point","North Sydney","Waverton","Wollstonecraft",
    "St Leonards","Artarmon","Chatswood","Roseville","Lindfield","Killara","Gordon"};
const char *const T5_SEQ[] = {
    "Richmond","East Richmond","Clarendon","Windsor","Mulgrave","Vineyard",
    "Riverstone","Schofields","Quakers Hill","Marayong","Blacktown","Seven Hills",
    "Toongabbie","Pendle Hill","Wentworthville","Westmead","Parramatta","Harris Park",
    "Merrylands","Guildford","Yennora","Fairfield","Canley Vale","Cabramatta",
    "Warwick Farm","Liverpool","Casula","Glenfield","Edmondson Park","Leppington"};
const char *const T2_SEG1[] = {"Leppington","Edmondson Park","Glenfield","Casula",
    "Liverpool","Warwick Farm","Cabramatta","Canley Vale","Fairfield","Yennora",
    "Guildford","Merrylands"};
const char *const T2_SEG2[] = {
    "Parramatta","Harris Park","Granville","Clyde","Auburn","Lidcombe","Flemington",
    "Homebush","Strathfield","Burwood","Croydon","Ashfield","Summer Hill","Lewisham",
    "Petersham","Stanmore","Newtown","Macdonaldtown","Redfern","Central","Town Hall",
    "Wynyard","Circular Quay","St James","Museum"};
const char *const T6_SEQ[] = {"Bankstown","Yagoona","Birrong","Regents Park","Berala","Lidcombe"};
const char *const T3_SEQ[] = {
    "Liverpool","Warwick Farm","Cabramatta","Carramar","Villawood","Leightonfield",
    "Chester Hill","Sefton","Regents Park","Berala","Lidcombe","Flemington","Homebush",
    "Strathfield","Burwood","Croydon","Ashfield","Summer Hill","Lewisham","Petersham",
    "Stanmore","Newtown","Macdonaldtown","Redfern","Central","Town Hall","Wynyard",
    "Circular Quay","St James","Museum"};
const char *const T4_SEQ[] = {
    "Waterfall","Heathcote","Engadine","Loftus","Cronulla","Woolooware","Caringbah",
    "Miranda","Gymea","Kirrawee","Sutherland","Jannali","Como","Oatley","Mortdale",
    "Penshurst","Hurstville","Allawah","Carlton","Kogarah","Rockdale","Banksia",
    "Arncliffe","Wolli Creek","Tempe","Sydenham","Redfern","Central","Town Hall",
    "Martin Place","Kings Cross","Edgecliff","Bondi Junction"};
const char *const T7_SEQ[] = {"Lidcombe","Olympic Park"};
const char *const T8_SEQ[] = {
    "Macarthur","Campbelltown","Leumeah","Minto","Ingleburn","Macquarie Fields",
    "Glenfield","Holsworthy","East Hills","Panania","Revesby","Padstow","Riverwood",
    "Narwee","Beverly Hills","Kingsgrove","Bexley North","Bardwell Park","Turrella",
    "Sydenham","St Peters","Erskineville","Redfern","Wolli Creek","International Airport",
    "Domestic Airport","Mascot","Green Square","Central","Town Hall","Wynyard",
    "Circular Quay","St James","Museum"};
const char *const M1_SEQ[] = {
    "Tallawong","Rouse Hill","Kellyville","Bella Vista","Norwest","Hills Showground",
    "Castle Hill","Cherrybrook","Epping","Macquarie University","Macquarie Park",
    "North Ryde","Chatswood","Crows Nest","Victoria Cross","Barangaroo","Martin Place",
    "Gadigal","Central","Waterloo","Sydenham","Marrickville","Dulwich Hill",
    "Hurlstone Park","Canterbury","Campsie","Belmore","Lakemba","Wiley Park","Punchbowl","Bankstown"};
const char *const T1_SEQ[] = {
    "Berowra","Mount Kuring-gai","Mount Colah","Asquith","Hornsby","Waitara","Wahroonga",
    "Warrawee","Turramurra","Pymble","Gordon","Killara","Lindfield","Roseville",
    "Chatswood","Artarmon","St Leonards","Wollstonecraft","Waverton","North Sydney",
    "Milsons Point","Wynyard","Town Hall","Central","Redfern","Strathfield","Lidcombe",
    "Auburn","Clyde","Granville","Harris Park","Parramatta","Westmead","Wentworthville",
    "Pendle Hill","Toongabbie","Seven Hills","Blacktown","Doonside","Rooty Hill",
    "Mount Druitt","St Marys","Werrington","Kingswood","Penrith","Emu Plains","Marayong",
    "Quakers Hill","Schofields","Riverstone","Vineyard","Mulgrave","Windsor","Clarendon",
    "East Richmond","Richmond"};

// ===== rendering (original metroboard behavior) =====
void runAnimationStep() {
  static uint32_t lastChaseStep = 0;
  static uint16_t chasePos[LINE_COUNT];
  static int8_t chaseDir[LINE_COUNT];
  static bool chaseInit = false;
  static String lastAnim = "";

  uint32_t now = millis();
  if (!lastAnim.equalsIgnoreCase(gAnim.name)) { chaseInit = false; lastAnim = gAnim.name; }

  String anim = gAnim.name; anim.toLowerCase();
  uint32_t speed = (gAnim.speed <= 0) ? 100 : (uint32_t)gAnim.speed;

  if (anim == "rainbow") {
    uint32_t shift = now / speed;
    for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
      strips[ln].setBrightness(gEffectiveBrightness);
      for (uint16_t i = 0; i < STRIP_LEN[ln]; i++) {
        uint8_t hue = (uint8_t)((i * 256 / STRIP_LEN[ln] + shift) & 0xFF);
        strips[ln].setPixelColor(i, wheel(hue));
      }
      strips[ln].show();
    }
    return;
  }

  if (anim == "bounce" || anim == "chase") {
    if (!chaseInit) {
      for (uint8_t ln = 0; ln < LINE_COUNT; ln++) { chasePos[ln] = 0; chaseDir[ln] = 1; }
      chaseInit = true; lastChaseStep = now;
    }
    if (now - lastChaseStep >= speed) {
      lastChaseStep = now;
      for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
        uint16_t len = STRIP_LEN[ln];
        chasePos[ln] = uint16_t(int16_t(chasePos[ln]) + chaseDir[ln]);
        if (chasePos[ln] == 0 || chasePos[ln] + 1 >= len) chaseDir[ln] = -chaseDir[ln];
      }
    }
    for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
      strips[ln].setBrightness(gEffectiveBrightness);
      strips[ln].clear();
      uint32_t col = lineColor((Line)ln);
      int16_t center = chasePos[ln];
      for (int k = -1; k <= 1; k++) {
        int16_t idx = center + k;
        if (idx >= 0 && idx < (int16_t)STRIP_LEN[ln]) strips[ln].setPixelColor((uint16_t)idx, col);
      }
      strips[ln].show();
    }
    return;
  }

  uint32_t cycle = speed * 20;
  float t = cycle ? float(now % cycle) / float(cycle) : 0.0f;
  float intensity = 0.5f + 0.5f * sinf(2.0f * 3.14159f * t);
  uint8_t br = (uint8_t)(gEffectiveBrightness * intensity);
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
    strips[ln].setBrightness(br);
    for (uint16_t i = 0; i < STRIP_LEN[ln]; i++)
      strips[ln].setPixelColor(i, strips[ln].Color(gAnim.r, gAnim.g, gAnim.b));
    strips[ln].show();
  }
}

void buildBindings() {
  bindSeq(L_T9, T9_SEQ, sizeof(T9_SEQ) / sizeof(*T9_SEQ));
  bindSeq(L_T5, T5_SEQ, sizeof(T5_SEQ) / sizeof(*T5_SEQ));
  bindSeq(L_T2, T2_SEG1, sizeof(T2_SEG1) / sizeof(*T2_SEG1), 0);
  bindSeq(L_T2, T2_SEG2, sizeof(T2_SEG2) / sizeof(*T2_SEG2), 12);
  bindSeq(L_T6, T6_SEQ, sizeof(T6_SEQ) / sizeof(*T6_SEQ));
  bindSeq(L_T3, T3_SEQ, sizeof(T3_SEQ) / sizeof(*T3_SEQ));
  bindSeq(L_T4, T4_SEQ, sizeof(T4_SEQ) / sizeof(*T4_SEQ));
  bindSeq(L_T7, T7_SEQ, sizeof(T7_SEQ) / sizeof(*T7_SEQ));
  bindSeq(L_T8, T8_SEQ, sizeof(T8_SEQ) / sizeof(*T8_SEQ));
  bindSeq(L_M1, M1_SEQ, sizeof(M1_SEQ) / sizeof(*M1_SEQ));
  bindSeq(L_T1, T1_SEQ, sizeof(T1_SEQ) / sizeof(*T1_SEQ));
  stateBuf = (uint8_t *)malloc(NB ? NB : 1);
  ttlBuf = (uint8_t *)malloc(NB ? NB : 1);
  if (stateBuf) memset(stateBuf, 0, NB);
  if (ttlBuf) memset(ttlBuf, 0, NB);
  Serial.printf("Bindings=%u\n", (unsigned)NB);
}

bool dirtyLine[LINE_COUNT] = {0};
inline void clearDirty() {
  for (uint8_t i = 0; i < LINE_COUNT; i++) dirtyLine[i] = false;
}

void applyBatchToState(JsonVariantConst states) {
  if (!states.is<JsonObjectConst>()) return;
  JsonObjectConst obj = states.as<JsonObjectConst>();
  for (size_t i = 0; i < NB; i++) {
    JsonVariantConst v = obj[B[i].station];
    if (v.isNull()) continue;
    int s = v.as<int>();
    if (s < 0) s = 0;
    if (s > 3) s = 3;
    stateBuf[i] = (uint8_t)s;
    if (s == 3) ttlBuf[i] = GRACE_POLLS;
    dirtyLine[B[i].line] = true;
  }
}

void repaintDirtyLinesAndDecayOnce() {
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
    strips[ln].setBrightness(gEffectiveBrightness);
    if (!dirtyLine[ln]) continue;
    strips[ln].clear();
    for (size_t i = 0; i < NB; i++) {
      if (B[i].line != (Line)ln) continue;
      bool on = (stateBuf[i] == 3) || (ttlBuf[i] > 0);
      if (on) strips[ln].setPixelColor(B[i].led, lineColor(B[i].line));
    }
    strips[ln].show();
  }
  for (size_t i = 0; i < NB; i++)
    if (stateBuf[i] != 3 && ttlBuf[i] > 0) ttlBuf[i]--;
  clearDirty();
}

void renderAll() {
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
    strips[ln].setBrightness(gEffectiveBrightness);
    strips[ln].clear();
  }
  for (size_t i = 0; i < NB; i++) {
    bool on = (stateBuf[i] == 3) || (ttlBuf[i] > 0);
    if (on) strips[B[i].line].setPixelColor(B[i].led, lineColor(B[i].line));
  }
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++) strips[ln].show();
  for (size_t i = 0; i < NB; i++)
    if (stateBuf[i] != 3 && ttlBuf[i] > 0) ttlBuf[i]--;
}

bool loadConfigFromPrefs() {
  gPrefs.begin("metroboard", true);
  String ssid = gPrefs.getString("ssid", gConfig.ssid);
  String pass = gPrefs.getString("pass", gConfig.pass);
  String board = gPrefs.getString("board", gConfig.boardId);
  gPrefs.end();
  gConfig.ssid = ssid; gConfig.pass = pass; gConfig.boardId = board;
  return gConfig.isValid();
}

void saveConfigToPrefs(const DeviceConfig &cfg) {
  gPrefs.begin("metroboard", false);
  gPrefs.putString("ssid", cfg.ssid);
  gPrefs.putString("pass", cfg.pass);
  gPrefs.putString("board", cfg.boardId);
  gPrefs.end();
}

String configPageHtml() {
  String html =
      "<!doctype html><html><head><meta charset='utf-8'/>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
      "<title>Metroboard Setup</title>"
      "<style>body{font-family:Arial,Helvetica,sans-serif;margin:24px;}"
      "label{display:block;margin-top:12px;font-weight:600;}"
      "input{width:100%;max-width:360px;padding:8px;margin-top:6px;}"
      "button{margin-top:18px;padding:10px 16px;font-size:15px;}"
      ".note{margin-top:18px;color:#555;}</style></head><body>"
      "<h1>Metroboard Setup</h1>"
      "<form method='POST' action='/save'>"
      "<label>WiFi SSID</label>"
      "<input name='ssid' required value='" + gConfig.ssid + "'/>"
      "<label>WiFi Password</label>"
      "<input name='pass' type='password' value='" + gConfig.pass + "'/>"
      "<label>Board ID</label>"
      "<input name='board' required value='" + gConfig.boardId + "'/>"
      "<button type='submit'>Save & Restart</button></form>"
      "<p class='note'>After saving, the board will reboot.</p></body></html>";
  return html;
}

void startConfigPortal() {
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_AP);
  uint32_t suffix = (uint32_t)(ESP.getEfuseMac() & 0xFFFFFF);
  String apName = "Metroboard-Setup-" + String(suffix, HEX);
  WiFi.softAP(apName.c_str());
  IPAddress ip = WiFi.softAPIP();
  Serial.printf("\n[CFG] AP \"%s\" IP=%s\n", apName.c_str(), ip.toString().c_str());

  gDnsServer.start(53, "*", ip);

  gConfigServer.on("/", HTTP_GET, []() { gConfigServer.send(200, "text/html", configPageHtml()); });
  gConfigServer.on("/generate_204", HTTP_GET, []() { gConfigServer.sendHeader("Location","/",true); gConfigServer.send(302,"text/plain",""); });
  gConfigServer.on("/hotspot-detect.html", HTTP_GET, []() { gConfigServer.sendHeader("Location","/",true); gConfigServer.send(302,"text/plain",""); });
  gConfigServer.on("/ncsi.txt", HTTP_GET, []() { gConfigServer.sendHeader("Location","/",true); gConfigServer.send(302,"text/plain",""); });
  gConfigServer.on("/connecttest.txt", HTTP_GET, []() { gConfigServer.sendHeader("Location","/",true); gConfigServer.send(302,"text/plain",""); });
  gConfigServer.on("/redirect", HTTP_GET, []() { gConfigServer.sendHeader("Location","/",true); gConfigServer.send(302,"text/plain",""); });
  gConfigServer.on("/save", HTTP_POST, []() {
    DeviceConfig next = gConfig;
    next.ssid = gConfigServer.arg("ssid");
    next.pass = gConfigServer.arg("pass");
    next.boardId = gConfigServer.arg("board");
    next.ssid.trim(); next.boardId.trim();
    if (!next.isValid()) { gConfigServer.send(400, "text/plain", "SSID and Board ID are required."); return; }
    saveConfigToPrefs(next);
    gConfigServer.send(200, "text/html", "<html><body><h2>Saved.</h2><p>Rebooting...</p></body></html>");
    delay(500);
    ESP.restart();
  });
  gConfigServer.onNotFound([]() { gConfigServer.sendHeader("Location","/",true); gConfigServer.send(302,"text/plain",""); });
  gConfigServer.begin();
  setStatus(STATUS_SERVER_ACTIVE);
  for (;;) {
    gDnsServer.processNextRequest();
    gConfigServer.handleClient();
    delay(10);
  }
}

bool wifiConnect(uint32_t timeoutMs = 20000) {
  WiFi.mode(WIFI_STA); WiFi.setSleep(false);
  setStatus(STATUS_WIFI_CONNECTING);
  if (gConfig.pass.length() > 0) WiFi.begin(gConfig.ssid.c_str(), gConfig.pass.c_str());
  else WiFi.begin(gConfig.ssid.c_str());
  Serial.printf("WiFi: \"%s\"", gConfig.ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) { delay(300); Serial.print('.'); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nIP=%s heap=%u\n", WiFi.localIP().toString().c_str(), ESP.getFreeHeap());
    if (gBoardIdValid) setStatus(STATUS_WIFI_CONNECTED_VALID);
    return true;
  }
  Serial.println("\n[WiFi] FAILED to connect.");
  setStatus(STATUS_WIFI_DISCONNECTED);
  return false;
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (gBoardIdValid) setStatus(STATUS_WIFI_CONNECTED_VALID);
    return;
  }
  Serial.println("[WiFi] LOST connection. Reconnecting...");
  setStatus(STATUS_WIFI_CONNECTING);
  WiFi.disconnect(true, true);
  delay(100);
  if (gConfig.pass.length() > 0) WiFi.begin(gConfig.ssid.c_str(), gConfig.pass.c_str());
  else WiFi.begin(gConfig.ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) { Serial.print('.'); delay(250); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] Reconnected. IP=%s\n", WiFi.localIP().toString().c_str());
    if (gBoardIdValid) setStatus(STATUS_WIFI_CONNECTED_VALID);
  } else {
    Serial.println("\n[WiFi] FAILED to reconnect.");
    setStatus(STATUS_WIFI_DISCONNECTED);
  }
}

// ===== HTTP OTA with verbose progress logging =====
bool performOtaUpdate(const char *firmwareUrl, const char *expectedMd5,
                      int expectedSize, const char *targetVersion) {
  if (!firmwareUrl || strlen(firmwareUrl) == 0) return false;
  if (WiFi.status() != WL_CONNECTED) return false;

  gOtaInProgress = true;
  gLastOtaAttemptMs = millis();
  gLastOtaTargetVersion = String(targetVersion ? targetVersion : "");
  gLastOtaResult = "in_progress";
  setStatus(STATUS_OTA_UPDATING);

  Serial.println();
  Serial.println("================ OTA START ================");
  Serial.printf("[OTA] from version : %s\n", kFirmwareVersion);
  Serial.printf("[OTA] to version   : %s\n", targetVersion && strlen(targetVersion) ? targetVersion : "(unknown)");
  Serial.printf("[OTA] url          : %s\n", firmwareUrl);
  Serial.printf("[OTA] manifest size: %d bytes\n", expectedSize);
  Serial.printf("[OTA] manifest md5 : %s\n", expectedMd5 && strlen(expectedMd5) ? expectedMd5 : "(none)");
  Serial.printf("[OTA] free heap    : %u\n", ESP.getFreeHeap());

  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(30000);
  client.setHandshakeTimeout(30000);

  HTTPClient http;
  http.setTimeout(30000);
  http.setReuse(false);

  if (!http.begin(client, firmwareUrl)) {
    Serial.println("[OTA] FAIL: http.begin");
    gLastOtaResult = "fail_http_begin";
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }
  http.addHeader("Accept", "application/octet-stream");
  http.addHeader("Cache-Control", "no-cache");
  http.addHeader("Connection", "close");
  http.addHeader("User-Agent", String("ESP32-Metroboard/ota/") + kFirmwareVersion);

  int code = http.GET();
  Serial.printf("[OTA] http response: %d\n", code);
  if (code != HTTP_CODE_OK) {
    gLastOtaResult = String("fail_http_") + code;
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }

  int contentLength = http.getSize();
  int updateSize = contentLength > 0 ? contentLength : expectedSize;
  Serial.printf("[OTA] http content-length: %d\n", contentLength);
  Serial.printf("[OTA] update size to use : %d\n", updateSize);

  if (expectedSize > 0 && contentLength > 0 && expectedSize != contentLength) {
    Serial.printf("[OTA] FAIL: size mismatch manifest=%d http=%d\n", expectedSize, contentLength);
    gLastOtaResult = "fail_size_mismatch";
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }
  if (updateSize <= 0) {
    Serial.println("[OTA] FAIL: missing content length");
    gLastOtaResult = "fail_no_size";
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }

  // Show which partition we're flashing into
  const esp_partition_t *running = esp_ota_get_running_partition();
  const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
  if (running) Serial.printf("[OTA] running from   : %s @ 0x%06x\n", running->label, running->address);
  if (target)  Serial.printf("[OTA] flashing into  : %s @ 0x%06x (slot %u bytes)\n",
                              target->label, target->address, target->size);

  if (!Update.begin((size_t)updateSize)) {
    Serial.printf("[OTA] FAIL: Update.begin error=%u\n", Update.getError());
    gLastOtaResult = String("fail_update_begin_") + Update.getError();
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }

  // Per-chunk progress callback (logs every 5%)
  Update.onProgress([](size_t written, size_t total) {
    static uint8_t lastPct = 255;
    if (total == 0) return;
    uint8_t pct = (uint8_t)((written * 100ULL) / total);
    if (pct != lastPct && pct % 5 == 0) {
      lastPct = pct;
      Serial.printf("[OTA] progress %3u%% (%u / %u bytes)\n",
                    pct, (unsigned)written, (unsigned)total);
    }
  });

  if (expectedMd5 && strlen(expectedMd5) == 32) {
    if (!Update.setMD5(expectedMd5)) {
      Serial.println("[OTA] FAIL: invalid MD5 from manifest");
      Update.abort();
      gLastOtaResult = "fail_md5_invalid";
      http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
      return false;
    }
    Serial.println("[OTA] MD5 verification armed");
  } else {
    Serial.println("[OTA] WARN: no MD5 — skipping verification");
  }

  Serial.println("[OTA] streaming...");
  WiFiClient *stream = http.getStreamPtr();
  uint32_t streamStart = millis();
  size_t written = Update.writeStream(*stream);
  uint32_t streamMs = millis() - streamStart;
  Serial.printf("[OTA] wrote %u / %d bytes in %u ms (%.1f kB/s)\n",
                (unsigned)written, updateSize, (unsigned)streamMs,
                streamMs ? (written / 1024.0f) / (streamMs / 1000.0f) : 0.0f);

  if (written != (size_t)updateSize) {
    Serial.println("[OTA] FAIL: short write");
    Update.abort();
    gLastOtaResult = "fail_short_write";
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }

  if (!Update.end()) {
    Serial.printf("[OTA] FAIL: Update.end error=%u\n", Update.getError());
    gLastOtaResult = String("fail_update_end_") + Update.getError();
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }
  if (!Update.isFinished()) {
    Serial.println("[OTA] FAIL: did not finish cleanly");
    gLastOtaResult = "fail_not_finished";
    http.end(); gOtaInProgress = false; setStatus(STATUS_WIFI_CONNECTED_VALID);
    return false;
  }

  http.end();
  gLastOtaResult = "success_rebooting";
  Serial.println("[OTA] SUCCESS — rebooting in 500 ms");
  Serial.println("==========================================");
  delay(500);
  ESP.restart();
  return true;
}

bool checkForOtaUpdate() {
  if (gOtaInProgress || WiFi.status() != WL_CONNECTED || !gBoardIdValid) return false;

  String url = serviceBaseUrl();
  url += "/firmware_manifest?board_id=";
  url += enc(gConfig.boardId.c_str());
  url += "&current=";
  url += enc(kFirmwareVersion);
  url += "&build=";
  url += enc(kBuildLabel);

  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(20000);
  client.setHandshakeTimeout(20000);

  HTTPClient http;
  http.setTimeout(20000);
  http.setReuse(false);

  Serial.printf("[OTA] check %s\n", url.c_str());

  if (!http.begin(client, url)) { http.end(); return false; }
  http.addHeader("Accept", "application/json");
  http.addHeader("Accept-Encoding", "identity");
  http.addHeader("Cache-Control", "no-cache");
  http.addHeader("Connection", "close");
  http.addHeader("User-Agent", String("ESP32-Metroboard/ota/") + kFirmwareVersion);

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[OTA] manifest HTTP %d\n", code);
    http.end(); return false;
  }

  String body = http.getString();
  http.end();
  body.trim();

  StaticJsonDocument<1536> doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) { Serial.printf("[OTA] manifest JSON error: %s\n", err.c_str()); return false; }

  bool updateAvailable = doc["update_available"] | false;
  const char *version = doc["version"] | "";
  const char *firmwareUrl = doc["url"] | "";
  const char *md5 = doc["md5"] | "";
  int size = doc["size"] | 0;

  Serial.printf("[OTA] manifest: available=%d version=\"%s\" size=%d md5=%s\n",
                updateAvailable, version, size, md5);
  Serial.printf("[OTA] manifest url: %s\n", firmwareUrl);

  if (!updateAvailable) {
    const char *reason = doc["reason"] | "none";
    Serial.printf("[OTA] no update. current=%s latest=%s reason=%s\n",
                  kFirmwareVersion, version, reason);
    return false;
  }

  return performOtaUpdate(firmwareUrl, md5, size, version);
}

// ===== Batch tuner =====
static const size_t URL_CHAR_BUDGET = 1700;
static const uint8_t MAX_BATCHES_PER_POLL = 6;

size_t computeBatchEnd(size_t i) {
  const char *base = kPayloadBase;
  size_t baseLen = strlen(base) + strlen("?board_id=") + gConfig.boardId.length() +
                   strlen("&fw=") + strlen(kFirmwareVersion) +
                   strlen("&build=") + strlen(kBuildLabel) + strlen("&only=");
  size_t used = baseLen;
  size_t j = i;
  while (j < NB) {
    const char *s = B[j].station;
    size_t need = (j == i) ? 0 : 1;
    while (*s) {
      char c = *s++;
      if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') need += 1;
      else need += 3;
    }
    if (used + need > URL_CHAR_BUDGET) break;
    used += need; j++;
  }
  if (j == i) j++;
  return j;
}

int fetchAllBatchesAndRenderOnce(bool allowRender) {
  if (NB == 0) return 0;

  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(20000);
  client.setHandshakeTimeout(20000);

  size_t i = 0;
  int ok = 0;
  uint8_t fired = 0;
  bool settingsApplied = false;

  while (i < NB && fired < MAX_BATCHES_PER_POLL) {
    size_t end = computeBatchEnd(i);

    String url = String(kPayloadBase);
    url += "?board_id="; url += gConfig.boardId;
    url += "&fw="; url += enc(kFirmwareVersion);
    url += "&build="; url += enc(kBuildLabel);
    url += "&only=";
    for (size_t j = i; j < end; j++) { if (j > i) url += ','; url += enc(B[j].station); }

    HTTPClient http;
    http.setTimeout(20000);
    http.setReuse(false);
    http.useHTTP10(false);
    http.addHeader("Accept", "application/json");
    http.addHeader("Accept-Encoding", "identity");
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Connection", "close");
    http.addHeader("User-Agent", "ESP32-Metroboard/orig+dbg/1.0");

    if (!http.begin(client, url)) { http.end(); break; }
    const char *hk[] = {"Content-Type", "Content-Encoding"};
    http.collectHeaders(hk, 2);

    Serial.printf("[BATCH] GET %s\n", url.c_str());
    int code = http.GET();
    if (code != HTTP_CODE_OK) { http.end(); break; }

    if (http.header("Content-Encoding").indexOf("gzip") >= 0 ||
        http.header("Content-Type").indexOf("application/json") < 0) { http.end(); break; }

    String body = http.getString();
    http.end();
    body.trim();
    char c0 = body.length() ? body.charAt(0) : '\0';
    if (c0 != '{' && c0 != '[') break;

    gDoc.clear();
    if (deserializeJson(gDoc, body)) break;

    JsonObject root = gDoc.as<JsonObject>();
    uint32_t ver = root["settings_version"] | 0;
    if (!settingsApplied &&
        (ver == 0 || ver != lastSettingsVersion ||
         millis() - lastSettingsFetch >= SETTINGS_FETCH_MS)) {
      lastSettingsVersion = ver;
      lastSettingsFetch = millis();
      const char *mode = root["mode"] | "live";
      gMode = String(mode); gMode.toLowerCase();
      int b = root["brightness"] | 16; b = constrain(b, 0, 255);
      int bEff = root["brightness_effective"] | b; bEff = constrain(bEff, 0, 255);
      gBaseBrightness = b; gEffectiveBrightness = (uint8_t)bEff;
      JsonObject nm = root["night_mode"];
      if (!nm.isNull()) {
        gNightMode.enabled = nm["enabled"] | false;
        gNightMode.active = root["night_mode_active"] | false;
        gNightMode.start = nm["start"] | "23:00";
        gNightMode.end = nm["end"] | "06:00";
        gNightMode.action = nm["action"] | "off";
        gNightMode.dimLevel = (uint8_t)(nm["dim_level"] | 20);
      }
      JsonObject an = root["animation"];
      if (!an.isNull()) {
        gAnim.name = an["name"] | "pulse";
        gAnim.speed = an["speed"] | 120;
        JsonArray col = an["color"].as<JsonArray>();
        if (col.size() >= 3) { gAnim.r = uint8_t(col[0]); gAnim.g = uint8_t(col[1]); gAnim.b = uint8_t(col[2]); }
      }
      settingsApplied = true;
      Serial.printf("[CFG] ver=%u mode=%s bright=%u eff=%u night=%d active=%d anim=%s\n",
                    ver, gMode.c_str(), gBaseBrightness, gEffectiveBrightness,
                    gNightMode.enabled, gNightMode.active, gAnim.name.c_str());
    }

    JsonVariantConst states = root["states"];
    applyBatchToState(states);

    ok++; fired++; i = end;
    Serial.printf("[BATCH] HTTP %d  (%d bytes)\n", code, body.length());
    delay(5); yield();
  }

  if (allowRender) {
    if (ok > 0) repaintDirtyLinesAndDecayOnce();
    else renderAll();
  }
  return ok;
}

// ===== setup / loop =====
TaskHandle_t gPollTaskHandle = nullptr;

void beginStrips() {
  for (uint8_t i = 0; i < LINE_COUNT; i++) {
    strips[i].begin();
    strips[i].setBrightness(gEffectiveBrightness);
    strips[i].clear();
    strips[i].show();
  }
}

void initStatusLed() {
  statusStrip.begin();
  statusStrip.setBrightness(kStatusBrightness);
  setStatus(STATUS_POWER_ON);
}

bool isBoardIdValid() {
  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(20000);
  client.setHandshakeTimeout(20000);
  HTTPClient http;
  String url = serviceBaseUrl();
  url += "/board_settings?board_id="; url += enc(gConfig.boardId.c_str());
  if (!http.begin(client, url)) { http.end(); return true; }
  int code = http.GET();
  http.end();
  if (code == HTTP_CODE_OK) return true;
  if (code == HTTP_CODE_BAD_REQUEST || code == HTTP_CODE_NOT_FOUND || code == HTTP_CODE_FORBIDDEN) return false;
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(150);
  Serial.printf("\nMetroboard %s [build=%s]\n", kFirmwareVersion, kBuildLabel);
  initStatusLed();
  loadConfigFromPrefs();
  if (!gConfig.isValid()) { Serial.println("[CFG] Missing settings. Starting setup portal."); startConfigPortal(); }
  if (!wifiConnect(90000)) { Serial.println("[CFG] WiFi failed. Starting setup portal."); setStatus(STATUS_WIFI_DISCONNECTED); startConfigPortal(); }
  gBoardIdValid = isBoardIdValid();
  if (!gBoardIdValid) { Serial.println("[CFG] Board ID invalid. Starting setup portal."); setStatus(STATUS_BOARD_INVALID); startConfigPortal(); }
  setStatus(STATUS_WIFI_CONNECTED_VALID);
  beginStrips();
  buildBindings();
  renderAll();

  // Print startup status immediately
  printDebugStatus();

  xTaskCreatePinnedToCore(
      [](void *) {
        for (;;) {
          ensureWifi();
          uint32_t now = millis();
          if (!gOtaInProgress && now > OTA_STARTUP_DELAY_MS &&
              (lastOtaCheck == 0 || now - lastOtaCheck >= OTA_CHECK_MS)) {
            lastOtaCheck = now;
            checkForOtaUpdate();
          }
          if (gMode != "animation" && now - lastPoll >= pollInterval) {
            lastPoll = now;
            int batches = fetchAllBatchesAndRenderOnce(true);
            if (batches > 0) pollInterval = POLL_MS_MIN;
            else { uint32_t next = pollInterval + 1500; if (next > POLL_MS_MAX) next = POLL_MS_MAX; pollInterval = next; }
          } else if (gMode == "animation" && now - lastSettingsFetch >= SETTINGS_FETCH_MS) {
            fetchAllBatchesAndRenderOnce(false);
          }
          vTaskDelay(10 / portTICK_PERIOD_MS);
        }
      },
      "pollTask", 8192, nullptr, 1, &gPollTaskHandle, 0);
}

void loop() {
  static uint32_t lastDebugMs = 0;
  uint32_t now = millis();
  if (now - lastDebugMs >= DEBUG_PRINT_MS) { lastDebugMs = now; printDebugStatus(); }

  if (gOtaInProgress) { delay(50); return; }
  if (gMode == "animation") { runAnimationStep(); delay(20); }
  else { delay(5); }
}
