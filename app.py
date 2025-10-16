# app.py — Metroboard backend (AnyTrip v3 Mirror + Auto Station Map)
# -------------------------------------------------------------------
# Features:
#  • Mirrors your HTML AnyTrip logic (departures/trips/states)
#  • Auto-builds Sydney Trains + Metro station map on startup
#  • Accepts ?ids=... or ?only=... for /states
#  • Converts names like "Central9" → "au2:200060"
#  • Caches data for 10s to limit AnyTrip hits
# -------------------------------------------------------------------

import os
import time
import re
import requests
from fastapi import FastAPI, Query, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------- Config ----------------
PROXY = os.getenv("ANYTRIP_PROXY", "https://cors-proxy.joshchans.com/")
REGION = os.getenv("ANYTRIP_REGION", "au2")
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN", "*")
CACHE_TTL = int(os.getenv("CACHE_TTL", "10"))
DEBUG = os.getenv("DEBUG", "1") == "1"

session = requests.Session()
CACHE = {}

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Referer": "https://anytrip.com.au/",
    "Origin": "https://anytrip.com.au",
    "X-Requested-With": "XMLHttpRequest"
}


# ---------------- Helpers ----------------
def vlog(*a):
    if DEBUG:
        print("[metroboard]", *a)


def get_json(url: str) -> dict:
    """Browser-like fetch with cache and graceful fallback"""
    now = time.time()
    if url in CACHE and now - CACHE[url][0] < CACHE_TTL:
        return CACHE[url][1]

    full = PROXY + url if PROXY else url
    try:
        resp = session.get(full, headers=HEADERS, timeout=10)
        if not resp.text.strip().startswith("{") and not resp.text.strip().startswith("["):
            vlog("WARN non-JSON", url)
            return {}
        data = resp.json()
        CACHE[url] = (now, data)
        return data
    except Exception as e:
        vlog("WARN get_json", url, e)
        return {}


# ---------------- FastAPI ----------------
app = FastAPI(title="Metroboard (AnyTrip Mirror)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if CORS_ALLOW_ORIGIN == "*" else [CORS_ALLOW_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- Station map builder ----------------
def build_station_map() -> dict:
    """Fetch and cache all known Sydney Trains + Metro stations."""
    url = f"https://anytrip.com.au/api/v3/region/{REGION}/stops"
    data = get_json(url)
    stops = data.get("response", {}).get("stops") or data.get("stops") or []
    result = {}

    for s in stops:
        stop = s.get("stop", s)
        modes = stop.get("modes", [])
        if not any("train" in str(m) or "metro" in str(m) for m in modes):
            continue
        sid = stop.get("id") or stop.get("code")
        name = (
            stop.get("name", {}).get("station_readable_name")
            or stop.get("fullName")
            or stop.get("name")
            or ""
        )
        clean = name.replace(" Station", "").strip()
        if sid and clean:
            result[clean] = sid

    CACHE["station_map"] = (time.time(), result)
    vlog(f"Built station map with {len(result)} entries")
    return result


@app.on_event("startup")
def auto_build_map():
    """Automatically build station map at startup."""
    try:
        build_station_map()
    except Exception as e:
        vlog("WARN auto map build failed", e)


@app.get("/build_station_map")
def manual_build_station_map():
    """Manual rebuild of station map."""
    result = build_station_map()
    return JSONResponse({"count": len(result), "sample": dict(list(result.items())[:10])})


# ---------------- Core endpoints ----------------
@app.get("/")
def root():
    return {
        "api": "AnyTrip v3 mirror",
        "proxy": PROXY,
        "map_entries": len(CACHE.get("station_map", (0, {}))[1]),
        "endpoints": [
            "/search?q=",
            "/departures/{stop_id}",
            "/tripInstance/{trip_id}/{start_date}",
            "/stop/{stop_id}",
            "/states?only=Central9,Redfern9"
        ]
    }


@app.get("/search")
def search(q: str = Query(...)):
    urls = [
        f"https://anytrip.com.au/api/v3/search?q={q}&types=stop",
        f"https://anytrip.com.au/api/v3/region/{REGION}/search?q={q}&types=stop",
        f"https://anytrip.com.au/api/v3/suggest?q={q}&types=stop",
    ]
    for u in urls:
        j = get_json(u)
        arr = j.get("response", {}).get("stops") or j.get("stops") or j
        if isinstance(arr, list) and arr:
            return arr
    return []


@app.get("/departures/{stop_id}")
def departures(stop_id: str):
    ts = int(time.time())
    url = f"https://anytrip.com.au/api/v3/region/{REGION}/departures/{stop_id}?ts={ts}&limit=80&includeCancelled=true"
    return get_json(url)


@app.get("/tripInstance/{trip_id}/{start_date}")
def trip_instance(trip_id: str, start_date: str):
    url = f"https://anytrip.com.au/api/v3/region/{REGION}/tripInstance/{trip_id}/{start_date}/0"
    return get_json(url)


@app.get("/stop/{stop_id}")
def stop(stop_id: str):
    url = f"https://anytrip.com.au/api/v3/region/{REGION}/stop/{stop_id}"
    return get_json(url)


# ---------------- States endpoint ----------------
@app.get("/states")
def states(ids: str | None = Query(None), only: str | None = Query(None)):
    """
    Returns a train presence state for each station (0–3):
      0 = no train nearby
      1 = within 15 min
      2 = approaching (<=6 min)
      3 = arriving (<=2 min)
    Accepts ?ids=au2:... or ?only=Central9,Redfern9,...
    """
    query = ids or only
    if not query:
        return JSONResponse({"error": "Provide ?ids=... or ?only=..."}, status_code=400)

    # Load cached map
    station_map = CACHE.get("station_map", (0, {}))[1]
    id_list = []

    for item in [s.strip() for s in query.split(",") if s.strip()]:
        if item.startswith("au2:"):
            id_list.append(item)
            continue
        base = re.sub(r"\d+$", "", item).strip()
        sid = station_map.get(base)
        if sid:
            id_list.append(sid)
        else:
            vlog("WARN unresolved", base)

    if not id_list:
        return JSONResponse({"error": "No valid station IDs resolved"}, status_code=404)

    # Fetch departure times
    now = int(time.time())
    out = {}
    for sid in id_list:
        url = f"https://anytrip.com.au/api/v3/region/{REGION}/departures/{sid}?ts={now}&limit=3"
        j = get_json(url)
        deps = j.get("response", {}).get("departures", [])
        if not deps:
            out[sid] = 0
            continue

        soonest = min(
            (d.get("stopTimeInstance", {}).get("departure", {}).get("time") or 0)
            for d in deps
        )
        mins = max(0, round((soonest - now) / 60))
        if mins <= 2:
            state = 3
        elif mins <= 6:
            state = 2
        elif mins <= 15:
            state = 1
        else:
            state = 0
        out[sid] = state

    return JSONResponse(out)


# ---------------- Health check ----------------
@app.get("/healthz")
def health():
    return {
        "status": "ok",
        "map_size": len(CACHE.get("station_map", (0, {}))[1]),
        "cached_keys": len(CACHE),
        "proxy": bool(PROXY)
    }


# ---------------- Run locally ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
