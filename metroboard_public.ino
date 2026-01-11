// Metroboard — public deployment sketch.

#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <mbedtls/sha256.h>
#include <time.h>

// =========================================
// ===== Optional defaults (leave blank) ===
// =========================================
// These values are only used if no settings are saved in flash.
#define DEFAULT_WIFI_SSID ""
#define DEFAULT_WIFI_PASS ""
#define DEFAULT_BOARD_ID ""
// =========================================
// =========================================
// =========================================

static const char *kPayloadBase =
    "https://damp-catlin-metroboard-7be2a3b3.koyeb.app/board_payload";
static const char *kFirmwareVersion = "1.0.0";

static const uint32_t POLL_MS_MIN = 1000, POLL_MS_MAX = 5000;
static const uint32_t SETTINGS_FETCH_MS = 30000;
static const uint32_t OTA_CHECK_MS = 6UL * 60UL * 60UL * 1000UL;

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
  L_T9,
  L_T5,
  L_T2,
  L_T6,
  L_T3,
  L_T4,
  L_T7,
  L_T8,
  L_M1,
  L_T1,
  LINE_COUNT
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
  bool enabled;
  bool active;
  String start;
  String end;
  String action;
  uint8_t dimLevel;
} gNightMode = {false, false, "23:00", "06:00", "off", 20};

struct AnimCfg {
  String name;
  int speed;
  uint8_t r, g, b;
} gAnim = {"pulse", 120, 255, 80, 0};

uint32_t lastSettingsFetch = 0;
uint32_t lastSettingsVersion = 0;
uint32_t lastOtaCheck = 0;

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

// ===== helpers =====
enum StatusCode : uint8_t {
  STATUS_POWER_ON,
  STATUS_WIFI_CONNECTED_VALID,
  STATUS_SERVER_ACTIVE,
  STATUS_WIFI_CONNECTING,
  STATUS_WIFI_DISCONNECTED,
  STATUS_BOARD_INVALID
};

void setStatus(StatusCode code) {
  uint8_t r = 0, g = 0, b = 0;
  switch (code) {
  case STATUS_POWER_ON:
    r = 160;
    g = 160;
    b = 160;
    break;
  case STATUS_WIFI_CONNECTED_VALID:
    r = 0;
    g = 160;
    b = 0;
    break;
  case STATUS_SERVER_ACTIVE:
    r = 80;
    g = 180;
    b = 255;
    break;
  case STATUS_WIFI_CONNECTING:
    r = 200;
    g = 80;
    b = 0;
    break;
  case STATUS_WIFI_DISCONNECTED:
    r = 200;
    g = 0;
    b = 0;
    break;
  case STATUS_BOARD_INVALID:
    r = 200;
    g = 0;
    b = 120;
    break;
  }
  statusStrip.setBrightness(kStatusBrightness);
  statusStrip.setPixelColor(0, statusStrip.Color(r, g, b));
  statusStrip.show();
}

