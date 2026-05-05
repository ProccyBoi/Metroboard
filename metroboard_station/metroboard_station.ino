// Metroboard — Serial Station Tester (next / previous only)
// BRIGHTNESS=16. Type 'n' for next, 'p' for previous.

#include <Adafruit_NeoPixel.h>

#define BRIGHTNESS 16
enum Line:uint8_t{L_T9,L_T5,L_T2,L_T6,L_T3,L_T4,L_T7,L_T8,L_M1,L_T1,LINE_COUNT};
const char* LINE_NAME[LINE_COUNT]={"T9","T5","T2","T6","T3","T4","T7","T8","M1","T1"};

const uint8_t  STRIP_PINS[LINE_COUNT]={4,13,14,16,17,18,19,21,22,23};
//                                     T9  T5  T2  T6  T3  T4  T7  T8  M1  T1
const uint16_t STRIP_LEN [LINE_COUNT]={43, 31, 38,  6, 30, 33,  2, 38, 31, 66}; // T5↑31, T2=38, T8↑38
bool REVERSE[LINE_COUNT]={0,0,0,0,0,0,1,0,0,0}; // T7 reversed physically
const uint32_t LINE_COLOR_HEX[LINE_COUNT] = {
  0xE53935, // T9 red
  0xD81B60, // T5 magenta
  0x00ACC1, // T2 teal/cyan
  0xA0522D, // T6 brown (Sienna, LED-friendly)
  0xFB8C00, // T3 orange
  0x0D47A1, // T4 dark blue
  0x9E9E9E, // T7 grey
  0x006400, // T8 dark green (pure green, LED-friendly)
  0x26C6DA, // M1 light teal
  0xF39C12  // T1 yellow/orange
};

Adafruit_NeoPixel strips[LINE_COUNT]={
  Adafruit_NeoPixel(STRIP_LEN[0],STRIP_PINS[0],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[1],STRIP_PINS[1],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[2],STRIP_PINS[2],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[3],STRIP_PINS[3],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[4],STRIP_PINS[4],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[5],STRIP_PINS[5],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[6],STRIP_PINS[6],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[7],STRIP_PINS[7],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[8],STRIP_PINS[8],NEO_GRB+NEO_KHZ800),
  Adafruit_NeoPixel(STRIP_LEN[9],STRIP_PINS[9],NEO_GRB+NEO_KHZ800)
};

struct LedBinding{const char* station;Line line;uint16_t led_phys;};
#define MAX_BINDINGS 500
LedBinding B[MAX_BINDINGS]; size_t NB=0;

// ==== sequences ====
// T9 — include both your Normanhurst→Epping segment and a Gordon segment so nothing is “missing”
// T9 — Normanhurst → Epping → (Main Northern) → City Circle
const char* const T9_SEQ[] = {
  // Upper Northern
  "Hornsby","Normanhurst","Thornleigh","Pennant Hills","Beecroft","Cheltenham","Epping",
  // Main Northern toward Strathfield
  "Eastwood","Denistone","West Ryde","Meadowbank","Rhodes","Concord West","North Strathfield","Strathfield","Burwood",
  // Inner West to the City
  "Redfern","Central","Town Hall","Wynyard","Milsons Point","North Sydney","Waverton","Wollstonecraft","St Leonards","Artarmon","Chatswood","Roseville","Lindfield","Killara","Gordon"
};


// T5 — add East Richmond; keep Liverpool; (Granville remains removed)
const char* const T5_SEQ[]={
  "Richmond","East Richmond",
  "Clarendon","Windsor","Mulgrave","Vineyard","Riverstone","Schofields","Quakers Hill","Marayong","Blacktown",
  "Seven Hills","Toongabbie","Pendle Hill","Wentworthville","Westmead","Parramatta","Harris Park",
  /*(Granville removed)*/ "Merrylands","Guildford","Yennora","Fairfield","Canley Vale","Cabramatta",
  "Warwick Farm","Liverpool","Casula","Glenfield","Edmondson Park","Leppington"
};

