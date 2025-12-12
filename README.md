# Metroboard

Live LED board for the **Sydney Trains** network 🚇💡

Metroboard pulls real‑time vehicle positions from Transport for NSW (TfNSW) and reduces them to simple per‑station states (off / near / at station). Those states drive LEDs on a physical display or a web preview.

---

## How it works

* **Backend (FastAPI)**

  * Connects to the TfNSW **GTFS‑Realtime VehiclePositions** feed.
  * Restricts stations to *exactly* the Sydney Trains map via a **whitelist file** (`stations_whitelist.txt`).
  * Computes per‑station state from nearest train distance:

    * `0` = off (no train nearby)
    * `1` = near (≤ 500 m; slow blink)
    * `3` = at station (≤ 120 m; solid)
    * `2` = reserved for future “fast blink”.
  * Exposes REST endpoints for the web UI / hardware.

* **Frontend (optional)**

  * Simple React/HTML client that polls `/states` and renders LED‑style indicators.

* **Hardware**

  * ESP32 (or similar) driving addressable LEDs. One LED per station, mapped by name.

---

## Requirements

* Python **3.10+**
* A **TfNSW API key** (GTFS‑Realtime access)
* Static GTFS files alongside the app:

  * `routes.txt`, `stops.txt` (always required)
  * `trips.txt`, `stop_times.txt` (only needed if you want to *generate* the whitelist automatically)

---

## Setup

Install dependencies:

```bash
pip install fastapi uvicorn requests protobuf
```

Run the backend (set `TFNSW_API_TOKEN` if you want live train data):

```bash
# macOS/Linux
export TFNSW_API_TOKEN=your_api_key_here
uvicorn app:app --reload --port 8000
```

```powershell
# Windows (PowerShell)
$env:TFNSW_API_TOKEN="your_api_key_here"
uvicorn app:app --reload --port 8000
```

Backend will start at **[http://localhost:8000](http://localhost:8000)**.
If you skip `TFNSW_API_TOKEN`, board settings pages will still work but live
train endpoints like `/states` and `/trains` will return an error until you
provide the key.

---

## Station whitelist

To match the **official Sydney Trains network map**, the backend reads a whitelist file and *only* exposes those stations.

You have two options:

### A) Generate from GTFS (recommended)

If you have the full static GTFS (contains `trips.txt` and `stop_times.txt`):

```bash
python build_station_whitelist.py stops.txt routes.txt trips.txt stop_times.txt stations_whitelist.txt
```

This writes one **parent station `stop_id` per line** to `stations_whitelist.txt` for all lines in `{T1..T9, CCN, SCO, BMT, SHL}`.

### B) Create by hand (quick)

Create a text file `stations_whitelist.txt` next to `app.py` with one entry per line. You can use station **names** (case‑insensitive; “ Station” suffix ignored) or parent **stop\_ids**:

```
Central
Town Hall
Wynyard
North Sydney
Epping
Eastwood
# …and so on
```

> **Note:** The backend accepts either names or numeric parent `stop_id`s.

---

## Configuration (env vars)

| Variable                 | Required | Default                   | Description                                        |
| ------------------------ | :------: | ------------------------- | -------------------------------------------------- |
| `TFNSW_API_TOKEN`        |   Live    | —                         | TfNSW API key for GTFS‑RT (required for train data). |
| `STATION_WHITELIST_PATH` |          | `stations_whitelist.txt`  | Path to whitelist file.                            |
| `FEED_CACHE_SEC`         |          | `3`                       | Cache lifetime for VehiclePositions (seconds).     |
| `CORS_ALLOW_ORIGIN`      |          | —                         | Comma‑sep. list (e.g. `*` or `https://your.site`). |
| `SYD_BBOX`               |          | `-34.2,150.5,-33.4,151.4` | Optional guardrail bbox `S,W,N,E`.                 |
| `SYD_BBOX_DISABLE`       |          | `0`                       | Set to `1` to disable bbox filtering entirely.     |

---

## API

* `GET /healthz` → `{"status":"ok","stations":N,"trains_last_fetch_s":age}`
* `GET /stations` → `[{"name":"Epping","stop_id":"212110","lat":...,"lon":...}, ...]`
* `GET /states` → `{ "Station": 0|1|3, ... }`

  * Optional: `?only=Epping,Eastwood` to limit output to a subset
* `GET /debug` → configuration + counters (bbox, cache, routes)

### Examples

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/stations | jq .
curl "http://localhost:8000/states?only=Epping,Eastwood" | jq .
```

---

## Frontend (optional)

Use the provided React component (`MetroboardPanel`) or a plain HTML+JS page to preview `/states` while testing.

* Same‑origin by default; if hosting separately, enable CORS and pass `?api=https://api.example.com` to the page.

---

## Hardware integration (outline)

* Map station names from `/stations` to LED indices in firmware.
* Poll `/states` every \~5 s.
* LED behaviour:

  * `0` off
  * `1` slow blink (near)
  * `3` solid (at station)
  * `2` reserved for later

---

## Troubleshooting

* **`/stations` is empty**

  * Whitelist file missing or not found (check `STATION_WHITELIST_PATH`).
  * Whitelist names don’t match GTFS `stop_name`s (try using parent `stop_id`s).

* **`/states` always zeros**

  * GTFS‑RT fetch failing (bad token/network) → check `/healthz` and server logs.
  * `ROUTE_SHORT_ALLOW` doesn’t include the line you expect.

* **CORS errors in browser**

  * Set `CORS_ALLOW_ORIGIN="*"` (or your site origin) before starting the app.

---

## License

MIT