static const char *routeSuffix(Line ln) {
  switch (ln) {
  case L_T9:
    return "9";
  case L_T5:
    return "5";
  case L_T2:
    return "2";
  case L_T6:
    return "6";
  case L_T3:
    return "3";
  case L_T4:
    return "4";
  case L_T7:
    return "7";
  case L_T8:
    return "8";
  case L_M1:
    return "M";
  case L_T1:
    return "1";
  default:
    return "?";
  }
}
static char *makeKey(const char *base, Line ln) {
  const char *suf = routeSuffix(ln);
  size_t lb = strlen(base), ls = strlen(suf);
  char *out = (char *)malloc(lb + ls + 1);
  if (!out)
    return nullptr;
  memcpy(out, base, lb);
  memcpy(out + lb, suf, ls);
  out[lb + ls] = '\0';
  return out;
}
static inline uint16_t physIdx(Line ln, uint16_t led) {
  return REVERSE[ln] ? uint16_t(STRIP_LEN[ln] - 1 - led) : led;
}
void addBind(const char *base, Line ln, uint16_t led) {
  if (NB >= MAX_BINDINGS)
    return;
  uint16_t idx = physIdx(ln, led);
  if (idx >= STRIP_LEN[ln])
    return;
  char *key = makeKey(base, ln);
  if (!key)
    return;
  B[NB++] = {key, ln, idx};
}
void bindSeq(Line ln, const char *const *seq, size_t n, uint16_t start = 0) {
  for (size_t i = 0; i < n; i++)
    addBind(seq[i], ln, start + (uint16_t)i);
}
static inline bool unres(char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
         (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~';
}
static String enc(const char *s) {
  String o;
  while (*s) {
    char c = *s++;
    if (unres(c))
      o += c;
    else {
      char b[4];
      snprintf(b, sizeof(b), "%%%02X", (unsigned char)c);
      o += b;
    }
  }
  return o;
}

struct FirmwareInfo {
  String version;
  String url;
  String sha256;
};

static String firmwareMetaUrl() {
  String url = String(kPayloadBase);
  int slash = url.lastIndexOf('/');
  if (slash > 0) {
    url = url.substring(0, slash);
  }
  url += "/firmware/latest";
  return url;
}

static void parseSemver(const String &version, int parts[3]) {
  parts[0] = 0;
  parts[1] = 0;
  parts[2] = 0;
  String v = version;
  if (v.startsWith("v") || v.startsWith("V")) {
    v = v.substring(1);
  }
  int partIndex = 0;
  int start = 0;
  for (int i = 0; i <= v.length(); i++) {
    if (i == v.length() || v[i] == '.') {
      if (partIndex > 2)
        break;
      String token = v.substring(start, i);
      parts[partIndex++] = token.toInt();
      start = i + 1;
    }
  }
}

static int compareSemver(const String &a, const String &b) {
  int pa[3], pb[3];
  parseSemver(a, pa);
  parseSemver(b, pb);
  for (int i = 0; i < 3; i++) {
    if (pa[i] != pb[i])
      return pa[i] > pb[i] ? 1 : -1;
  }
  return 0;
}

static bool isNewerVersion(const String &remote, const String &current) {
  return compareSemver(remote, current) > 0;
}

static bool fetchFirmwareInfo(FirmwareInfo &info) {
  String url = firmwareMetaUrl();
  bool isHttps = url.startsWith("https://");
  HTTPClient http;
  bool ok = false;

  if (isHttps) {
    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(20000);
    client.setHandshakeTimeout(20000);
    if (http.begin(client, url)) {
      int code = http.GET();
      if (code == HTTP_CODE_OK) {
        String payload = http.getString();
        DynamicJsonDocument doc(1024);
        if (deserializeJson(doc, payload) == DeserializationError::Ok) {
          info.version = doc["version"] | "";
          info.url = doc["url"] | "";
          info.sha256 = doc["sha256"] | "";
          ok = info.version.length() > 0 && info.url.length() > 0 &&
               info.sha256.length() > 0;
        }
      }
    }
  } else {
    WiFiClient client;
    if (http.begin(client, url)) {
      int code = http.GET();
      if (code == HTTP_CODE_OK) {
        String payload = http.getString();
        DynamicJsonDocument doc(1024);
        if (deserializeJson(doc, payload) == DeserializationError::Ok) {
          info.version = doc["version"] | "";
          info.url = doc["url"] | "";
          info.sha256 = doc["sha256"] | "";
          ok = info.version.length() > 0 && info.url.length() > 0 &&
               info.sha256.length() > 0;
        }
      }
    }
  }

  http.end();
  return ok;
}

static String sha256Hex(const unsigned char *digest, size_t len) {
  static const char *hex = "0123456789abcdef";
  String out;
  out.reserve(len * 2);
  for (size_t i = 0; i < len; i++) {
    out += hex[(digest[i] >> 4) & 0x0F];
    out += hex[digest[i] & 0x0F];
  }
  return out;
}

static bool applyOtaUpdate(const FirmwareInfo &info) {
  bool isHttps = info.url.startsWith("https://");
  HTTPClient http;
  int contentLength = -1;
  bool sizeUnknown = true;

  if (isHttps) {
    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(30000);
    client.setHandshakeTimeout(30000);
    if (!http.begin(client, info.url)) {
      return false;
    }
  } else {
    WiFiClient client;
    if (!http.begin(client, info.url)) {
      return false;
    }
  }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    http.end();
    return false;
  }

  contentLength = http.getSize();
  sizeUnknown = contentLength <= 0;

  if (!Update.begin(sizeUnknown ? UPDATE_SIZE_UNKNOWN : contentLength)) {
    http.end();
    return false;
  }

  WiFiClient *stream = http.getStreamPtr();
  uint8_t buf[1024];

  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts_ret(&ctx, 0);

  int remaining = contentLength;
  while (http.connected() && (sizeUnknown || remaining > 0)) {
    size_t available = stream->available();
    if (available) {
      int toRead = (int)min((size_t)sizeof(buf), available);
      int read = stream->readBytes(buf, toRead);
      if (read > 0) {
        mbedtls_sha256_update_ret(&ctx, buf, read);
        size_t written = Update.write(buf, read);
        if (written != (size_t)read) {
          Update.abort();
          mbedtls_sha256_free(&ctx);
          http.end();
          return false;
        }
        if (!sizeUnknown) {
          remaining -= read;
        }
      }
    } else {
      delay(1);
    }
    yield();
  }

  unsigned char digest[32];
  mbedtls_sha256_finish_ret(&ctx, digest);
  mbedtls_sha256_free(&ctx);

  String computed = sha256Hex(digest, sizeof(digest));
  if (!computed.equalsIgnoreCase(info.sha256)) {
    Update.abort();
    http.end();
    return false;
  }

  bool ended = Update.end(sizeUnknown);
  http.end();

  if (!ended || !Update.isFinished()) {
    return false;
  }

  return true;
}
static uint32_t lineColor(Line ln) {
  uint32_t h = LINE_COLOR_HEX[ln];
  uint8_t r = (h >> 16) & 0xFF, g = (h >> 8) & 0xFF, b = h & 0xFF;
  return strips[ln].Color(r, g, b);
}
static uint32_t wheel(uint8_t pos) {
  pos = 255 - pos;
  if (pos < 85)
    return strips[0].Color(255 - pos * 3, 0, pos * 3);
  if (pos < 170) {
    pos -= 85;
    return strips[0].Color(0, pos * 3, 255 - pos * 3);
  }
  pos -= 170;
  return strips[0].Color(pos * 3, 255 - pos * 3, 0);
}

