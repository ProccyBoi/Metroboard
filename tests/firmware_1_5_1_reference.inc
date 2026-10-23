// Frozen reference functions from Metroboard firmware 1.5.1.
// Keep this baseline unchanged: host tests compare optimized source against it.

static inline bool legacyUnres(char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
           (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
           c == '~';
}

static String legacyEnc(const char* s) {
    String o;
    while (*s) {
        char c = *s++;
        if (legacyUnres(c))
            o += c;
        else {
            char b[4];
            snprintf(b, sizeof(b), "%%%02X", (unsigned char)c);
            o += b;
        }
    }
    return o;
}

size_t legacyComputeBatchEnd(size_t i) {
    const char* base = kPayloadBase;
    size_t baseLen = strlen(base) + strlen("?board_id=") +
                     gConfig.boardId.length() + strlen("&fw=") +
                     strlen(kFirmwareVersion) + strlen("&build=") +
                     strlen(kBuildLabel) + strlen("&ota=") +
                     gLastOtaResult.length() + strlen("&ota_target=") +
                     gLastOtaTargetVersion.length() + strlen("&only=");

    size_t used = baseLen;
    size_t j = i;

    while (j < NB) {
        const char* s = B[j].station;
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
        if (used + need > URL_CHAR_BUDGET) break;
        used += need;
        j++;
    }

    if (j == i) j++;
    return j;
}

void legacyAppendDeviceQuery(String& url) {
    url += "?board_id=";
    url += gConfig.boardId;
    url += "&fw=";
    url += legacyEnc(kFirmwareVersion);
    url += "&build=";
    url += legacyEnc(kBuildLabel);
    url += "&ota=";
    url += legacyEnc(gLastOtaResult.c_str());
    url += "&ota_target=";
    url += legacyEnc(gLastOtaTargetVersion.c_str());
}

void legacyRepaintDirtyLinesAndDecayOnce() {
    for (uint8_t ln = 0; ln < LINE_COUNT; ln++) {
        strips[ln].setBrightness(gEffectiveBrightness);
        if (!dirtyLine[ln]) continue;
        strips[ln].clear();
        for (size_t i = 0; i < NB; i++) {
            if (B[i].line != (Line)ln) continue;
            uint32_t color = lineColorForState(B[i].line, stateBuf[i], ttlBuf[i]);
            if (color) strips[ln].setPixelColor(B[i].led, color);
        }
        strips[ln].show();
    }
    for (size_t i = 0; i < NB; i++)
        if (stateBuf[i] != 3 && ttlBuf[i] > 0) ttlBuf[i]--;
    clearDirty();
}