// T2 — unchanged (perfect) incl. Macdonaldtown
const char* const T2_SEG1[]={
  "Leppington","Edmondson Park","Glenfield","Casula","Liverpool","Warwick Farm","Cabramatta","Canley Vale","Fairfield","Yennora","Guildford","Merrylands"
};
const char* const T2_SEG2[]={
  "Parramatta","Harris Park","Granville","Clyde","Auburn","Lidcombe","Flemington","Homebush",
  "Strathfield","Burwood","Croydon","Ashfield","Summer Hill","Lewisham","Petersham","Stanmore","Newtown",
  "Macdonaldtown","Redfern","Central","Town Hall","Wynyard","Circular Quay","St James","Museum"
};

// T6 — unchanged
const char* const T6_SEQ[]={"Bankstown","Yagoona","Birrong","Regents Park","Berala","Lidcombe"};

// T3 — unchanged (perfect) incl. Macdonaldtown
const char* const T3_SEQ[]={
  "Liverpool","Warwick Farm","Cabramatta","Carramar","Villawood","Leightonfield","Chester Hill","Sefton","Regents Park",
  "Berala","Lidcombe","Flemington","Homebush","Strathfield","Burwood","Croydon","Ashfield","Summer Hill","Lewisham","Petersham",
  "Stanmore","Newtown","Macdonaldtown","Redfern","Central","Town Hall","Wynyard","Circular Quay","St James","Museum"
};

// T4 — unchanged
const char* const T4_SEQ[]={
  "Waterfall","Heathcote","Engadine","Loftus",
  "Cronulla","Woolooware","Caringbah","Miranda","Gymea","Kirrawee",
  "Sutherland","Jannali","Como","Oatley","Mortdale","Penshurst","Hurstville","Allawah","Carlton","Kogarah","Rockdale","Banksia","Arncliffe",
  "Wolli Creek","Tempe","Sydenham","Redfern","Central","Town Hall","Martin Place","Kings Cross","Edgecliff","Bondi Junction"
};

// T7 — unchanged sequence; physical reverse via REVERSE flag
const char* const T7_SEQ[]={"Lidcombe","Olympic Park"};

// T8 — include Wynyard, Circular Quay, St James between Town Hall and Museum
const char* const T8_SEQ[]={
  "Macarthur","Campbelltown","Leumeah","Minto","Ingleburn","Macquarie Fields","Glenfield","Holsworthy","East Hills","Panania","Revesby",
  "Padstow","Riverwood","Narwee","Beverly Hills","Kingsgrove","Bexley North","Bardwell Park","Turrella",
  "Sydenham","St Peters","Erskineville","Redfern","Wolli Creek",
  "International Airport","Domestic Airport","Mascot","Green Square",
  "Central","Town Hall","Wynyard","Circular Quay","St James","Museum"
};

// M1 — add Central (and keep Martin Place ↔ Gadigal swapped as requested)
const char* const M1_SEQ[]={
  "Tallawong","Rouse Hill","Kellyville","Bella Vista","Norwest","Hills Showground","Castle Hill","Cherrybrook",
  "Epping","Macquarie University","Macquarie Park","North Ryde","Chatswood",
  "Crows Nest","Victoria Cross","Barangaroo","Martin Place","Gadigal","Central","Waterloo",
  "Sydenham","Marrickville","Dulwich Hill","Hurlstone Park","Canterbury","Campsie","Belmore","Lakemba","Wiley Park","Punchbowl","Bankstown"
};

