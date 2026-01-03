// Metroboard — Solid-at-Station with settings+states payload, no flicker, BRIGHTNESS=16, Grace TTL, Coalesced Batch Updates

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include <time.h>

// ===== WiFi / Endpoint =====
#define WIFI_SSID "Bob"
#define WIFI_PASS "96365509"

// NOTE: Use board_payload for combined settings + states
static const char* kBoardId       = "AB12C";   // unique per Metroboard
static const char* kPayloadBase   = "https://damp-catlin-metroboard-7be2a3b3.koyeb.app/board_payload";

static const uint32_t POLL_MS_MIN = 1000, POLL_MS_MAX = 5000;
static const uint32_t SETTINGS_FETCH_MS = 30000; // refresh settings every 30s even if version unchanged

// ===== LEDs =====
#define STATUS_PIN 25
uint8_t gBaseBrightness = 16;       // updated from settings
uint8_t gEffectiveBrightness = 16;  // can later factor in night mode

enum Line : uint8_t { L_T9, L_T5, L_T2, L_T6, L_T3, L_T4, L_T7, L_T8, L_M1, L_T1, LINE_COUNT };
const uint8_t  STRIP_PINS[LINE_COUNT] = {4,13,14,16,17,18,19,21,22,23};
//                                     T9  T5  T2  T6  T3  T4  T7  T8  M1  T1
const uint16_t STRIP_LEN [LINE_COUNT] = {43, 31, 38,  6, 30, 33,  2, 38, 31, 56}; // synced to tester
bool REVERSE[LINE_COUNT] = {0,0,0,0,0,0,1,0,0,0}; // T7 reversed physically

// Fixed per-line colors (LED-friendly palette synced to tester)
const uint32_t LINE_COLOR_HEX[LINE_COUNT] = {
  0xE53935, // T9 red
  0xD81B60, // T5 magenta
  0x00ACC1, // T2 teal/cyan
  0xA0522D, // T6 brown (Sienna, LED-friendly)
  0xFB8C00, // T3 orange
  0x0D47A1, // T4 dark blue
  0x9E9E9E, // T7 grey
  0x006400, // T8 dark green (pure green)
  0x26C6DA, // M1 light teal
  0xF39C12  // T1 yellow/orange
};

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

Adafruit_NeoPixel statusStrip(1, STATUS_PIN, NEO_GRB + NEO_KHZ800);
DynamicJsonDocument gDoc(8192);

String gMode = "live"; // "live" or "animation"

struct NightModeCfg {
  bool enabled;
  String start;   // "HH:MM"
  String end;
  String action;  // "off" or "dim"
  uint8_t dimLevel;
} gNightMode = {false, "23:00", "06:00", "off", 20};

struct AnimCfg {
  String name;   // "pulse", "rainbow", "bounce"
  int speed;     // arbitrary units
  uint8_t r, g, b;
} gAnim = {"pulse", 120, 255, 80, 0};

uint32_t lastSettingsFetch = 0;
uint32_t lastSettingsVersion = 0;

// ===== Binding model =====
struct LedBinding { const char* station; Line line; uint16_t led; };
#define MAX_BINDINGS 500
LedBinding B[MAX_BINDINGS]; size_t NB = 0;

// Persistent state + tiny hold-time after last solid to avoid blink-offs
uint8_t* stateBuf = nullptr;  // 0..3 (we light only when ==3)
uint8_t* ttlBuf   = nullptr;  // polls-to-hold after last 3
static const uint8_t GRACE_POLLS = 3; // a touch longer to mask late batches

// ===== helpers =====
void statusOrange() {
  statusStrip.setPixelColor(0, statusStrip.Color(200, 80, 0)); // orange
  statusStrip.show();
}

void statusGreen() {
  statusStrip.setPixelColor(0, statusStrip.Color(0, 160, 0)); // green
  statusStrip.show();
}

