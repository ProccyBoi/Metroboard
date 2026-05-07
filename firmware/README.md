# Metroboard Firmware OTA

Koyeb serves OTA firmware only when this directory contains the binary named in
`manifest.json`.

Workflow:

1. Edit the tracked root sketch `metroboard_public.ino`.
2. Bump both `kFirmwareVersion` in `metroboard_public.ino` and `version` in
   `firmware/manifest.json`.
3. Run `python -m platformio run -e metroboard_public`; the post-build script
   verifies that the sketch version and manifest version match, then copies the
   binary to `firmware/metroboard_public.bin`.
4. Commit and push. After Koyeb deploys, boards already running OTA-capable
   firmware will see the newer manifest and install the update.

If `metroboard_public.bin` is absent, `/firmware_manifest` safely reports that
no update is available.

Recovery bridge:

- `python -m platformio run -e metroboard_bridge` builds a small OTA-only
  bridge from `src/bridge.cpp`.
- Copy its output to `firmware/metroboard_bridge_1.4.6.bin` only when a board
  is stuck on old firmware that cannot take the full image directly.
- Point that board's OTA filename at the bridge with target version
  `1.4.6-bridge`. Once the bridge boots, the server advertises the global
  `metroboard_public.bin` image so the board can finish upgrading.

Safety checks:

- The server only advertises firmware when the target version is newer than the
  board-reported version.
- Firmware also refuses non-newer versions, invalid MD5 hashes, invalid sizes,
  and firmware URLs outside this service before writing flash.
- Current firmware learns about available updates from the regular
  `/board_payload` response, avoiding a separate periodic manifest request that
  can visibly stall animations.
- OTA writes in small chunks and yields between chunks to avoid the ESP32 task
  watchdog during large downloads.