// ===== sequences (synced to tester) =====
const char *const T9_SEQ[] = {
    "Hornsby",       "Normanhurst",       "Thornleigh",  "Pennant Hills",
    "Beecroft",      "Cheltenham",        "Epping",      "Eastwood",
    "Denistone",     "West Ryde",         "Meadowbank",  "Rhodes",
    "Concord West",  "North Strathfield", "Strathfield", "Burwood",
    "Redfern",       "Central",           "Town Hall",   "Wynyard",
    "Milsons Point", "North Sydney",      "Waverton",    "Wollstonecraft",
    "St Leonards",   "Artarmon",          "Chatswood",   "Roseville",
    "Lindfield",     "Killara",           "Gordon"};
const char *const T5_SEQ[] = {
    "Richmond",       "East Richmond", "Clarendon",      "Windsor",
    "Mulgrave",       "Vineyard",      "Riverstone",     "Schofields",
    "Quakers Hill",   "Marayong",      "Blacktown",      "Seven Hills",
    "Toongabbie",     "Pendle Hill",   "Wentworthville", "Westmead",
    "Parramatta",     "Harris Park",   "Merrylands",     "Guildford",
    "Yennora",        "Fairfield",     "Canley Vale",    "Cabramatta",
    "Warwick Farm",   "Liverpool",     "Casula",         "Glenfield",
    "Edmondson Park", "Leppington"};
const char *const T2_SEG1[] = {"Leppington", "Edmondson Park", "Glenfield",
                               "Casula",     "Liverpool",      "Warwick Farm",
                               "Cabramatta", "Canley Vale",    "Fairfield",
                               "Yennora",    "Guildford",      "Merrylands"};
const char *const T2_SEG2[] = {
    "Parramatta", "Harris Park", "Granville",     "Clyde",       "Auburn",
    "Lidcombe",   "Flemington",  "Homebush",      "Strathfield", "Burwood",
    "Croydon",    "Ashfield",    "Summer Hill",   "Lewisham",    "Petersham",
    "Stanmore",   "Newtown",     "Macdonaldtown", "Redfern",     "Central",
    "Town Hall",  "Wynyard",     "Circular Quay", "St James",    "Museum"};
