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
- Board settings UI with per-board brightness, night mode, and animation options.
- Firmware payload endpoint for easy embedded-device integration.

## How it works (technical overview)
1. **Static GTFS load**: Reads `stops.txt`, `routes.txt`, `trips.txt`, and `agency.txt` from the repository root, plus the Git LFS-backed `gtfs/stop_times.txt`. Optional files include `stations_whitelist.txt` and `route_overrides.csv`.
2. **Route mapping**: Builds a route short-name mapping and applies overrides (CSV or JSON) when present.
3. **Station index**: Filters to rail/metro routes, keeps numeric parent stations, and builds station+route keys used by the LED board.
4. **Realtime merge**: Fetches GTFS-Realtime feeds, merges data, and caches responses for a short interval.
5. **State calculation**: Matches trains to stations by stop ID when possible; otherwise uses distance thresholds to determine far/near/solid states.

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
- `FEED_CACHE_SEC` (cache duration in seconds; default 3).
- `MB_D_SOLID`, `MB_D_NEAR`, `MB_D_FAR` (distance thresholds in meters; defaults 200/800/1500).
- `MB_AGENCIES`, `MB_ROUTE_SHORT_ALLOW` (allowlists for agencies/route shorts).
- `ROUTE_SHORT_OVERRIDES_PATH`, `ROUTE_SHORT_OVERRIDES_JSON` (override route short names).
- `BOARD_SETTINGS_PATH` (board config file; default `boards.json`).
- `STOP_TIMES_PATH` or `GTFS_STOP_TIMES_PATH` (override the stop times file; default prefers `gtfs/stop_times.txt`).
- `CORS_ALLOW_ORIGIN` (comma-separated CORS origins).
- `SYD_BBOX` or `SYD_BBOX_DISABLE` (configure/disable bounding box guardrail).
- `MB_VERBOSE` (logging verbosity; default on).
- `MB_FORCE_DISTANCE` (force distance-only matching).
- `MB_TIMEZONE` (IANA timezone for night-mode windows; default `Australia/Sydney`).

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

## LED status codes
- `0` — off
- `1` — far/in area (distance > `D_NEAR` and ≤ `D_FAR`)
- `2` — approaching (distance > `D_SOLID` and ≤ `D_NEAR`, or incoming status)
- `3` — at station / solid (stopped or within `D_SOLID`)

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
- Set brightness and mode (`live` vs `animation`).
- Configure night-mode windows and effective brightness.
- Adjust animation color and speed.

Settings are stored in `boards.json` and included in the firmware payload.
