// Metroboard LED Demo — No WiFi, Wave From Centre + Other Patterns

#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

// ===== LEDs =====
#define STATUS_PIN 25
#define BRIGHTNESS 16

enum Line : uint8_t { L_T9, L_T5, L_T2, L_T6, L_T3, L_T4, L_T7, L_T8, L_M1, L_T1, LINE_COUNT };
const uint8_t  STRIP_PINS[LINE_COUNT] = {4,13,14,16,17,18,19,21,22,23};
//                                     T9  T5  T2  T6  T3  T4  T7  T8  M1  T1
const uint16_t STRIP_LEN [LINE_COUNT] = {43, 31, 38,  6, 30, 33,  2, 38, 31, 56};
bool REVERSE[LINE_COUNT] = {0,0,0,0,0,0,1,0,0,0}; // T7 reversed physically

// Rough "centre" LED index per line (for wave-from-centre effect)
const uint16_t CENTER_IDX[LINE_COUNT] = {
  21, // T9   (43)
  15, // T5   (31)
  19, // T2   (38)
   3, // T6   (6)
  15, // T3   (30)
  16, // T4   (33)
   0, // T7   (2)  just use 0
  19, // T8   (38)
  15, // M1   (31)
  28  // T1   (56)
};

// Fixed per-line colours
const uint32_t LINE_COLOR_HEX[LINE_COUNT] = {
  0xE53935, // T9 red
  0xD81B60, // T5 magenta
  0x00ACC1, // T2 teal/cyan
  0xA0522D, // T6 brown
  0xFB8C00, // T3 orange
  0x0D47A1, // T4 dark blue
  0x9E9E9E, // T7 grey
  0x006400, // T8 dark green
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

// ===== helpers =====
static inline uint16_t physIdx(Line ln, uint16_t led){
  return REVERSE[ln] ? uint16_t(STRIP_LEN[ln] - 1 - led) : led;
}

static uint32_t baseLineColor(Line ln){
  uint32_t h = LINE_COLOR_HEX[ln];
  uint8_t r = (h>>16)&0xFF, g = (h>>8)&0xFF, b = h&0xFF;
  return strips[ln].Color(r,g,b);
}

// scale colour by 0..1
static uint32_t scaleColor(uint32_t c, float f){
  if(f <= 0.0f) return 0;
  if(f > 1.0f) f = 1.0f;
  uint8_t r = (c >> 16) & 0xFF;
  uint8_t g = (c >>  8) & 0xFF;
  uint8_t b =  c        & 0xFF;
  r = (uint8_t)(r * f);
  g = (uint8_t)(g * f);
  b = (uint8_t)(b * f);
  return (uint32_t)((r << 16) | (g << 8) | b);
}

// ===== patterns =====

// Pattern 0: expanding wave from "Central" (centre index)
void patternWaveFromCentre(uint32_t tMs){
  const float speed = 0.15f;   // lower = slower wave
  const float width = 4.0f;    // how many LEDs are bright at once

  for(uint8_t ln=0; ln<LINE_COUNT; ln++){
    strips[ln].clear();
    uint16_t centre = CENTER_IDX[ln];
    uint32_t baseCol = baseLineColor((Line)ln);
    uint16_t len = STRIP_LEN[ln];

    for(uint16_t i=0; i<len; i++){
      int d = abs((int)i - (int)centre);
      float phase = (float)tMs * speed / 1000.0f - (float)d;
      float wave = 0.5f * (1.0f + sinf(phase));  // 0..1
      // fade with distance so it looks like a "ring"
      float distFade = expf(-(float)d / width);
      float bright = wave * distFade;
      if(bright < 0.05f) continue;
      uint16_t phys = physIdx((Line)ln, i);
      strips[ln].setPixelColor(phys, scaleColor(baseCol, bright));
    }
    strips[ln].show();
  }
}

// Pattern 1: simple “train” chaser along each line
void patternChase(uint32_t tMs){
  const uint16_t periodMs = 2500; // time for one wrap
  for(uint8_t ln=0; ln<LINE_COUNT; ln++){
    strips[ln].clear();
    uint16_t len = STRIP_LEN[ln];
    uint32_t baseCol = baseLineColor((Line)ln);

    float pos = fmodf((float)tMs / (float)periodMs, 1.0f) * len;
    int head = (int)pos;

    for(int k=-2; k<=2; k++){
      int idx = head + k;
      if(idx < 0 || idx >= (int)len) continue;
      float bright = 1.0f - (float)abs(k)/3.0f;
      if(bright < 0.2f) bright = 0.2f;
      uint16_t phys = physIdx((Line)ln, (uint16_t)idx);
      strips[ln].setPixelColor(phys, scaleColor(baseCol, bright));
    }
    strips[ln].show();
  }
}

// Pattern 2: breathing / pulsing whole lines
void patternBreathing(uint32_t tMs){
  const float speed = 0.0015f; // breathing speed
  float phase = (float)tMs * speed;
  float breath = 0.5f * (1.0f + sinf(phase)); // 0..1
  breath = 0.2f + breath * 0.8f;              // never fully off

  for(uint8_t ln=0; ln<LINE_COUNT; ln++){
    strips[ln].clear();
    uint32_t baseCol = baseLineColor((Line)ln);
    uint16_t len = STRIP_LEN[ln];
    uint32_t col = scaleColor(baseCol, breath);
    for(uint16_t i=0; i<len; i++){
      uint16_t phys = physIdx((Line)ln, i);
      strips[ln].setPixelColor(phys, col);
    }
    strips[ln].show();
  }
}

// ===== pattern selector =====
const uint32_t PATTERN_DURATION_MS = 15000; // 15s per mode

void runPatterns(){
  uint32_t now = millis();
  uint32_t block = now / PATTERN_DURATION_MS;
  uint8_t pattern = block % 3;

  switch(pattern){
    case 0: patternWaveFromCentre(now); break;
    case 1: patternChase(now);          break;
    case 2: patternBreathing(now);      break;
  }
}

// ===== setup / loop =====
void beginStrips(){
  statusStrip.begin();
  statusStrip.setBrightness(BRIGHTNESS);
  statusStrip.setPixelColor(0, statusStrip.Color(0, 0, 50)); // demo mode blue
  statusStrip.show();

  for(uint8_t i=0;i<LINE_COUNT;i++){
    strips[i].begin();
    strips[i].setBrightness(BRIGHTNESS);
    strips[i].clear();
    strips[i].show();
  }
}

void setup(){
  Serial.begin(115200);
  delay(150);
  Serial.println("\nMetroboard LED Demo — no WiFi, patterns demo");
  randomSeed(analogRead(0));
  beginStrips();
}

void loop(){
  runPatterns();
  // small delay to avoid hammering the strips
  delay(20);
}
