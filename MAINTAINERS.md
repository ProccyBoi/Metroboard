# Metroboard Maintainer Guide

This file is for future maintainers who need to change, deploy, debug, or recover
the Metroboard system. It explains what each important file does, how the pieces
fit together, and the lessons learned while fixing route matching, firmware OTA,
and LED reliability problems.

## System Overview

Metroboard has two main parts:

1. The backend in `app.py`, deployed to Koyeb.
2. The ESP32 firmware, currently built from the root `metroboard_public.ino`.

The ESP32 polls Koyeb for `/board_payload`. The backend fetches TfNSW realtime
vehicle positions, maps route IDs to public line names, calculates station LED
states, and returns a compact JSON payload. The ESP32 maps station keys such as
`Central1` or `Blacktown5` to physical NeoPixel LEDs.

Firmware OTA is also served by Koyeb. The ESP32 checks `/firmware_manifest`,
downloads `/firmware.bin` when the manifest version is newer, verifies the MD5,
writes the update, and reboots.

## Important Files

### Backend

- `app.py`
  - Main FastAPI backend.
  - Fetches TfNSW GTFS-Realtime vehicle positions.
  - Loads static GTFS files and station-line mappings.
  - Resolves realtime route IDs such as `IWL`, `NTH`, `NSN`, `CMB`, `T3`, and
    `CTY_*` into public route names.
  - Exposes the board UI and JSON endpoints:
    - `/board_payload`
    - `/board_settings`
    - `/board_status`
    - `/firmware_manifest`
    - `/firmware.bin`
    - `/debug`
  - Lesson learned: route IDs from TfNSW are operational IDs, not always public
    line IDs. Do not assume a prefix maps directly to the line printed on a map.

- `Dockerfile`
  - Builds the Koyeb container.
  - Copies the backend, static GTFS files, station mapping files, boards config,
    and `firmware/` directory into `/app`.
  - Inflates `gtfs/stop_times.txt.gz` during image build.
  - Lesson learned: Koyeb only sees files copied by the Dockerfile. If an endpoint
    depends on a file, make sure the Dockerfile copies it.

- `requirements.txt`
  - Python dependencies for FastAPI, GTFS-Realtime parsing, requests, and form
    handling.

- `boards.json`
  - Default tracked board settings.
  - Used as initial data when no persistent settings are present.

- `data/boards.json`
  - Runtime board settings copy used locally or by persistent deployments.
  - Be careful committing local changes here because they may include live board
    configuration.

### Static GTFS and Station Data

- `stops.txt`, `routes.txt`, `trips.txt`, `agency.txt`
  - Static GTFS files used to build route, trip, station, and stop mappings.

- `gtfs/stop_times.txt.gz`
  - Compressed stop-times data committed for deploy size and reliability.
  - Inflated by `scripts/ensure_stop_times.py` in Docker.
  - Lesson learned: missing or invalid `stop_times.txt` breaks platform and route
    inference. Keep the compressed file valid.

- `station_line_pairs.csv`
  - Important manual station-to-line mapping.
  - The backend uses it to know which station keys are valid for each public line.

- `station_line_pairs.txt`
  - Older/simple station-line mapping format. Keep for compatibility unless the
    backend no longer references it.

- `stations_whitelist.txt`
  - Restricts station loading to the intended board/network scope.

- `assets/`
  - Optional web UI assets.
  - `assets/board_overlay.png`/`.jpg`/`.webp` can be used as the board
    simulator background. The simulator then draws station hit targets over the
    artwork.
  - Lesson learned: an artwork overlay is only as accurate as the crop and
    perspective correction. Use a straight-on image cropped to the board edges.

### Firmware Source

- `metroboard_public.ino`
  - Root firmware source used by PlatformIO through `src/main.cpp`.
  - This should be treated as the primary firmware source of truth for builds.
  - Contains:
    - Wi-Fi setup portal
    - board ID validation
    - LED strip pin/length config
    - station-to-LED binding sequences
    - `/board_payload` polling
    - live/test/animation mode
    - HTTP OTA client
  - Lesson learned: firmware station sequences must match physical LEDs. Do not
    add missing stations unless the physical board has LEDs for them.

- `src/main.cpp`
  - PlatformIO wrapper that includes `../metroboard_public.ino`.
  - Keeps the Arduino-style sketch usable while letting PlatformIO compile it.

- `platformio.ini`
  - PlatformIO build and upload config.
  - Current environment: `metroboard_public`.
  - Uses `esp32dev`, Arduino framework, COM17 for upload/monitor, and
    `min_spiffs.csv` partitioning.
  - Defines firmware libraries and runs the post-build copy script.

- `metroboard_public/metroboard_public.ino`
  - Arduino folder-style firmware variant.
  - At the time this guide was written, it includes extra debug logging and a
    build label. It is not what PlatformIO builds unless `src/main.cpp` is
    changed to include it.

- `metroboard_demo/metroboard_demo.ino`
  - Demo/test sketch variant.

- `metroboard_final/metroboard_final.ino`
  - Older/finalized sketch variant kept for reference.

- `metroboard_station/metroboard_station.ino`
  - Station-mode sketch variant kept for reference.

### Firmware OTA

- `firmware/manifest.json`
  - Declares the OTA firmware version and the binary filename served by Koyeb.
  - The backend reads this file first. The `filename` must match a real file in
    `firmware/`.
  - Current note: PlatformIO copies builds to `firmware/metroboard_public.bin`,
    but the manifest may point at another file if a manual Arduino build is being
    served. Keep the manifest and intended binary aligned.

