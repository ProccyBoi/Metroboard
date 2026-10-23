# Metroboard

Metroboard is a FastAPI service that converts Transport for NSW (TfNSW) GTFS-Realtime vehicle positions into simple per-station LED status states for Sydney Trains and Metro boards. It filters the static GTFS feed to rail/metro routes, builds station+line keys (for example `Epping9` or `Central1`), and exposes JSON endpoints plus a minimal web UI for board settings.

## 🔧 **Board settings (edit here)**
**Use this link to modify board settings:**  
**https://damp-catlin-metroboard-7be2a3b3.koyeb.app/login**

## Features
- Real-time station status from GTFS-Realtime feeds (Sydney Trains + Metro by default).
- Static GTFS filtering to rail/metro routes (route types 1 and 2) with optional allowlists.
- Station+route indexing with optional whitelist and bounding box guardrails.
- LED-friendly status codes for off/far/approaching/at-station.
- Board settings UI with per-board brightness, optional distance-based brightness, night mode, and animation options.
- Firmware payload endpoint for easy embedded-device integration.

## How it works (technical overview)
1. **Static GTFS load**: Reads `stops.txt`, `routes.txt`, `trips.txt`, and `agency.txt` from the repository root, plus the Git LFS-backed `gtfs/stop_times.txt`. Optional files include `stations_whitelist.txt` and `route_overrides.csv`.
2. **Route mapping**: Builds a route short-name mapping and applies overrides (CSV or JSON) when present.
3. **Station index**: Filters to rail/metro routes, keeps numeric parent stations, and builds station+route keys used by the LED board.
4. **Realtime merge**: Fetches GTFS-Realtime feeds, merges data, and caches responses for a short interval.
5. **State calculation**: Matches each train to one station by stop ID when possible; otherwise uses the nearest station on that route and distance thresholds to determine far/near/solid states.

## Requirements
- Python 3.10+
- Dependencies: `fastapi`, `uvicorn`, `requests`, `urllib3`, `gtfs-realtime-bindings`, `protobuf`, `python-dotenv`, `python-multipart`

Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration
Environment variables:
- `TFNSW_API_TOKEN` (required for live data).
- `FEEDS` (comma-separated GTFS-RT URLs; defaults to Sydney Trains + Metro).
- `FEED_CACHE_SEC` (successful-feed refresh interval in seconds; default 5, with a hard minimum of 5 even if a lower hosted value is still configured).
- `FEED_FAILURE_BACKOFF_MAX_SEC` (maximum per-feed retry delay after failures or rate limiting; default 300 seconds).
- `FEED_STALE_MAX_SEC` (maximum age for last-good feed data; default 90 seconds and never lower than `FEED_CACHE_SEC`; expired train positions are removed even during backoff).
- `MB_D_SOLID`, `MB_D_NEAR`, `MB_D_FAR` (distance thresholds in meters; defaults 200/800/1500).
- `MB_AGENCIES`, `MB_ROUTE_SHORT_ALLOW` (allowlists for agencies/route shorts).
- `ROUTE_SHORT_OVERRIDES_PATH`, `ROUTE_SHORT_OVERRIDES_JSON` (override route short names).
- `BOARD_SETTINGS_PATH` (board config file; default `data/boards.json`).
- `STOP_TIMES_PATH` or `GTFS_STOP_TIMES_PATH` (override the stop times file; default prefers `gtfs/stop_times.txt`).
- `CORS_ALLOW_ORIGIN` (comma-separated CORS origins).
- `SYD_BBOX` or `SYD_BBOX_DISABLE` (configure/disable bounding box guardrail).
- `MB_VERBOSE` (logging verbosity; default on).
- `MB_FORCE_DISTANCE` (force distance-only matching).
- `MB_TIMEZONE` (IANA timezone for night-mode windows; default `Australia/Sydney`).
- `FIRMWARE_OTA_ENABLED` (enable Koyeb-hosted firmware OTA; default on).
- `FIRMWARE_MANIFEST_PATH` (default `firmware/manifest.json`).
- `FIRMWARE_PATH` (fallback firmware binary path; default `firmware/metroboard_public.bin`).
- `FIRMWARE_VERSION` (fallback firmware version when no manifest is present).
- `PUBLIC_BASE_URL` (optional public URL override for OTA download links).
- `BOARD_SETTINGS_PATH` should point at persistent storage in hosted deployments if dashboard edits must survive redeploys.
- `BOARD_OVERLAY_PATH` (legacy optional local board artwork image; default `assets/board_overlay.png`).
- `BOARD_OVERLAY_URL` (legacy optional externally hosted board artwork image URL).

## Data files
Place these alongside `app.py` (repository root):
- Required: `stops.txt`, `routes.txt`, `trips.txt`, `agency.txt`, `gtfs/stop_times.txt`
- Optional: `stations_whitelist.txt`, `route_overrides.csv`

