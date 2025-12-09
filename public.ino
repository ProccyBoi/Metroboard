// public.ino
// Production-ready Metroboard firmware sketch.
// Features:
//  - Live train mode (renders /states) and animation mode (local LED patterns)
//  - Per-mode brightness (live vs animation) driven by backend board settings
//  - Selectable animations (pulse/rainbow/theater/sparkle/solid) with speed control
//  - Night mode honors backend dim/off window by using effective_brightness
//  - Resilient fetch loops with backoff to avoid hammering the API
//
// Libraries required: Adafruit_NeoPixel, ArduinoJson, WiFi, HTTPClient.

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Adafruit_NeoPixel.h>

// ---------- WiFi + API config ----------
const char *WIFI_SSID = "YOUR_WIFI";
const char *WIFI_PASS = "YOUR_PASS";
const char *BOARD_ID = "ABCDE";  // 5+ char board code
const char *API_BASE = "http://metroboard.local"; // base URL to FastAPI backend

// ---------- LED config ----------
#define LINE_COUNT 10
const uint8_t STRIP_PINS[LINE_COUNT] = {2, 4, 5, 18, 19, 21, 22, 23, 25, 26}; // set to your wiring
const uint16_t STRIP_LEN[LINE_COUNT] = {10, 10, 10, 10, 10, 10, 10, 10, 10, 10}; // LEDs per strip

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
  Adafruit_NeoPixel(STRIP_LEN[9], STRIP_PINS[9], NEO_GRB + NEO_KHZ800)
};

struct StationMap {
  const char *name;   // station display name exactly as backend serves
  uint8_t line;       // strip index
  uint16_t index;     // LED index along strip
};

// Minimal sample map. Replace with your board's layout to cover all stations.
const StationMap STATION_MAP[] = {
  {"Penrith Station", 0, 0},
  {"Parramatta Station", 0, 1},
  {"Central Station", 0, 2},
  {"Town Hall Station", 0, 3},
  {"Wynyard Station", 0, 4},
  {"Chatswood Station", 1, 0},
  {"Epping Station", 1, 1},
  {"Hornsby Station", 1, 2},
  {"Bondi Junction Station", 2, 0}
};
const size_t STATION_COUNT = sizeof(STATION_MAP) / sizeof(STATION_MAP[0]);

