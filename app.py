# app.py — Metroboard backend (AnyTrip version)
# ------------------------------------------------------------
# Uses AnyTrip public API (https://anytrip.com.au/api/v3/region/au2)
# for Sydney Trains + Metro departures.
#
# LEDs light when a train of the same route departs from that station
# within a configurable window (MB_DEPART_WINDOW_S, default 90 s).
# ------------------------------------------------------------

import os
import time
import csv
import json
import re
import typing as T
import requests
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
ANYTRIP_BASE = "https://anytrip.com.au/api/v3/region/au2"
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "5"))
MB_DEPART_WINDOW_S = int(os.getenv("MB_DEPART_WINDOW_S", "90"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE = os.getenv("MB_VERBOSE", "1") == "1"

# optional whitelist of routes (e.g. "T1,T2,T3,M1")
ROUTE_SHORT_ALLOW: set[str] = {s.strip().upper() for s in os.getenv("MB_ROUTE_SHORT_ALLOW", "").split(",") if s.strip()}

def vlog(*a):
    if MB_VERBOSE:
        print("[metroboard]", *a)

# ---------------- HTTP session with retry ----------------
def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.6,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# ---------------- Caching wrapper ----------------
_CACHE: dict[str, tuple[float, T.Any]] = {}

def cache_get(key: str, ttl: int):
    v = _CACHE.get(key)
    if v:
        ts, val = v
        if time.time() - ts < ttl:
            return val
    return None

def cache_set(key: str, val: T.Any):
    _CACHE[key] = (time.time(), val)

# ---------------- AnyTrip fetchers ----------------
def fetch_anytrip_departures(stop_id: str, limit: int = 50) -> list[dict]:
    """Returns list of departures for one stop."""
    key = f"depart:{stop_id}"
    cached = cache_get(key, FEED_CACHE_SEC)
    if cached is not None:
        return cached

    url = f"{ANYTRIP_BASE}/departures/{stop_id}?limit={limit}&modes=au:sydneytrains,au:metro"
    s = _session()
    r = s.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    out = []
    for d in data.get("response", {}).get("departures", []):
        tri = d.get("tripInstance", {})
        trip = tri.get("trip", {})
        route = trip.get("route", {})
        stopTime = d.get("stopTimeInstance", {})
        stop = stopTime.get("stop", {})
        dep = stopTime.get("departure", {})

        short = (route.get("shortName") or "").strip().upper()
        if ROUTE_SHORT_ALLOW and short not in ROUTE_SHORT_ALLOW:
            continue

        out.append({
            "route": short or None,
            "headsign": trip.get("headsign"),
            "platform": stop.get("platform"),
            "stop_name": stop.get("name"),
            "departure": dep.get("time"),
        })
    cache_set(key, out)
    return out

# ---------------- Station index (static GTFS subset) ----------------
# You can reuse your existing stops.txt / routes.txt logic if you want full GTFS filtering.
# For simplicity, we keep a minimal file for mapping base station names to AnyTrip IDs.

def load_station_index(path: str = "stations_anytrip.csv") -> dict[str, dict]:
    """
    CSV: name,stop_id,route_short(optional)
    Example:
      Central,au2:Central,T1
      Town Hall,au2:TownHall,T1
    """
    idx: dict[str, dict] = {}
    if not os.path.exists(path):
        vlog("WARN: stations_anytrip.csv not found – using empty index.")
        return idx
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            sid = (row.get("stop_id") or "").strip()
            rs = (row.get("route_short") or "").strip().upper()
            if name and sid:
                idx[name] = {"name": name, "stop_id": sid, "route_short": rs}
    vlog(f"stations loaded: {len(idx)}")
    return idx

# ---------------- FastAPI setup ----------------
app = FastAPI()

if CORS_ALLOW_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

STATION_INDEX: dict[str, dict] = {}

@app.on_event("startup")
def _startup():
    global STATION_INDEX
    STATION_INDEX = load_station_index()

@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/healthz", "/debug", "/stations", "/states", "/trains"]}

@app.get("/healthz")
def healthz():
    return {"status": "ok", "stations": len(STATION_INDEX), "cache_entries": len(_CACHE)}

# ---------------- /trains endpoint ----------------
@app.get("/trains")
def trains(stop: str = Query("au2:Central", description="AnyTrip stop_id, e.g. au2:Central")):
    """Live departures for one stop."""
    try:
        departures = fetch_anytrip_departures(stop)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Simplify for UI
    now = time.time()
    out = []
    for d in departures:
        t = d.get("departure")
        if t is None:
            continue
        secs = int(t - now)
        if 0 <= secs <= MB_DEPART_WINDOW_S:
            status = "DEPARTING_SOON"
        elif secs > MB_DEPART_WINDOW_S:
            status = "SCHEDULED"
        else:
            status = "PAST"
        out.append({
            "route": d["route"],
            "headsign": d["headsign"],
            "platform": d["platform"],
            "departure_epoch": t,
            "depart_in_s": secs,
            "status": status,
        })
    return {"stop": stop, "count": len(out), "departures": out}

# ---------------- /stations endpoint ----------------
@app.get("/stations")
def stations():
    return JSONResponse(list(STATION_INDEX.values()))

# ---------------- /states endpoint ----------------
@app.get("/states")
def states():
    """
    Returns {station_name: brightness_level}
    Brightness 3 = departing soon (< window)
    2 = has later departures
    0 = none
    """
    now = time.time()
    out: dict[str, int] = {}

    for name, meta in STATION_INDEX.items():
        stop_id = meta["stop_id"]
        rs = meta.get("route_short", "").upper()
        try:
            deps = fetch_anytrip_departures(stop_id)
        except Exception:
            out[name] = 0
            continue

        level = 0
        for d in deps:
            drs = (d.get("route") or "").upper()
            if rs and drs and drs != rs:
                continue
            t = d.get("departure")
            if t is None:
                continue
            secs = t - now
            if 0 <= secs <= MB_DEPART_WINDOW_S:
                level = 3
                break
            elif secs > MB_DEPART_WINDOW_S and secs <= 10 * MB_DEPART_WINDOW_S:
                level = max(level, 2)
        out[name] = level

    active = sum(1 for v in out.values() if v > 0)
    vlog(f"states active={active}/{len(out)}")
    return JSONResponse(out)

# ---------------- /debug endpoint ----------------
@app.get("/debug")
def debug():
    return {
        "stations_count": len(STATION_INDEX),
        "cache_entries": len(_CACHE),
        "depart_window_s": MB_DEPART_WINDOW_S,
        "feed_cache_sec": FEED_CACHE_SEC,
        "routes_allowed": sorted(list(ROUTE_SHORT_ALLOW)) if ROUTE_SHORT_ALLOW else "all",
    }

# ---------------- run (dev) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                reload=os.getenv("RELOAD", "0") == "1")