const char *const T6_SEQ[] = {"Bankstown",    "Yagoona", "Birrong",
                              "Regents Park", "Berala",  "Lidcombe"};
const char *const T3_SEQ[] = {
    "Liverpool",    "Warwick Farm",  "Cabramatta",    "Carramar",
    "Villawood",    "Leightonfield", "Chester Hill",  "Sefton",
    "Regents Park", "Berala",        "Lidcombe",      "Flemington",
    "Homebush",     "Strathfield",   "Burwood",       "Croydon",
    "Ashfield",     "Summer Hill",   "Lewisham",      "Petersham",
    "Stanmore",     "Newtown",       "Macdonaldtown", "Redfern",
    "Central",      "Town Hall",     "Wynyard",       "Circular Quay",
    "St James",     "Museum"};
const char *const T4_SEQ[] = {
    "Waterfall",   "Heathcote",  "Engadine",      "Loftus",      "Cronulla",
    "Woolooware",  "Caringbah",  "Miranda",       "Gymea",       "Kirrawee",
    "Sutherland",  "Jannali",    "Como",          "Oatley",      "Mortdale",
    "Penshurst",   "Hurstville", "Allawah",       "Carlton",     "Kogarah",
    "Rockdale",    "Banksia",    "Arncliffe",     "Wolli Creek", "Tempe",
    "Sydenham",    "Redfern",    "Central",       "Town Hall",   "Martin Place",
    "Kings Cross", "Edgecliff",  "Bondi Junction"};
const char *const T7_SEQ[] = {"Lidcombe", "Olympic Park"};
const char *const T8_SEQ[] = {"Macarthur",
                              "Campbelltown",
                              "Leumeah",
                              "Minto",
                              "Ingleburn",
                              "Macquarie Fields",
                              "Glenfield",
                              "Holsworthy",
                              "East Hills",
                              "Panania",
                              "Revesby",
                              "Padstow",
                              "Riverwood",
                              "Narwee",
                              "Beverly Hills",
                              "Kingsgrove",
                              "Bexley North",
                              "Bardwell Park",
                              "Turrella",
                              "Sydenham",
                              "St Peters",
                              "Erskineville",
                              "Redfern",
                              "Wolli Creek",
                              "International Airport",
                              "Domestic Airport",
                              "Mascot",
                              "Green Square",
                              "Central",
                              "Town Hall",
                              "Wynyard",
                              "Circular Quay",
                              "St James",
                              "Museum"};
const char *const M1_SEQ[] = {"Tallawong",      "Rouse Hill",
                              "Kellyville",     "Bella Vista",
                              "Norwest",        "Hills Showground",
                              "Castle Hill",    "Cherrybrook",
                              "Epping",         "Macquarie University",
                              "Macquarie Park", "North Ryde",
                              "Chatswood",      "Crows Nest",
                              "Victoria Cross", "Barangaroo",
                              "Martin Place",   "Gadigal",
                              "Central",        "Waterloo",
                              "Sydenham",       "Marrickville",
                              "Dulwich Hill",   "Hurlstone Park",
                              "Canterbury",     "Campsie",
                              "Belmore",        "Lakemba",
                              "Wiley Park",     "Punchbowl",
                              "Bankstown"};
const char *const T1_SEQ[] = {
    "Berowra",       "Mount Kuring-gai", "Mount Colah", "Asquith",
    "Hornsby",       "Waitara",          "Wahroonga",   "Warrawee",
    "Turramurra",    "Pymble",           "Gordon",      "Killara",
    "Lindfield",     "Roseville",        "Chatswood",   "Artarmon",
    "St Leonards",   "Wollstonecraft",   "Waverton",    "North Sydney",
    "Milsons Point", "Wynyard",          "Town Hall",   "Central",
    "Redfern",       "Strathfield",      "Lidcombe",    "Auburn",
    "Clyde",         "Granville",        "Harris Park", "Parramatta",
    "Westmead",      "Wentworthville",   "Pendle Hill", "Toongabbie",
    "Seven Hills",   "Blacktown",        "Doonside",    "Rooty Hill",
    "Mount Druitt",  "St Marys",         "Werrington",  "Kingswood",
    "Penrith",       "Emu Plains"};