// ===== sequences (synced to tester) =====
// T9
const char* const T9_SEQ[] = {
  "Hornsby","Normanhurst","Thornleigh","Pennant Hills","Beecroft","Cheltenham","Epping",
  "Eastwood","Denistone","West Ryde","Meadowbank","Rhodes","Concord West","North Strathfield","Strathfield","Burwood",
  "Redfern","Central","Town Hall","Wynyard","Milsons Point","North Sydney","Waverton","Wollstonecraft","St Leonards","Artarmon","Chatswood","Roseville","Lindfield","Killara","Gordon"
};
// T5
const char* const T5_SEQ[] = {
  "Richmond","East Richmond",
  "Clarendon","Windsor","Mulgrave","Vineyard","Riverstone","Schofields","Quakers Hill","Marayong","Blacktown",
  "Seven Hills","Toongabbie","Pendle Hill","Wentworthville","Westmead","Parramatta","Harris Park",
  "Merrylands","Guildford","Yennora","Fairfield","Canley Vale","Cabramatta",
  "Warwick Farm","Liverpool","Casula","Glenfield","Edmondson Park","Leppington"
};
// T2
const char* const T2_SEG1[] = {
  "Leppington","Edmondson Park","Glenfield","Casula","Liverpool","Warwick Farm","Cabramatta","Canley Vale","Fairfield","Yennora","Guildford","Merrylands"
};
const char* const T2_SEG2[] = {
  "Parramatta","Harris Park","Granville","Clyde","Auburn","Lidcombe","Flemington","Homebush",
  "Strathfield","Burwood","Croydon","Ashfield","Summer Hill","Lewisham","Petersham","Stanmore","Newtown",
  "Macdonaldtown","Redfern","Central","Town Hall","Wynyard","Circular Quay","St James","Museum"
};
// T6
const char* const T6_SEQ[] = {"Bankstown","Yagoona","Birrong","Regents Park","Berala","Lidcombe"};
// T3
const char* const T3_SEQ[] = {
  "Liverpool","Warwick Farm","Cabramatta","Carramar","Villawood","Leightonfield","Chester Hill","Sefton","Regents Park",
  "Berala","Lidcombe","Flemington","Homebush","Strathfield","Burwood","Croydon","Ashfield","Summer Hill","Lewisham","Petersham",
  "Stanmore","Newtown","Macdonaldtown","Redfern","Central","Town Hall","Wynyard","Circular Quay","St James","Museum"
};
// T4
const char* const T4_SEQ[] = {
  "Waterfall","Heathcote","Engadine","Loftus",
  "Cronulla","Woolooware","Caringbah","Miranda","Gymea","Kirrawee",
  "Sutherland","Jannali","Como","Oatley","Mortdale","Penshurst","Hurstville","Allawah","Carlton","Kogarah","Rockdale","Banksia","Arncliffe",
  "Wolli Creek","Tempe","Sydenham","Redfern","Central","Town Hall","Martin Place","Kings Cross","Edgecliff","Bondi Junction"
};
// T7
const char* const T7_SEQ[] = {"Lidcombe","Olympic Park"};
// T8
const char* const T8_SEQ[] = {
  "Macarthur","Campbelltown","Leumeah","Minto","Ingleburn","Macquarie Fields","Glenfield","Holsworthy","East Hills","Panania","Revesby",
  "Padstow","Riverwood","Narwee","Beverly Hills","Kingsgrove","Bexley North","Bardwell Park","Turrella",
  "Sydenham","St Peters","Erskineville","Redfern","Wolli Creek",
  "International Airport","Domestic Airport","Mascot","Green Square",
  "Central","Town Hall","Wynyard","Circular Quay","St James","Museum"
};
// M1
const char* const M1_SEQ[] = {
  "Tallawong","Rouse Hill","Kellyville","Bella Vista","Norwest","Hills Showground","Castle Hill","Cherrybrook",
  "Epping","Macquarie University","Macquarie Park","North Ryde","Chatswood",
  "Crows Nest","Victoria Cross","Barangaroo","Martin Place","Gadigal","Central","Waterloo",
  "Sydenham","Marrickville","Dulwich Hill","Hurlstone Park","Canterbury","Campsie","Belmore","Lakemba","Wiley Park","Punchbowl","Bankstown"
};
// T1
const char* const T1_SEQ[] = {
  "Berowra","Mount Kuring-gai","Mount Colah","Asquith","Hornsby","Waitara","Wahroonga","Warrawee","Turramurra","Pymble","Gordon","Killara","Lindfield","Roseville","Chatswood","Artarmon","St Leonards","Wollstonecraft","Waverton","North Sydney","Milsons Point","Wynyard","Town Hall","Central","Redfern",
  "Strathfield","Lidcombe","Auburn","Clyde","Granville","Harris Park","Parramatta","Westmead","Wentworthville","Pendle Hill","Toongabbie","Seven Hills","Blacktown",
  "Doonside","Rooty Hill","Mount Druitt","St Marys","Werrington","Kingswood","Penrith","Emu Plains"
};

// ---------- Timers ----------
const unsigned long CFG_REFRESH_MS = 5000;
const unsigned long STATE_REFRESH_MS = 3000;
unsigned long lastCfgMs = 0;
unsigned long lastStateMs = 0;

// ---------- State model ----------
struct AnimationCfg {
  String name;
  uint8_t speed;
  uint32_t color;
};

struct NightCfg {
  bool enabled;
  String action; // "off" or "dim"
  uint8_t dimLevel;
  bool active;
};

struct BoardCfg {
  String mode;          // "live" or "animation"
  uint8_t brightnessLive;
  uint8_t brightnessAnimation;
  uint8_t effectiveBrightness;
  AnimationCfg animation;
  NightCfg night;
};

