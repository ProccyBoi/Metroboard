# app.py — Metroboard backend (AnyTrip API version with /states)
# --------------------------------------------------------------
# - Replaces TfNSW GTFS-RT with AnyTrip v3 API
# - Endpoints:
#     /stations?q=Central
#     /departures/{stop_id}
#     /trip/{trip_id}/{start_date}
#     /states?only=Central,Wynyard
# - Provides train "state" data for LED Metroboard display
# --------------------------------------------------------------

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
BASE_URL = f"https://anytrip.com.au/api/v3/region/{REGION}"
TRAIN_MODES = {"au:sydneytrains", "au:nswtrains", "au:metro", "au2:sydneytrains", "au2:nswtrains", "au2:metro"}
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN", "*")

CACHE_TTL = int(os.getenv("CACHE_TTL", "10"))  # seconds
DEBUG = os.getenv("DEBUG", "1") == "1"

LINE_COLOURS = {
    "M1": "#00b5cc", "T1": "#f6a623", "T2": "#2ba3e6", "T3": "#f37021",
    "T4": "#0d66c2", "T5": "#c2185b", "T7": "#7b1fa2", "T8": "#1e8e3e", "T9": "#d32f2f"
}

session = requests.Session()
CACHE = {}  # key → (timestamp, data)


# ---------------- Helpers ----------------
def vlog(*a):
    if DEBUG:
        print("[metroboard]", *a)