// ===== build & render =====
void runAnimationStep() {
  static uint32_t lastChaseStep = 0;
  static uint16_t chasePos[LINE_COUNT];
  static int8_t chaseDir[LINE_COUNT];
  static bool chaseInit = false;
  static String lastAnim = "";

  uint32_t now = millis();

  if (!lastAnim.equalsIgnoreCase(gAnim.name)) {
    chaseInit = false;
    lastAnim = gAnim.name;
  }

  String anim = gAnim.name;
  anim.toLowerCase();

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
      for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
        chasePos[ln] = 0;
        chaseDir[ln] = 1;
      }
      chaseInit = true;
      lastChaseStep = now;
    }

    if (now - lastChaseStep >= speed) {
      lastChaseStep = now;
      for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
        uint16_t len = STRIP_LEN[ln];
        chasePos[ln] = uint16_t(int16_t(chasePos[ln]) + chaseDir[ln]);
        if (chasePos[ln] == 0 || chasePos[ln] + 1 >= len) {
          chaseDir[ln] = -chaseDir[ln];
        }
      }
    }

    for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
      strips[ln].setBrightness(gEffectiveBrightness);
      strips[ln].clear();
      uint32_t col = lineColor((Line)ln);
      int16_t center = chasePos[ln];
      for (int k = -1; k <= 1; k++) {
        int16_t idx = center + k;
        if (idx >= 0 && idx < (int16_t)STRIP_LEN[ln]) {
          strips[ln].setPixelColor((uint16_t)idx, col);
        }
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
    for (uint16_t i = 0; i < STRIP_LEN[ln]; i++) {
      strips[ln].setPixelColor(i, strips[ln].Color(gAnim.r, gAnim.g, gAnim.b));
    }
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
  if (stateBuf)
    memset(stateBuf, 0, NB);
  if (ttlBuf)
    memset(ttlBuf, 0, NB);
  Serial.printf("Bindings=%u\n", (unsigned)NB);
}

bool dirtyLine[LINE_COUNT] = {0};
inline void clearDirty() {
  for (uint8_t i = 0; i < LINE_COUNT; i++)
    dirtyLine[i] = false;
}

void applyBatchToState(JsonVariantConst states) {
  if (!states.is<JsonObjectConst>())
    return;
  JsonObjectConst obj = states.as<JsonObjectConst>();
  for (size_t i = 0; i < NB; i++) {
    JsonVariantConst v = obj[B[i].station];
    if (v.isNull())
      continue;

    int s = v.as<int>();
    if (s < 0)
      s = 0;
    if (s > 3)
      s = 3;

    stateBuf[i] = (uint8_t)s;
    if (s == 3)
      ttlBuf[i] = GRACE_POLLS;
    dirtyLine[B[i].line] = true;
  }
}

void repaintDirtyLinesAndDecayOnce() {
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
    strips[ln].setBrightness(gEffectiveBrightness);
    if (!dirtyLine[ln])
      continue;
    strips[ln].clear();
    for (size_t i = 0; i < NB; i++) {
      if (B[i].line != (Line)ln)
        continue;
      bool on = (stateBuf[i] == 3) || (ttlBuf[i] > 0);
      if (on)
        strips[ln].setPixelColor(B[i].led, lineColor(B[i].line));
    }
    strips[ln].show();
  }
  for (size_t i = 0; i < NB; i++)
    if (stateBuf[i] != 3 && ttlBuf[i] > 0)
      ttlBuf[i]--;
  clearDirty();
}

void renderAll() {
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
    strips[ln].setBrightness(gEffectiveBrightness);
    strips[ln].clear();
  }
  for (size_t i = 0; i < NB; i++) {
    bool on = (stateBuf[i] == 3) || (ttlBuf[i] > 0);
    if (on)
      strips[B[i].line].setPixelColor(B[i].led, lineColor(B[i].line));
  }
  for (uint8_t ln = 0; ln < LINE_COUNT; ln++)
    strips[ln].show();
  for (size_t i = 0; i < NB; i++)
    if (stateBuf[i] != 3 && ttlBuf[i] > 0)
      ttlBuf[i]--;
}