static const char* routeSuffix(Line ln){
  switch(ln){
    case L_T9: return "9";  case L_T5: return "5";  case L_T2: return "2";
    case L_T6: return "6";  case L_T3: return "3";  case L_T4: return "4";
    case L_T7: return "7";  case L_T8: return "8";  case L_M1: return "M";
    case L_T1: return "1";  default:   return "?";
  }
}
static char* makeKey(const char* base, Line ln){
  const char* suf = routeSuffix(ln);
  size_t lb = strlen(base), ls = strlen(suf);
  char* out = (char*)malloc(lb + ls + 1);
  if(!out) return nullptr;
  memcpy(out, base, lb);
  memcpy(out + lb, suf, ls);
  out[lb + ls] = '\0';
  return out;
}
static inline uint16_t physIdx(Line ln, uint16_t led){
  return REVERSE[ln] ? uint16_t(STRIP_LEN[ln] - 1 - led) : led;
}
void addBind(const char* base, Line ln, uint16_t led){
  if(NB >= MAX_BINDINGS) return;
  uint16_t idx = physIdx(ln, led);
  if(idx >= STRIP_LEN[ln]) return;
  char* key = makeKey(base, ln);
  if(!key) return;
  B[NB++] = { key, ln, idx };
}
void bindSeq(Line ln, const char* const* seq, size_t n, uint16_t start=0){
  for(size_t i=0;i<n;i++) addBind(seq[i], ln, start + (uint16_t)i);
}
static inline bool unres(char c){
  return (c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='-'||c=='_'||c=='.'||c=='~';
}
static String enc(const char* s){
  String o; while(*s){ char c=*s++;
    if(unres(c)) o+=c; else { char b[4]; snprintf(b,sizeof(b),"%%%02X",(unsigned char)c); o+=b; }
  } return o;
}
static uint32_t lineColor(Line ln){
  uint32_t h = LINE_COLOR_HEX[ln];
  uint8_t r = (h>>16)&0xFF, g = (h>>8)&0xFF, b = h&0xFF;
  return strips[ln].Color(r,g,b);
}
static uint32_t wheel(uint8_t pos){
  pos = 255 - pos;
  if(pos < 85)   return strips[0].Color(255 - pos * 3, 0, pos * 3);
  if(pos < 170){ pos -= 85; return strips[0].Color(0, pos * 3, 255 - pos * 3); }
  pos -= 170;    return strips[0].Color(pos * 3, 255 - pos * 3, 0);
}

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

// ===== build & render =====
void runAnimationStep(){
  static uint32_t lastChaseStep = 0;
  static uint16_t chasePos[LINE_COUNT];
  static int8_t chaseDir[LINE_COUNT];
  static bool chaseInit = false;
  static String lastAnim = "";

  uint32_t now = millis();

  // Reset per-animation state when switching animations
  if(!lastAnim.equalsIgnoreCase(gAnim.name)){
    chaseInit = false;
    lastAnim = gAnim.name;
  }

  // Normalize name for comparisons
  String anim = gAnim.name;
  anim.toLowerCase();

  uint32_t speed = (gAnim.speed <= 0) ? 100 : (uint32_t)gAnim.speed;

  // ----- Rainbow wheel -----
  if(anim == "rainbow"){
    uint32_t shift = now / speed;
    for(uint8_t ln=0; ln<LINE_COUNT; ln++){
      strips[ln].setBrightness(gEffectiveBrightness);
      for(uint16_t i=0; i<STRIP_LEN[ln]; i++){
        uint8_t hue = (uint8_t)((i * 256 / STRIP_LEN[ln] + shift) & 0xFF);
        strips[ln].setPixelColor(i, wheel(hue));
      }
      strips[ln].show();
    }
    return;
  }

  // ----- Bouncing chase (3-LED train) -----
  if(anim == "bounce" || anim == "chase"){
    if(!chaseInit){
      for(uint8_t ln=0; ln<LINE_COUNT; ln++){ chasePos[ln] = 0; chaseDir[ln] = 1; }
      chaseInit = true;
      lastChaseStep = now;
    }

    if(now - lastChaseStep >= speed){
      lastChaseStep = now;
      for(uint8_t ln=0; ln<LINE_COUNT; ln++){
        uint16_t len = STRIP_LEN[ln];
        chasePos[ln] = uint16_t(int16_t(chasePos[ln]) + chaseDir[ln]);
        if(chasePos[ln] == 0 || chasePos[ln] + 1 >= len){
          chaseDir[ln] = -chaseDir[ln];
        }
      }
    }

    for(uint8_t ln=0; ln<LINE_COUNT; ln++){
      strips[ln].setBrightness(gEffectiveBrightness);
      strips[ln].clear();
      uint32_t col = lineColor((Line)ln);
      int16_t center = chasePos[ln];
      for(int k=-1; k<=1; k++){
        int16_t idx = center + k;
        if(idx>=0 && idx < (int16_t)STRIP_LEN[ln]){
          strips[ln].setPixelColor((uint16_t)idx, col);
        }
      }
      strips[ln].show();
    }
    return;
  }

  // ----- Pulse (default) -----
  uint32_t cycle = speed * 20;
  float t = cycle ? float(now % cycle) / float(cycle) : 0.0f;
  float intensity = 0.5f + 0.5f * sinf(2.0f * 3.14159f * t);

  uint8_t br = (uint8_t)(gEffectiveBrightness * intensity);
  for(uint8_t ln=0; ln<LINE_COUNT; ln++){
    strips[ln].setBrightness(br);
    for(uint16_t i=0; i<STRIP_LEN[ln]; i++){
      strips[ln].setPixelColor(i, strips[ln].Color(gAnim.r, gAnim.g, gAnim.b));
    }
    strips[ln].show();
  }
}

void buildBindings(){
  bindSeq(L_T9, T9_SEQ, sizeof(T9_SEQ)/sizeof(*T9_SEQ));
  bindSeq(L_T5, T5_SEQ, sizeof(T5_SEQ)/sizeof(*T5_SEQ));
  bindSeq(L_T2, T2_SEG1, sizeof(T2_SEG1)/sizeof(*T2_SEG1), 0);
  bindSeq(L_T2, T2_SEG2, sizeof(T2_SEG2)/sizeof(*T2_SEG2), 12);
  bindSeq(L_T6, T6_SEQ, sizeof(T6_SEQ)/sizeof(*T6_SEQ));
  bindSeq(L_T3, T3_SEQ, sizeof(T3_SEQ)/sizeof(*T3_SEQ));
  bindSeq(L_T4, T4_SEQ, sizeof(T4_SEQ)/sizeof(*T4_SEQ));
  bindSeq(L_T7, T7_SEQ, sizeof(T7_SEQ)/sizeof(*T7_SEQ));
  bindSeq(L_T8, T8_SEQ, sizeof(T8_SEQ)/sizeof(*T8_SEQ));
  bindSeq(L_M1, M1_SEQ, sizeof(M1_SEQ)/sizeof(*M1_SEQ));
  bindSeq(L_T1, T1_SEQ, sizeof(T1_SEQ)/sizeof(*T1_SEQ));

  stateBuf = (uint8_t*)malloc(NB?NB:1);
  ttlBuf   = (uint8_t*)malloc(NB?NB:1);
  if(stateBuf) memset(stateBuf, 0, NB);
  if(ttlBuf)   memset(ttlBuf, 0, NB);
  Serial.printf("Bindings=%u\n", (unsigned)NB);
}

bool dirtyLine[LINE_COUNT] = {0};
inline void clearDirty(){ for(uint8_t i=0;i<LINE_COUNT;i++) dirtyLine[i]=false; }

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

void repaintDirtyLinesAndDecayOnce(){
  statusStrip.setBrightness(gEffectiveBrightness);
  for(uint8_t ln=0; ln<LINE_COUNT; ln++){
    strips[ln].setBrightness(gEffectiveBrightness);
    if (!dirtyLine[ln]) continue;
    strips[ln].clear();
    for(size_t i=0;i<NB;i++){
      if(B[i].line != (Line)ln) continue;
      bool on = (stateBuf[i]==3) || (ttlBuf[i]>0);
      if(on) strips[ln].setPixelColor(B[i].led, lineColor(B[i].line));
    }
    strips[ln].show();
  }
  for(size_t i=0;i<NB;i++) if(stateBuf[i]!=3 && ttlBuf[i]>0) ttlBuf[i]--;
  clearDirty();
}

void renderAll(){
  statusStrip.setBrightness(gEffectiveBrightness);
  for(uint8_t ln=0; ln<LINE_COUNT; ln++){
    strips[ln].setBrightness(gEffectiveBrightness);
    strips[ln].clear();
  }
  for(size_t i=0;i<NB;i++){
    bool on = (stateBuf[i]==3) || (ttlBuf[i]>0);
    if(on) strips[B[i].line].setPixelColor(B[i].led, lineColor(B[i].line));
  }
  for(uint8_t ln=0; ln<LINE_COUNT; ln++) strips[ln].show();
  for(size_t i=0;i<NB;i++) if(stateBuf[i]!=3 && ttlBuf[i]>0) ttlBuf[i]--;
}

// ===== networking & polling =====
void wifiConnect(){
  WiFi.mode(WIFI_STA); WiFi.setSleep(false); WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("WiFi: \"%s\"", WIFI_SSID);
  while(WiFi.status() != WL_CONNECTED){ delay(300); Serial.print('.'); }
  Serial.printf("\nIP=%s heap=%u\n", WiFi.localIP().toString().c_str(), ESP.getFreeHeap());
  statusGreen();
}
void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    statusGreen();
    return;
  }

  Serial.println("[WiFi] LOST connection. Reconnecting...");
  statusOrange();

  WiFi.disconnect(true,true);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) {
    Serial.print('.');
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] Reconnected. IP=%s\n",
                  WiFi.localIP().toString().c_str());
    statusGreen();
  } else {
    Serial.println("\n[WiFi] FAILED to reconnect.");
  }
}