def cached_get(url: str) -> dict:
    """Fetch JSON safely with cache & AnyTrip-friendly headers"""
    now = time.time()
    if url in CACHE and (now - CACHE[url][0]) < CACHE_TTL:
        return CACHE[url][1]

    proxy_url = PROXY + url if PROXY else url
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/119.0 Safari/537.36",
        "Referer": "https://anytrip.com.au/",
        "Origin": "https://anytrip.com.au",
        "X-Requested-With": "XMLHttpRequest"
    }

    try:
        resp = session.get(proxy_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            vlog("WARN non-200", resp.status_code, url)
            return {}
        # handle empty / HTML bodies gracefully
        if not resp.text.strip().startswith("{") and not resp.text.strip().startswith("["):
            vlog("WARN non-JSON response", url)
            return {}
        data = resp.json()
        CACHE[url] = (now, data)
        return data
    except Exception as e:
        vlog("WARN cached_get failed", url, e)
        return {}



def extract_line_code(name: str | None) -> str:
    if not name:
        return "T?"
    m = re.search(r"\b([MT]\d+)\b", name, re.I)
    return m.group(1).upper() if m else name.strip().split()[0].upper()


def clean_stop(n: str | None) -> str:
    return (n or "").replace(" Station", "").strip()


def clean_via(v: str | None) -> str:
    v = (v or "").strip()
    return v.replace("via ", "", 1) if v.lower().startswith("via ") else v


def mins_from_now(epoch: int) -> str:
    """Convert UNIX time to human readable countdown"""
    now = int(time.time())
    m = max(0, round((epoch - now) / 60))
    if m >= 60:
        h, rem = divmod(m, 60)
        return f"{h}h {rem} min"
    return "1 min" if m <= 1 else f"{m} min"


# ---------------- FastAPI ----------------
app = FastAPI(title="Metroboard (AnyTrip API)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ALLOW_ORIGIN] if CORS_ALLOW_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "ok": True,
        "api": "AnyTrip v3",
        "region": REGION,
        "proxy": PROXY,
        "endpoints": [
            "/stations?q=Central",
            "/departures/{stop_id}",
            "/trip/{trip_id}/{start_date}",
            "/states?only=Central,Wynyard"
        ],
    }


# ---------------- Stations search ----------------
@app.get("/stations")
def search_stations(q: str = Query(..., description="Station name or ID")):
    """Search for Sydney Trains / Metro stations"""
    q = q.strip()
    if not q:
        return JSONResponse([])

    urls = [
        f"https://anytrip.com.au/api/v3/search?q={q}&types=stop",
        f"https://anytrip.com.au/api/v3/region/{REGION}/search?q={q}&types=stop",
        f"https://anytrip.com.au/api/v3/suggest?q={q}&types=stop",
    ]
    for url in urls:
        try:
            j = cached_get(url)
            arr = j.get("response", {}).get("stops") or j.get("stops") or j
            if not isinstance(arr, list):
                continue
            out = []
            for s in arr:
                stop = s.get("stop", s)
                modes = stop.get("modes", [])
                if not any("train" in str(m) or "metro" in str(m) for m in modes):
                    continue
                sid = stop.get("id") or stop.get("code")
                if not sid:
                    continue
                name = stop.get("name", {}).get("station_readable_name") or stop.get("fullName") or stop.get("name")
                out.append({
                    "id": sid,
                    "name": clean_stop(name),
                    "locality": stop.get("locality") or "",
                })
            if out:
                return out
        except Exception as e:
            vlog("WARN search", url, e)
    return []


# ---------------- Departures ----------------
@app.get("/departures/{stop_id}")
def departures(stop_id: str, limit: int = 5):
    """Return next train departures for a stop"""
    ts = int(time.time())
    stop_id = stop_id if stop_id.startswith("au2:") else f"au2:{stop_id}"
    url = f"https://anytrip.com.au/api/v3/region/{REGION}/departures/{stop_id}?ts={ts}&limit=50&includeCancelled=true"
    j = cached_get(url)
    deps = j.get("response", {}).get("departures", [])

    trains = []
    for d in deps:
        trip = d.get("tripInstance", {}).get("trip", {})
        route = trip.get("route", {})
        mode = route.get("mode")
        if mode not in TRAIN_MODES:
            continue

        line_name = route.get("name") or route.get("longName") or ""
        line_code = extract_line_code(line_name)
        stop_inst = d.get("stopTimeInstance", {}).get("stop", {})
        platform = (
            stop_inst.get("disassembled", {}).get("platformName")
            or stop_inst.get("platform")
            or "-"
        )
        headsign = d.get("stopTimeInstance", {}).get("stopHeadsign", {}) or {}
        to = headsign.get("headline") or route.get("longName") or line_name
        via = clean_via(headsign.get("subtitle") or "")
        when = d.get("stopTimeInstance", {}).get("departure", {}).get("time")
        if not when:
            continue

        trains.append({
            "line": line_code,
            "to": to,
            "via": via,
            "platform": platform,
            "depart_in": mins_from_now(when),
            "colour": LINE_COLOURS.get(line_code, "#ff9152"),
            "when": when,
        })
        if len(trains) >= limit:
            break

    return JSONResponse({"stop_id": stop_id, "departures": trains})


# ---------------- Trip details ----------------
@app.get("/trip/{trip_id}/{start_date}")
def trip_detail(
    trip_id: str = Path(..., description="Trip ID (e.g. au2:12345)"),
    start_date: str = Path(..., description="Start date (YYYY-MM-DD)"),
):
    """Return full tripInstance details"""
    url = f"https://anytrip.com.au/api/v3/region/{REGION}/tripInstance/{trip_id}/{start_date}/0"
    j = cached_get(url)
    ti = j.get("response", {}).get("tripInstance", {})
    trip = ti.get("trip", {})
    route = trip.get("route", {})
    pattern = j.get("response", {}).get("realtimePattern", [])

    stops = []
    for s in pattern:
        if s.get("skipped"):
            continue
        p = s.get("stop", {}).get("parent") or s.get("stop")
        stops.append(clean_stop(
            p.get("disassembled", {}).get("stationName")
            or p.get("disassembled", {}).get("fullName")
            or p.get("name")
            or ""
        ))

    return JSONResponse({
        "trip_id": trip_id,
        "line": extract_line_code(route.get("name")),
        "destination": trip.get("headsign") or route.get("longName"),
        "stops": stops,
        "cars": trip.get("scheduledVehicleProperties", {}).get("cars"),
    })


# ---------------- States ----------------
@app.get("/states")
def states(only: str = Query(None, description="Comma-separated station names")):
    """
    Simplified "state" indicator per station:
      0 = no train nearby
      1 = far
      2 = approaching (<=6 min)
      3 = stopped/soon (<=2 min)
    """
    if not only:
        return JSONResponse({"error": "Provide ?only=Station1,Station2,..."}), 400

    station_list = [s.strip() for s in only.split(",") if s.strip()]
    out = {}

    for name in station_list:
        # fetch top 1–2 departures for each station
        search = search_stations(name)
        if not search:
            out[name] = 0
            continue
        stop_id = search[0]["id"]
        try:
            ts = int(time.time())
            url = f"https://anytrip.com.au/api/v3/region/{REGION}/departures/{stop_id}?ts={ts}&limit=3"
            j = cached_get(url)
            deps = j.get("response", {}).get("departures", [])
            if not deps:
                out[name] = 0
                continue
            soonest = min((d.get("stopTimeInstance", {}).get("departure", {}).get("time") or 0) for d in deps)
            mins = max(0, round((soonest - ts) / 60))
            if mins <= 2:
                state = 3
            elif mins <= 6:
                state = 2
            elif mins <= 15:
                state = 1
            else:
                state = 0
            out[name] = state
        except Exception as e:
            vlog("WARN /states", name, e)
            out[name] = 0

    return JSONResponse(out)


# ---------------- Health check ----------------
@app.get("/healthz")
def health():
    return {"status": "ok", "cached_keys": len(CACHE), "proxy": bool(PROXY)}


# ---------------- Run (dev) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