## API endpoints
- `GET /` — lists available routes.
- `GET /states` — station LED states (supports `?only=` and `?board_id=`).
- `GET /stations` — station metadata list (lat/lon/route_short).
- `GET /healthz` — health summary (station count, feed age).
- `GET /debug` — feed summary and bounding-box info.
- `GET /trains` — sampled raw train entries.
- `GET /board_settings?board_id=...` — board configuration JSON.
- `GET /board_payload?board_id=...` — firmware-friendly settings plus state map.
- `GET /firmware_manifest?board_id=...&current=...` — OTA firmware metadata.
- `GET /firmware.bin?board_id=...&version=...` — OTA firmware binary, when deployed.
- `GET /board_layout` — station keys and line grouping metadata for dashboard controls.
- `GET /board_overlay` — legacy configured local board artwork image, if present.

## Firmware OTA
The public ESP32 sketch supports HTTP OTA through Koyeb from firmware version `1.4.1`.

First flash `metroboard_public.ino` over USB once. After that, future firmware updates can be deployed through Koyeb:

1. Edit the tracked root sketch `metroboard_public.ino`.
2. Bump `kFirmwareVersion` in `metroboard_public.ino`.
3. Bump `version` in `firmware/manifest.json` to the same value.
4. Build with PlatformIO:
   ```bash
   python -m platformio run -e metroboard_public
   ```
   The post-build script copies the binary to `firmware/metroboard_public.bin`.
5. Commit and push so Koyeb redeploys.

The board checks `/firmware_manifest` over Wi-Fi, downloads `/firmware.bin` when the version is newer, verifies the MD5 from the manifest, writes the update, and reboots. If the binary is missing, the manifest reports no update.

To flash the OTA-capable firmware over USB the first time:
```bash
python -m platformio run -e metroboard_public -t upload
```
The PlatformIO environment is configured for `COM17`.

## LED status codes
- `0` — off
- `1` — far/in area (distance > `D_NEAR` and ≤ `D_FAR`)
- `2` — approaching (distance > `D_SOLID` and ≤ `D_NEAR`, or incoming status)
- `3` — at station / solid (stopped or within `D_SOLID`)

Dashboard distance-curve settings can override these default radii per board.

When distance brightness is enabled for a board, firmware renders `1` as a dim
line colour, `2` as medium brightness, and `3` at the board's configured full
brightness. When disabled, only `3` is lit, matching the older solid-only
behavior.

## Running locally
1. Export a TfNSW API token:
   ```bash
   export TFNSW_API_TOKEN="..."
   ```
2. Start the server:
   ```bash
   uvicorn app:app --reload --port 8000
   ```
3. Open endpoints:
   - `http://localhost:8000/states` for live statuses
   - `http://localhost:8000/stations` for metadata
   - `http://localhost:8000/board/{your_id}` for board settings UI

## Board settings UI
The minimal HTML UI at `/login` and `/board/{board_id}` lets you:
- Set brightness and mode (`live`, `test`, or `animation`).
- Toggle distance brightness for live train LEDs and tune far/approaching brightness plus solid/near/far distance radii. Distance brightness is off by default.
- Configure night-mode windows and effective brightness.
- Select whole-line test LEDs when Test lights mode is active.
- Adjust animation effect, color, and speed when Animation mode is active.
- Enable developer mode to view board status and configure per-board OTA firmware targets. Developer mode is off by default except for board `AB12C`.

Settings are stored in `data/boards.json` by default and included in the
firmware payload. For permanent hosted settings, set `BOARD_SETTINGS_PATH` to a
persistent volume path or move settings storage to a database such as Postgres.
The ESP setup portal validates Board ID format as you type and checks the
backend live when the device has Wi-Fi during setup.

The dashboard intentionally uses compact line controls instead of rendering the
station visualiser.

## M1 extension to Bankstown

The public firmware already maps all 31 M1 LEDs from Tallawong to Bankstown,
including the ten stations beyond Sydenham. Keep their order and LED indices
unchanged when updating firmware. The backend includes those stations in its
station/line data and uses M1 vehicle positions to drive them; no opening-date
switch is required. Hills Showground is included in the station whitelist.

Keep the TfNSW token valid for both configured realtime feeds and refresh the
static GTFS data when the new service is published. The regression suite checks
stopped trains, GPS fallback for unknown platform IDs, approaching states, and
separation between M1 and T6 at Bankstown. These simulations do not verify the
operator's opening-day feed availability or route identifiers.

Run the regression suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

The firmware logic tests require `g++` or `clang++`. They compile selected
functions from the actual sketch and compare URL encoding, batching, LED
rendering and decay against the previous implementation. They do not replace
the full PlatformIO build or testing on a physical board.

Firmware releases must include a successfully built binary matching the sketch
and manifest version. Updating a version number alone does not update boards.