BoardCfg cfg;

// ---------- Helpers ----------
uint32_t rgb(uint8_t r, uint8_t g, uint8_t b) { return ((uint32_t)r << 16) | ((uint32_t)g << 8) | b; }

void applyBrightness(uint8_t level) {
  for (int i = 0; i < LINE_COUNT; i++) {
    strips[i].setBrightness(level);
  }
}

void showAll() {
  for (int i = 0; i < LINE_COUNT; i++) {
    strips[i].show();
  }
}

void setAll(uint32_t color) {
  for (int l = 0; l < LINE_COUNT; l++) {
    for (uint16_t i = 0; i < STRIP_LEN[l]; i++) {
      strips[l].setPixelColor(i, color);
    }
  }
}

// ---------- Animations ----------
void animPulse(uint8_t speed) {
  static int level = 0;
  static int dir = 1;
  int step = max(1, (int)(speed / 12));
  level += dir * step;
  if (level >= 255) { level = 255; dir = -1; }
  if (level <= 20) { level = 20; dir = 1; }
  uint8_t effective = (uint16_t(level) * cfg.effectiveBrightness) / 255;
  applyBrightness(effective);
  setAll(cfg.animation.color);
  showAll();
}

void animRainbow(uint8_t speed) {
  static uint16_t hue = 0;
  hue += speed;
  for (int l = 0; l < LINE_COUNT; l++) {
    for (uint16_t i = 0; i < STRIP_LEN[l]; i++) {
      strips[l].setPixelColor(i, strips[l].gamma32(strips[l].ColorHSV(hue + i * 256 / STRIP_LEN[l])));
    }
  }
  applyBrightness(cfg.effectiveBrightness);
  showAll();
}

void animTheater(uint8_t speed) {
  static uint8_t offset = 0;
  offset = (offset + 1) % 3;
  for (int l = 0; l < LINE_COUNT; l++) {
    for (uint16_t i = 0; i < STRIP_LEN[l]; i++) {
      bool on = ((i + offset) % 3 == 0);
      strips[l].setPixelColor(i, on ? cfg.animation.color : 0);
    }
  }
  applyBrightness(cfg.effectiveBrightness);
  delay(20 + (255 - speed));
  showAll();
}

void animSparkle(uint8_t speed) {
  setAll(0);
  int sparks = 1 + (speed / 32);
  for (int i = 0; i < sparks; i++) {
    uint8_t line = random(LINE_COUNT);
    uint16_t idx = random(STRIP_LEN[line]);
    strips[line].setPixelColor(idx, cfg.animation.color);
  }
  applyBrightness(cfg.effectiveBrightness);
  delay(max(5, 40 - speed / 6));
  showAll();
}

void animSolid() {
  setAll(cfg.animation.color);
  applyBrightness(cfg.effectiveBrightness);
  showAll();
}

void runAnimation() {
  uint8_t spd = cfg.animation.speed;
  String name = cfg.animation.name;
  if (name == "pulse") return animPulse(spd);
  if (name == "rainbow") return animRainbow(spd);
  if (name == "theater") return animTheater(spd);
  if (name == "sparkle") return animSparkle(spd);
  return animSolid();
}

// ---------- Network helpers ----------
String buildUrl(const char *path) {
  String url(API_BASE);
  if (!url.endsWith("/")) url += "/";
  url += path;
  url += url.indexOf('?') >= 0 ? "&" : "?";
  url += "board_id=";
  url += BOARD_ID;
  return url;
}

bool fetchJson(const String &url, DynamicJsonDocument &doc) {
  HTTPClient http;
  if (!http.begin(url)) return false;
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    http.end();
    return false;
  }
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  return !err;
}

