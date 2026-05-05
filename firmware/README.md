# Metroboard Firmware OTA

Koyeb serves OTA firmware only when this directory contains the binary named in
`manifest.json`.

Workflow:

1. Build/export the ESP32 binary from `metroboard_public.ino`.
2. Copy the exported binary to `firmware/metroboard_public.bin`.
3. Bump both `kFirmwareVersion` in `metroboard_public.ino` and `version` in
   `firmware/manifest.json`.
4. Commit and push. After Koyeb deploys, boards already running OTA-capable
   firmware will see the newer manifest and install the update.

If `metroboard_public.bin` is absent, `/firmware_manifest` safely reports that
no update is available.