// ===== Batch tuner =====
static const size_t URL_CHAR_BUDGET = 1700;    // safe budget for GET URL length
static const uint8_t MAX_BATCHES_PER_POLL = 6; // fetch more per tick to speed things up

size_t computeBatchEnd(size_t i){
  // base: .../board_payload?board_id=ABCDE&only=
  const char* base = kPayloadBase;
  size_t baseLen =
      strlen(base)
      + strlen("?board_id=")
      + strlen(kBoardId)
      + strlen("&only=");

  size_t used = baseLen;
  size_t j = i;

  while(j < NB){
    const char* s = B[j].station;
    size_t need = (j == i) ? 0 : 1; // comma
    while(*s){
      char c = *s++;
      if ((c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')||
          c=='-'||c=='_'||c=='.'||c=='~')
      {
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

  if (j == i) j++; // always take at least one
  return j;
}

int fetchAllBatchesAndRenderOnce(){
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
    url += "?board_id=";
    url += kBoardId;
    url += "&only=";
    for (size_t j = i; j < end; j++) {
      if (j > i) url += ',';
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

    const char* hk[] = {"Content-Type", "Content-Encoding"};
    http.collectHeaders(hk, 2);

    Serial.printf("[BATCH] GET %s\n", url.c_str());

    int code = http.GET();
    if (code != HTTP_CODE_OK) {
      http.end();
      break;
    }

    if (http.header("Content-Encoding").indexOf("gzip") >= 0 ||
        http.header("Content-Type").indexOf("application/json") < 0)
    {
      http.end();
      break;
    }

    String body = http.getString();
    http.end();
    body.trim();

    char c0 = body.length() ? body.charAt(0) : '\0';
    if (c0 != '{' && c0 != '[') break;

    gDoc.clear();
    if (deserializeJson(gDoc, body)) break;

    JsonObject root = gDoc.as<JsonObject>();

    // ----- Apply settings if version changed or not yet applied -----
    uint32_t ver = root["settings_version"] | 0;
    if (!settingsApplied && (ver == 0 || ver != lastSettingsVersion || millis() - lastSettingsFetch >= SETTINGS_FETCH_MS)) {
      lastSettingsVersion = ver;
      lastSettingsFetch = millis();

      const char* mode = root["mode"] | "live";
      gMode = String(mode);
      gMode.toLowerCase();

      int b = root["brightness"] | 16;
      b = constrain(b, 0, 255);
      gBaseBrightness = b;
      gEffectiveBrightness = gBaseBrightness; // adjust later if you add time-based dimming

      JsonObject nm = root["night_mode"];
      if (!nm.isNull()) {
        gNightMode.enabled = nm["enabled"] | false;
        gNightMode.start   = nm["start"]   | "23:00";
        gNightMode.end     = nm["end"]     | "06:00";
        gNightMode.action  = nm["action"]  | "off";
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
      Serial.printf("[CFG] ver=%u mode=%s bright=%u night=%d anim=%s\n",
        ver, gMode.c_str(), gBaseBrightness, gNightMode.enabled, gAnim.name.c_str());
    }

    // ----- Apply states -----
    JsonVariantConst states = root["states"];
    applyBatchToState(states);

    ok++;
    fired++;
    i = end;

    Serial.printf("[BATCH] HTTP %d  (%d bytes)\n", code, body.length());

    delay(40);
    yield();
  }

  if (ok > 0) {
    repaintDirtyLinesAndDecayOnce();
  } else {
    renderAll();
  }

  return ok;
}

// ===== setup / loop =====
uint32_t lastPoll=0, pollInterval=POLL_MS_MIN;

void beginStrips(){
  statusStrip.begin(); statusStrip.setBrightness(gEffectiveBrightness);
  statusOrange();
  statusStrip.show();
  for(uint8_t i=0;i<LINE_COUNT;i++){
    strips[i].begin(); strips[i].setBrightness(gEffectiveBrightness);
    strips[i].clear(); strips[i].show();
  }
}

void setup(){
  Serial.begin(115200); delay(150);
  Serial.println("\nMetroboard — Solid Station Mode (TTL, fixed colors, BRIGHTNESS=16, coalesced)");
  wifiConnect(); beginStrips(); buildBindings(); renderAll();
}

void loop(){
  ensureWifi();

  // Animation mode: no live polling, just effect
  if (gMode == "animation") {
    runAnimationStep();
    delay(20);
    // Periodically refresh settings/version even in animation mode
    if (millis() - lastSettingsFetch >= SETTINGS_FETCH_MS) {
      fetchAllBatchesAndRenderOnce(); // will update settings_version/brightness/etc.
    }
    return;
  }

  // Live mode: poll states+settings together
  if(millis()-lastPoll >= pollInterval){
    lastPoll = millis();

    statusStrip.setPixelColor(0, statusStrip.Color(0,80,0));
    statusStrip.show();

    int batches = fetchAllBatchesAndRenderOnce();

    statusStrip.setPixelColor(0, statusStrip.Color(0,160,0));
    statusStrip.show();

    if(batches>0) pollInterval = POLL_MS_MIN;
    else {
      uint32_t next = pollInterval + 1500; if(next > POLL_MS_MAX) next = POLL_MS_MAX;
      pollInterval = next;
    }
  }
}
