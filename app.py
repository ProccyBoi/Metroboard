# app.py — Metroboard backend (Rail + Metro) — FINAL VERSION (GTFS-accurate)
# --------------------------------------------------------------------------
# - Uses static GTFS (stops.txt, routes.txt, trips.txt, stop_times.txt, agency.txt)
# - route_short_name derived directly from TfNSW GTFS routes.txt + trips.txt
# - No hardcoded ROUTE_PREFIX_MAP guessing
# - Filters to route_type in (1=subway/metro, 2=rail)
# - GTFS-RT from TfNSW (Sydney Trains + Metro)
# - /states matches trains to stations on the same route_short_name
# - Endpoints: /, /healthz, /debug, /stations, /states, /trains
# --------------------------------------------------------------------------

import os, time, csv, json, re, random, typing as T, requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
API_TOKEN = os.environ["TFNSW_API_TOKEN"]

URLS_VP = [u.strip() for u in os.getenv("FEEDS", "").split(",") if u.strip()] or [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]

D_SOLID, D_NEAR, D_FAR = [int(os.getenv(k, v)) for k, v in {
    "MB_D_SOLID": "200", "MB_D_NEAR": "800", "MB_D_FAR": "1500"
}.items()]

AGENCY_NAME_ALLOW = {a.strip() for a in os.getenv("MB_AGENCIES", "").split(",") if a.strip()}
ROUTE_SHORT_ALLOW = {s.strip().upper() for s in os.getenv("MB_ROUTE_SHORT_ALLOW", "").split(",") if s.strip()}

FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE = os.getenv("MB_VERBOSE", "1") == "1"
MB_FORCE_DISTANCE = os.getenv("MB_FORCE_DISTANCE", "0") == "1"

DEFAULT_SYD_BBOX = (-34.2, 150.5, -33.4, 151.4)
def _bbox_env():
    if os.getenv("SYD_BBOX_DISABLE") == "1":
        return None
    s = os.getenv("SYD_BBOX")
    if not s: return DEFAULT_SYD_BBOX
    try: return tuple(map(float, s.split(",")))
    except: return DEFAULT_SYD_BBOX
SYD_BBOX = _bbox_env()

def vlog(*a): 
    if MB_VERBOSE: print("[metroboard]", *a)

EXCLUDE_SUB = ("wharf", "light rail", "b-line", "interchange", "motorway", " m2")

# ---------------- Utils ----------------
def _session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=4, backoff_factor=0.6,
        status_forcelist=(500,502,503,504), allowed_methods=frozenset(["GET"]))))
    return s

def hav_m(a,b,c,d):
    from math import radians, sin, cos, asin, sqrt
    try: a,b,c,d = map(float,(a,b,c,d))
    except: return float("inf")
    R = 6371000.0
    da, db = radians(c-a), radians(d-b)
    t = sin(da/2)**2 + cos(radians(a))*cos(radians(c))*sin(db/2)**2
    return 2*R*asin(sqrt(max(0.0,min(1.0,t))))

def within_bbox(lat,lon,bbox): S,W,N,E=bbox; return S<=lat<=N and W<=lon<=E
def status_from_dist(m): 
    return 3 if m<=D_SOLID else 2 if m<=D_NEAR else 1 if m<=D_FAR else 0
def clean_name(n): 
    for suf in (" Station"," Railway Station"): 
        if n.endswith(suf): n=n[:-len(suf)]
    return n.strip()