bool loadConfigFromPrefs() {
  gPrefs.begin("metroboard", true);
  String ssid = gPrefs.getString("ssid", gConfig.ssid);
  String pass = gPrefs.getString("pass", gConfig.pass);
  String board = gPrefs.getString("board", gConfig.boardId);
  gPrefs.end();

  gConfig.ssid = ssid;
  gConfig.pass = pass;
  gConfig.boardId = board;
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
      "<input name='ssid' required value='" +
      gConfig.ssid + "'/>"
                     "<label>WiFi Password</label>"
                     "<input name='pass' type='password' value='" +
      gConfig.pass +
      "'/>"
      "<label>Board ID</label>"
      "<input name='board' required value='" +
      gConfig.boardId +
      "'/>"
      "<button type='submit'>Save & Restart</button>"
      "</form>"
      "<p class='note'>After saving, the board will reboot and connect to your "
      "WiFi.</p>"
      "</body></html>";
  return html;
}

void startConfigPortal() {
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_AP);
  uint32_t suffix = (uint32_t)(ESP.getEfuseMac() & 0xFFFFFF);
  String apName = "Metroboard-Setup-" + String(suffix, HEX);
  WiFi.softAP(apName.c_str());
  IPAddress ip = WiFi.softAPIP();
  Serial.printf("\n[CFG] AP \"%s\" IP=%s\n", apName.c_str(),
                ip.toString().c_str());

  gDnsServer.start(53, "*", ip);

  gConfigServer.on("/", HTTP_GET,
                   []() { gConfigServer.send(200, "text/html", configPageHtml()); });
  gConfigServer.on("/generate_204", HTTP_GET,
                   []() { gConfigServer.sendHeader("Location", "/", true); gConfigServer.send(302, "text/plain", ""); });
  gConfigServer.on("/hotspot-detect.html", HTTP_GET,
                   []() { gConfigServer.sendHeader("Location", "/", true); gConfigServer.send(302, "text/plain", ""); });
  gConfigServer.on("/ncsi.txt", HTTP_GET,
                   []() { gConfigServer.sendHeader("Location", "/", true); gConfigServer.send(302, "text/plain", ""); });
  gConfigServer.on("/connecttest.txt", HTTP_GET,
                   []() { gConfigServer.sendHeader("Location", "/", true); gConfigServer.send(302, "text/plain", ""); });
  gConfigServer.on("/redirect", HTTP_GET,
                   []() { gConfigServer.sendHeader("Location", "/", true); gConfigServer.send(302, "text/plain", ""); });
  gConfigServer.on("/save", HTTP_POST, []() {
    DeviceConfig next = gConfig;
    next.ssid = gConfigServer.arg("ssid");
    next.pass = gConfigServer.arg("pass");
    next.boardId = gConfigServer.arg("board");
    next.ssid.trim();
    next.boardId.trim();

    if (!next.isValid()) {
      gConfigServer.send(400, "text/plain",
                         "SSID and Board ID are required.");
      return;
    }

    saveConfigToPrefs(next);
    gConfigServer.send(200, "text/html",
                       "<html><body><h2>Saved.</h2>"
                       "<p>Rebooting...</p></body></html>");
    delay(500);
    ESP.restart();
  });
  gConfigServer.onNotFound(
      []() { gConfigServer.sendHeader("Location", "/", true); gConfigServer.send(302, "text/plain", ""); });
  gConfigServer.begin();

  setStatus(STATUS_SERVER_ACTIVE);
  for (;;) {
    gDnsServer.processNextRequest();
    gConfigServer.handleClient();
    delay(10);
  }
}

