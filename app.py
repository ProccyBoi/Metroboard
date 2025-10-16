# app.py — Metroboard backend (mirror of working HTML AnyTrip client)
# -------------------------------------------------------------------
# - Uses AnyTrip v3 endpoints via proxy
# - Mirrors behaviour of the HTML demo
# - Endpoints: /search, /departures/{id}, /tripInstance/{trip_id}/{start_date}, /stop/{id}, /states
# -------------------------------------------------------------------

import os
import time
import requests
from fastapi import FastAPI, Query, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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


app = FastAPI(title="Metroboard (AnyTrip Mirror)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if CORS_ALLOW_ORIGIN == "*" else [CORS_ALLOW_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "api": "AnyTrip v3 mirror",
        "proxy": PROXY,
        "endpoints": ["/search?q=", "/departures/{stop_id}", "/tripInstance/{trip_id}/{start_date}", "/stop/{stop_id}", "/states?ids="]
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


@app.get("/states")
def states(ids: str = Query(...)):
    """Roughly match the HTML logic: 0–3 state per stop_id"""
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    now = int(time.time())
    out = {}
    for sid in id_list:
        url = f"https://anytrip.com.au/api/v3/region/{REGION}/departures/{sid}?ts={now}&limit=3"
        j = get_json(url)
        deps = j.get("response", {}).get("departures", [])
        if not deps:
            out[sid] = 0
            continue
        soonest = min((d.get("stopTimeInstance", {}).get("departure", {}).get("time") or 0) for d in deps)
        mins = max(0, round((soonest - now) / 60))
        if mins <= 2:
            s = 3
        elif mins <= 6:
            s = 2
        elif mins <= 15:
            s = 1
        else:
            s = 0
        out[sid] = s
    return out