// T1 — unchanged from your tester (no Homebush/Flemington)
const char* const T1_SEQ[]={
  "Berowra","Mount Kuring-gai","Mount Colah","Asquith","Hornsby","Waitara","Wahroonga","Warrawee","Turramurra","Pymble","Gordon","Killara","Lindfield","Roseville","Chatswood","Artarmon","St Leonards","Wollstonecraft","Waverton","North Sydney","Milsons Point","Wynyard","Town Hall","Central","Redfern",
  "Strathfield","Lidcombe","Auburn","Clyde","Granville","Harris Park","Parramatta","Westmead","Wentworthville","Pendle Hill","Toongabbie","Seven Hills","Blacktown",
  "Doonside","Rooty Hill","Mount Druitt","St Marys","Werrington","Kingswood","Penrith","Emu Plains","Marayong","Quakers Hill","Schofields","Riverstone","Vineyard","Mulgrave","Windsor",
  "Clarendon","East Richmond","Richmond"
};

static inline uint16_t physIdx(Line ln,uint16_t logical){return REVERSE[ln]?uint16_t(STRIP_LEN[ln]-1-logical):logical;}
void addBindSeq(Line ln,const char* const* seq,size_t n,uint16_t start=0){
  for(size_t i=0;i<n;i++){uint16_t p=physIdx(ln,start+i);B[NB++]={seq[i],ln,p};}
}

void buildBindings(){
  addBindSeq(L_T9,T9_SEQ,sizeof(T9_SEQ)/sizeof(*T9_SEQ));
  addBindSeq(L_T5,T5_SEQ,sizeof(T5_SEQ)/sizeof(*T5_SEQ));
  addBindSeq(L_T2,T2_SEG1,sizeof(T2_SEG1)/sizeof(*T2_SEG1),0);
  addBindSeq(L_T2,T2_SEG2,sizeof(T2_SEG2)/sizeof(*T2_SEG2),12);
  addBindSeq(L_T6,T6_SEQ,sizeof(T6_SEQ)/sizeof(*T6_SEQ));
  addBindSeq(L_T3,T3_SEQ,sizeof(T3_SEQ)/sizeof(*T3_SEQ));
  addBindSeq(L_T4,T4_SEQ,sizeof(T4_SEQ)/sizeof(*T4_SEQ));
  addBindSeq(L_T7,T7_SEQ,sizeof(T7_SEQ)/sizeof(*T7_SEQ));
  addBindSeq(L_T8,T8_SEQ,sizeof(T8_SEQ)/sizeof(*T8_SEQ));
  addBindSeq(L_M1,M1_SEQ,sizeof(M1_SEQ)/sizeof(*M1_SEQ));
  addBindSeq(L_T1,T1_SEQ,sizeof(T1_SEQ)/sizeof(*T1_SEQ));
  Serial.printf("Bindings built: %u\n",(unsigned)NB);
}

uint32_t lineColor(Line ln){
  uint32_t h=LINE_COLOR_HEX[ln];uint8_t r=(h>>16)&0xFF,g=(h>>8)&0xFF,b=h&0xFF;
  return strips[ln].Color(r,g,b);
}
void clearAll(){for(uint8_t ln=0;ln<LINE_COUNT;ln++){strips[ln].clear();strips[ln].show();}}
void showBinding(size_t idx){
  if(idx>=NB) return;
  clearAll();
  strips[B[idx].line].setPixelColor(B[idx].led_phys,lineColor(B[idx].line));
  strips[B[idx].line].show();
  Serial.printf("[%u] Station: %s | Line: %s\n",(unsigned)idx,B[idx].station,LINE_NAME[B[idx].line]);
}

size_t cur=0;

void setup(){
  Serial.begin(115200);
  while(!Serial){}
  for(uint8_t i=0;i<LINE_COUNT;i++){strips[i].begin();strips[i].setBrightness(BRIGHTNESS);strips[i].clear();strips[i].show();}
  buildBindings();
  showBinding(cur);
  Serial.println("Type 'n' or 'p' in Serial Monitor to move.");
}

void loop(){
  if(Serial.available()){
    char c=Serial.read();
    if(c=='n'){cur=(cur+1)%NB;showBinding(cur);}
    else if(c=='p'){cur=(cur==0?NB-1:cur-1);showBinding(cur);}
  }
}