// ===== networking & polling =====
bool wifiConnect(uint32_t timeoutMs = 20000) {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  setStatus(STATUS_WIFI_CONNECTING);
  if (gConfig.pass.length() > 0)
    WiFi.begin(gConfig.ssid.c_str(), gConfig.pass.c_str());
  else
    WiFi.begin(gConfig.ssid.c_str());
  Serial.printf("WiFi: \"%s\"", gConfig.ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) {
    delay(300);
    Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nIP=%s heap=%u\n", WiFi.localIP().toString().c_str(),
                  ESP.getFreeHeap());
    if (gBoardIdValid) {
      setStatus(STATUS_WIFI_CONNECTED_VALID);
    }
    return true;
  }
  Serial.println("\n[WiFi] FAILED to connect.");
  setStatus(STATUS_WIFI_DISCONNECTED);
  return false;
}
void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (gBoardIdValid) {
      setStatus(STATUS_WIFI_CONNECTED_VALID);
    }
    return;
  }

  Serial.println("[WiFi] LOST connection. Reconnecting...");
  setStatus(STATUS_WIFI_CONNECTING);

  WiFi.disconnect(true, true);
  delay(100);
  if (gConfig.pass.length() > 0)
    WiFi.begin(gConfig.ssid.c_str(), gConfig.pass.c_str());
  else
    WiFi.begin(gConfig.ssid.c_str());

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) {
    Serial.print('.');
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] Reconnected. IP=%s\n",
                  WiFi.localIP().toString().c_str());
    if (gBoardIdValid) {
      setStatus(STATUS_WIFI_CONNECTED_VALID);
    }
  } else {
    Serial.println("\n[WiFi] FAILED to reconnect.");
    setStatus(STATUS_WIFI_DISCONNECTED);
  }
}

// ===== Batch tuner =====
static const size_t URL_CHAR_BUDGET = 1700;
static const uint8_t MAX_BATCHES_PER_POLL = 6;

size_t computeBatchEnd(size_t i) {
  const char *base = kPayloadBase;
  size_t baseLen = strlen(base) + strlen("?board_id=") +
                   gConfig.boardId.length() + strlen("&only=");

  size_t used = baseLen;
  size_t j = i;

  while (j < NB) {
    const char *s = B[j].station;
    size_t need = (j == i) ? 0 : 1;
    while (*s) {
      char c = *s++;
      if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
          c == '~') {
        need += 1;
      } else {
        need += 3;
      }
    }
    if (used + need > URL_CHAR_BUDGET)
      break;
    used += need;
    j++;
  }

  if (j == i)
    j++;
  return j;
}

int fetchAllBatchesAndRenderOnce(bool allowRender) {
  if (NB == 0)
    return 0;

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
    url += "?board_id=";
    url += gConfig.boardId;
    url += "&only=";
    for (size_t j = i; j < end; j++) {
      if (j > i)
        url += ',';
      url += enc(B[j].station);
    }

    HTTPClient http;
    http.setTimeout(20000);
    http.setReuse(false);
    http.useHTTP10(false);
    http.addHeader("Accept", "application/json");
    http.addHeader("Accept-Encoding", "identity");
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Connection", "close");
    http.addHeader("User-Agent", "ESP32-Metroboard/solid/1.3");

    if (!http.begin(client, url)) {
      http.end();
      break;
    }

    const char *hk[] = {"Content-Type", "Content-Encoding"};
    http.collectHeaders(hk, 2);

    Serial.printf("[BATCH] GET %s\n", url.c_str());

    int code = http.GET();
    if (code != HTTP_CODE_OK) {
      http.end();
      break;
    }

    if (http.header("Content-Encoding").indexOf("gzip") >= 0 ||
        http.header("Content-Type").indexOf("application/json") < 0) {
      http.end();
      break;
    }

    String body = http.getString();
    http.end();
    body.trim();

    char c0 = body.length() ? body.charAt(0) : '\0';
    if (c0 != '{' && c0 != '[')
      break;

    gDoc.clear();
    if (deserializeJson(gDoc, body))
      break;

    JsonObject root = gDoc.as<JsonObject>();

    uint32_t ver = root["settings_version"] | 0;
    if (!settingsApplied &&
        (ver == 0 || ver != lastSettingsVersion ||
         millis() - lastSettingsFetch >= SETTINGS_FETCH_MS)) {
      lastSettingsVersion = ver;
      lastSettingsFetch = millis();

      const char *mode = root["mode"] | "live";
      gMode = String(mode);
      gMode.toLowerCase();

      int b = root["brightness"] | 16;
      b = constrain(b, 0, 255);
      int bEff = root["brightness_effective"] | b;
      bEff = constrain(bEff, 0, 255);
      gBaseBrightness = b;
      gEffectiveBrightness = (uint8_t)bEff;

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
        if (col.size() >= 3) {
          gAnim.r = uint8_t(col[0]);
          gAnim.g = uint8_t(col[1]);
          gAnim.b = uint8_t(col[2]);
        }
      }

      settingsApplied = true;
      Serial.printf(
          "[CFG] ver=%u mode=%s bright=%u eff=%u night=%d active=%d anim=%s\n",
          ver, gMode.c_str(), gBaseBrightness, gEffectiveBrightness,
          gNightMode.enabled, gNightMode.active, gAnim.name.c_str());
    }

    JsonVariantConst states = root["states"];
    applyBatchToState(states);

    ok++;
    fired++;
    i = end;

    Serial.printf("[BATCH] HTTP %d  (%d bytes)\n", code, body.length());

    delay(5);
    yield();
    vTaskDelay(1 / portTICK_PERIOD_MS);
  }

  if (allowRender) {
    if (ok > 0) {
      repaintDirtyLinesAndDecayOnce();
    } else {
      renderAll();
    }
  }

  return ok;
}