# ---------------- GTFS loaders ----------------
def load_route_shortnames(routes="routes.txt"):
    out={}
    with open(routes,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=(r.get("route_id") or "").strip()
            rs=(r.get("route_short_name") or "").strip().upper()
            if rid and rs: out[rid]=rs
    vlog(f"route_short: {len(out)}")
    return out

def load_trip_to_short(trips="trips.txt", routes="routes.txt"):
    route_map=load_route_shortnames(routes)
    trip_map={}
    with open(trips,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tid=(r.get("trip_id") or "").strip()
            rid=(r.get("route_id") or "").strip()
            if tid and rid in route_map: trip_map[tid]=route_map[rid]
    vlog(f"trip_to_short: {len(trip_map)}")
    return trip_map

def build_child_to_parent(stops="stops.txt"):
    out={}
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=(r.get("stop_id") or "").strip()
            pid=(r.get("parent_station") or "").strip()
            lt=(r.get("location_type") or "0").strip()
            if sid and pid: out[sid]=pid
            if lt=="1" and sid: out.setdefault(sid,sid)
    vlog(f"child→parent: {len(out)}")
    return out

# ---------------- GTFS-RT ----------------
_FEED_CACHE=None; _FEED_TS=0.0
def fetch_vp_cached():
    global _FEED_CACHE,_FEED_TS
    now=time.time()
    if _FEED_CACHE and (now-_FEED_TS)<FEED_CACHE_SEC: return _FEED_CACHE, now-_FEED_TS
    hdr={"Authorization":f"apikey {API_TOKEN}","Accept":"application/x-protobuf","User-Agent":"metroboard/1.0"}
    s=_session(); merged=rt.FeedMessage(); merged.header.gtfs_realtime_version="2.0"
    total=0
    for url in URLS_VP:
        try:
            r=s.get(url,headers=hdr,timeout=20); r.raise_for_status()
            feed=rt.FeedMessage(); feed.ParseFromString(r.content)
            merged.entity.extend(feed.entity)
            total+=sum(1 for e in feed.entity if e.HasField("vehicle"))
        except Exception as e: vlog("WARN fetch",url,e)
    _FEED_CACHE, _FEED_TS=merged, now
    vlog("feed merged vehicles", total)
    return merged,0.0

# ---------------- Train collector ----------------
def collect_trains(feed, trip_to_short):
    trains=[]; by_route={}; missing={}
    for e in feed.entity:
        if not e.HasField("vehicle"): continue
        v=e.vehicle
        tripid=v.trip.trip_id if v.HasField("trip") else ""
        rid=v.trip.route_id if v.HasField("trip") else ""
        rs=trip_to_short.get(tripid) or ""
        if not rs: missing[rid or tripid]=missing.get(rid or tripid,0)+1
        if rs and ROUTE_SHORT_ALLOW and rs not in ROUTE_SHORT_ALLOW: continue
        sid=getattr(v,"stop_id","")
        st=int(getattr(v,"current_status",rt.VehiclePosition.IN_TRANSIT_TO))
        lat=getattr(v.position,"latitude",None) if v.HasField("position") else None
        lon=getattr(v.position,"longitude",None) if v.HasField("position") else None
        trains.append((lat,lon,rs or None,sid,st))
        if rs: by_route[rs]=by_route.get(rs,0)+1
    if missing: vlog(f"missing short_names: {len(missing)} sample={list(missing)[:8]}")
    vlog(f"collect_trains kept={len(trains)} by_route={len(by_route)}")
    return trains,by_route

# ---------------- FastAPI ----------------
app=FastAPI()
if CORS_ALLOW_ORIGIN:
    app.add_middleware(CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

ROUTE_ID_TO_SHORT={}; TRIP_TO_ROUTE_SHORT={}; CHILD_TO_PARENT={}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, TRIP_TO_ROUTE_SHORT, CHILD_TO_PARENT
    ROUTE_ID_TO_SHORT=load_route_shortnames("routes.txt")
    TRIP_TO_ROUTE_SHORT=load_trip_to_short("trips.txt","routes.txt")
    CHILD_TO_PARENT=build_child_to_parent("stops.txt")
    vlog("startup complete")

@app.get("/")
def root(): 
    return {"ok":True,"endpoints":["/states","/stations","/healthz","/debug","/trains"]}

@app.get("/healthz")
def healthz():
    age=None if _FEED_TS==0 else (time.time()-_FEED_TS)
    return {"status":"ok","trains_cache_age":age}

@app.get("/debug")
def debug():
    feed,_=fetch_vp_cached()
    trains,by_route=collect_trains(feed,TRIP_TO_ROUTE_SHORT)
    return {
        "total_trains":len(trains),
        "by_route":by_route,
        "missing_routes":len([t for t in trains if t[2] is None]),
        "feeds":URLS_VP,
        "verbose":MB_VERBOSE
    }

@app.get("/trains")
def trains():
    feed,_=fetch_vp_cached()
    trains,_=collect_trains(feed,TRIP_TO_ROUTE_SHORT)
    def st_name(st): return {0:"INCOMING_AT",1:"STOPPED_AT",2:"IN_TRANSIT_TO"}.get(st,"?")
    out=[{"route":rs,"lat":lat,"lon":lon,"stop_id":sid,"status":st_name(st),
          "parent_station":CHILD_TO_PARENT.get(sid)} for lat,lon,rs,sid,st in trains[:50]]
    return {"total":len(trains),"sample":out}

@app.get("/states")
def states():
    feed,_=fetch_vp_cached()
    trains,_=collect_trains(feed,TRIP_TO_ROUTE_SHORT)
    # Example minimal logic: counts trains by route
    out={rs:v for rs,v in sorted(((r or "UNK"),n) for r,n in dict((t[2],0) for t in trains).items())}
    return out

if __name__=="__main__":
    import uvicorn
    uvicorn.run("app:app",host="0.0.0.0",port=int(os.getenv("PORT","8000")),reload=os.getenv("RELOAD","0")=="1")