- `firmware/metroboard_public.bin`
  - Binary produced by PlatformIO from root `metroboard_public.ino`.
  - `scripts/pio_copy_firmware.py` copies it here after each successful
    PlatformIO build.

- `firmware/metroboard_original.ino.bin`
  - Manual Arduino-exported firmware binary currently referenced by
    `firmware/manifest.json` at the time this file was written.

- `firmware/metroboard_debug.ino.bin`, `firmware/metroboard_debug_1.4.2.bin`
  - Debug/manual firmware binaries kept for testing or recovery.

- `firmware/README.md`
  - Short OTA workflow reference.

- `scripts/pio_copy_firmware.py`
  - PlatformIO post-build script.
  - Copies `.pio/build/metroboard_public/firmware.bin` to
    `firmware/metroboard_public.bin`.

### Build and Helper Files

- `scripts/ensure_stop_times.py`
  - Inflates `gtfs/stop_times.txt.gz` to the stop-times path expected by the
    backend.

- `.gitignore`
  - Ignores large/generated local build products such as `.pio/` and inflated
    stop-times files.

- `.gitattributes`
  - Repository attributes. Check before changing line-ending or binary handling.

- `.vscode/extensions.json`
  - Editor recommendation metadata.

- `__pycache__/`
  - Python bytecode cache. It is currently tracked in this repo, but generally
    should not be relied on by the app.

## Normal Workflows

### Backend or Route Logic Change

1. Edit `app.py` or mapping/data files.
2. Run:
   ```bash
   python -m py_compile app.py
   ```
3. Commit and push.
4. Koyeb redeploys automatically.
5. Check:
   ```text
   https://damp-catlin-metroboard-7be2a3b3.koyeb.app/healthz
   https://damp-catlin-metroboard-7be2a3b3.koyeb.app/debug
   ```

### Firmware Change With OTA

1. Edit root `metroboard_public.ino`.
2. Bump `kFirmwareVersion`.
3. Bump `version` in `firmware/manifest.json`.
4. Build:
   ```bash
   python -m platformio run -e metroboard_public
   ```
5. Confirm the intended binary exists and matches the manifest filename.
6. Commit and push.
7. Check the live manifest:
   ```text
   /firmware_manifest?board_id=AB12C&current=<old-version>
   ```

### First USB Flash

OTA cannot install itself onto a board that does not already have OTA-capable
firmware. First flash must be over USB:

```bash
python -m platformio run -e metroboard_public -t upload
```

`platformio.ini` is configured for COM17. If COM17 is not visible, check the USB
cable, driver, board mode, and Windows Device Manager before uploading.

## Route Matching Lessons Learned

- TfNSW realtime route IDs are not always public route names.
- Public route meaning was verified against the AnyTrip endpoints used by
  `https://tfnsw.joshchans.com/`.
- Important mappings:
  - `IWL_*` -> `T2`
  - `NTH_*` -> `T9`
  - `NSN_*` -> `T1`
  - `CMB_*` -> `T5`
  - `T3_*` -> `T3`
  - `APS_*`, `APT_*`, `AIR*`, `ESS*` -> `T8`
  - `RTTA_*` should be ignored as technical/test movements.
- Unknown route handling should be conservative but useful. The current user
  preference is to pick a best route when possible rather than hide every
  ambiguous train.
- T1/T5 around Blacktown and Richmond is especially sensitive because the
  physical board has separate T1 and T5 LEDs for overlapping stations. The
  firmware must request both `Station1` and `Station5` keys where both exist.

## LED Signal Integrity Lessons

LEDs showing random colors, flickering, or looking "cooked" is usually a
hardware signal or power integrity issue, not a route matching issue.

Most likely causes:

- ESP32 data is 3.3 V while many NeoPixel/WS2812 strips are powered from 5 V.
  A 5 V strip may not reliably treat 3.3 V as a valid high signal.
- Data wires are too long or routed near noisy power wiring.
- Missing common ground between ESP32 and LED power supply.
- No series resistor on the data line.
- No bulk capacitor across LED 5 V and ground near the first LED.
- Voltage drop from insufficient power injection.
- Too much brightness/current draw causing brownouts or data corruption.
- Weak USB or supply current during Wi-Fi transmit bursts.

Recommended hardware fixes:

- Use a 74AHCT125 or 74HCT245 level shifter for each data line or for grouped
  data signals. AHCT/HCT parts are preferred because 3.3 V input is recognized
  while output is 5 V.
- Add a 330 to 470 ohm resistor in series with each data line near the ESP32.
- Add a large capacitor near the LED power input, typically 1000 uF or higher,
  rated above the supply voltage.
- Make sure LED ground and ESP32 ground are tied together.
- Keep data wires short. If they must be long, use twisted pair with data and
  ground, or buffer the signal closer to the strip.
- Inject 5 V power at multiple points on long strips.
- Use a power supply sized for worst case LED current, not average current.

Software mitigations:

- Lower brightness to reduce current draw.
- Avoid full-white/full-board animations at high brightness.
- Keep updates batched and avoid unnecessary redraws.
- Add a startup delay before heavy LED output if the power supply ramps slowly.

Software cannot fully fix a marginal data waveform. If the LEDs are random or
wrong even in a simple solid-color test sketch, fix wiring, grounding, level
shifting, or power first.

## Recovery Notes

- If OTA deploys bad firmware that still boots and connects to Wi-Fi, deploy a
  higher version with a fixed binary.
- If OTA breaks Wi-Fi, boot, or the setup portal, recover over USB.
- Keep at least one known-good binary in `firmware/` for rollback.
- Do not delete older manual binaries until the current OTA path has been tested
  on real hardware.