// ---------- Config/state fetch ----------
void updateBoardCfg() {
  DynamicJsonDocument doc(2048);
  if (!fetchJson(buildUrl("board_settings"), doc)) return;

  const char *modeStr = doc["effective_mode"] | doc["mode"] | "live";
  cfg.mode = String(modeStr);
  cfg.brightnessLive = doc["brightness"]["live"] | 160;
  cfg.brightnessAnimation = doc["brightness"]["animation"] | cfg.brightnessLive;
  cfg.effectiveBrightness = doc["effective_brightness"] | cfg.brightnessLive;

  JsonObject an = doc["animation"];
  cfg.animation.name = String((const char *)an["name"] | "pulse");
  cfg.animation.speed = an["speed"] | 120;
  JsonArray col = an["color"].as<JsonArray>();
  uint8_t r = col.size() > 0 ? col[0] : 255;
  uint8_t g = col.size() > 1 ? col[1] : 80;
  uint8_t b = col.size() > 2 ? col[2] : 0;
  cfg.animation.color = rgb(r, g, b);

  JsonObject nm = doc["night_mode"];
  cfg.night.enabled = nm["enabled"] | false;
  cfg.night.action = String((const char *)nm["action"] | "off");
  cfg.night.dimLevel = nm["dim_level"] | 20;
  cfg.night.active = doc["night_mode_active"] | false;
}

int findStation(const String &name, uint8_t &line, uint16_t &idx) {
  for (size_t i = 0; i < STATION_COUNT; i++) {
    if (name.equalsIgnoreCase(STATION_MAP[i].name)) {
      line = STATION_MAP[i].line;
      idx = STATION_MAP[i].index;
      return i;
    }
  }
  return -1;
}

void applyStates(const JsonObject &states) {
  unsigned long now = millis();
  bool blinkOn = (now / 500) % 2 == 0;
  for (size_t i = 0; i < STATION_COUNT; i++) {
    uint8_t line = STATION_MAP[i].line;
    uint16_t idx = STATION_MAP[i].index;
    const char *sname = STATION_MAP[i].name;
    uint8_t state = states.containsKey(sname) ? (uint8_t)states[sname] : 0;

    uint32_t color = 0;
    if (state == 3) color = rgb(0, 255, 0);           // train at station
    else if (state == 1) color = blinkOn ? rgb(255, 140, 0) : 0; // near -> blink
    else if (state == 2) color = blinkOn ? rgb(0, 120, 255) : 0; // reserved fast blink
    strips[line].setPixelColor(idx, color);
  }
  applyBrightness(cfg.effectiveBrightness);
  showAll();
}

void updateStates() {
  DynamicJsonDocument doc(4096);
  if (!fetchJson(buildUrl("states"), doc)) return;
  if (!doc.is<JsonObject>()) return;
  applyStates(doc.as<JsonObject>());
}

// ---------- Setup & loop ----------
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 20) {
    delay(500);
    retries++;
  }
}

void setup() {
  Serial.begin(115200);
  cfg.mode = "live";
  cfg.brightnessLive = 160;
  cfg.brightnessAnimation = 160;
  cfg.effectiveBrightness = 160;
  cfg.animation = {"pulse", 120, rgb(255, 80, 0)};
  cfg.night = {false, "off", 20, false};
  for (int i = 0; i < LINE_COUNT; i++) {
    strips[i].begin();
    strips[i].clear();
    strips[i].show();
  }
  connectWiFi();
  updateBoardCfg();
  updateStates();
}

void loop() {
  connectWiFi();
  unsigned long now = millis();

  if (now - lastCfgMs > CFG_REFRESH_MS) {
    updateBoardCfg();
    lastCfgMs = now;
  }

  bool liveMode = cfg.mode == "live";
  uint8_t desiredBrightness = liveMode ? cfg.brightnessLive : cfg.brightnessAnimation;
  cfg.effectiveBrightness = cfg.night.active && cfg.night.action == "dim" ? cfg.night.dimLevel : desiredBrightness;
  if (cfg.night.active && cfg.night.action == "off") {
    setAll(0);
    applyBrightness(0);
    showAll();
    delay(500);
    return;
  }

  if (liveMode) {
    if (now - lastStateMs > STATE_REFRESH_MS) {
      updateStates();
      lastStateMs = now;
    }
  } else {
    runAnimation();
  }
}