// ===== setup / loop =====
uint32_t lastPoll = 0, pollInterval = POLL_MS_MIN;
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
  String url = String(kPayloadBase);
  int slash = url.lastIndexOf('/');
  if (slash > 0) {
    url = url.substring(0, slash);
    url += "/board_settings?board_id=";
  } else {
    url += "/board_settings?board_id=";
  }
  url += enc(gConfig.boardId.c_str());

  if (!http.begin(client, url)) {
    http.end();
    return true;
  }

  int code = http.GET();
  http.end();

  if (code == HTTP_CODE_OK) {
    return true;
  }
  if (code == HTTP_CODE_BAD_REQUEST || code == HTTP_CODE_NOT_FOUND ||
      code == HTTP_CODE_FORBIDDEN) {
    return false;
  }
  return true;
}

void maybeCheckForOta() {
  uint32_t now = millis();
  if (now - lastOtaCheck < OTA_CHECK_MS) {
    return;
  }
  lastOtaCheck = now;

  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  FirmwareInfo info;
  if (!fetchFirmwareInfo(info)) {
    return;
  }

  if (!isNewerVersion(info.version, kFirmwareVersion)) {
    return;
  }

  Serial.printf("[OTA] Update available: %s -> %s\n",
                kFirmwareVersion, info.version.c_str());

  if (applyOtaUpdate(info)) {
    Serial.println("[OTA] Update applied, rebooting.");
    delay(500);
    ESP.restart();
  } else {
    Serial.println("[OTA] Update failed.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(150);
  Serial.println("\nMetroboard — Solid Station Mode (TTL, fixed colors, "
                 "BRIGHTNESS=16, coalesced)");
  initStatusLed();
  loadConfigFromPrefs();
  if (!gConfig.isValid()) {
    Serial.println("[CFG] Missing settings. Starting setup portal.");
    startConfigPortal();
  }
  if (!wifiConnect(90000)) {
    Serial.println("[CFG] WiFi failed. Starting setup portal.");
    setStatus(STATUS_WIFI_DISCONNECTED);
    startConfigPortal();
  }
  gBoardIdValid = isBoardIdValid();
  if (!gBoardIdValid) {
    Serial.println("[CFG] Board ID invalid. Starting setup portal.");
    setStatus(STATUS_BOARD_INVALID);
    startConfigPortal();
  }
  setStatus(STATUS_WIFI_CONNECTED_VALID);
  beginStrips();
  buildBindings();
  renderAll();

  xTaskCreatePinnedToCore(
      [](void *) {
        for (;;) {
          ensureWifi();
          maybeCheckForOta();

          uint32_t now = millis();
          if (gMode != "animation" && now - lastPoll >= pollInterval) {
            lastPoll = now;

            int batches = fetchAllBatchesAndRenderOnce(true);

            if (batches > 0)
              pollInterval = POLL_MS_MIN;
            else {
              uint32_t next = pollInterval + 1500;
              if (next > POLL_MS_MAX)
                next = POLL_MS_MAX;
              pollInterval = next;
            }
          } else if (gMode == "animation" &&
                     now - lastSettingsFetch >= SETTINGS_FETCH_MS) {
            fetchAllBatchesAndRenderOnce(false);
          }

          vTaskDelay(10 / portTICK_PERIOD_MS);
        }
      },
      "pollTask", 8192, nullptr, 1, &gPollTaskHandle, 0);
}

void loop() {
  if (gMode == "animation") {
    runAnimationStep();
    delay(20);
  } else {
    delay(5);
  }
}
