# app.py — Metroboard Backend (GTFS-accurate, scheduled only)
# ------------------------------------------------------------
# ✅ Reads TfNSW GTFS static data (routes.txt, trips.txt, stops.txt, stop_times.txt)
# ✅ Merges TfNSW GTFS-RT Vehicle Positions (Sydney Trains + Metro)
# ✅ Derives route_short_name directly from GTFS (no lookup table)
# ✅ Filters out NonTimetabled/Test trains
# ✅ Computes LED states (solid/near/far) based on same-line trains
# ✅ Includes full station→route mapping for /states logic
# ------------------------------------------------------------

import os, time, csv, json, typing as T, requests
from math import radians, sin, cos, asin, sqrt
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
API_TOKEN = os.environ["TFNSW_API_TOKEN"]
URLS_VP = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]

D_SOLID, D_NEAR, D_FAR = 200, 800, 1500
FEED_CACHE_SEC = 3
MB_VERBOSE = True
MB_FORCE_DISTANCE = False

DEFAULT_SYD_BBOX = (-34.2, 150.5, -33.4, 151.4)

def vlog(*a): 
    if MB_VERBOSE: print("[metroboard]", *a)

EXCLUDE_SUB = ("wharf", "light rail", "b-line", "interchange", "motorway")

# ---------------- Utilities ----------------
def _session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=4, backoff_factor=0.6,
        status_forcelist=(500,502,503,504), allowed_methods=frozenset(["GET"]))))
    return s

def hav_m(a,b,c,d):
    try: a,b,c,d = map(float,(a,b,c,d))
    except: return float("inf")
    R = 6371000.0
    da, db = radians(c-a), radians(d-b)
    t = sin(da/2)**2 + cos(radians(a))*cos(radians(c))*sin(db/2)**2
    return 2*R*asin(sqrt(max(0.0,min(1.0,t))))

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

def load_station_index(stops="stops.txt", stop_times="stop_times.txt", trips="trips.txt", routes="routes.txt"):
    """
    Builds STATION_INDEX: name → {stop_id, lat, lon, route_short}
    Uses stop_times→trips→routes to determine each station's primary route_short.
    """
    idx = {}
    stops_data = {r["stop_id"]: r for r in csv.DictReader(open(stops,encoding="utf-8-sig"))}
    trip_to_short = load_trip_to_short(trips, routes)
    stop_to_route = {}

    # Derive route from stop_times
    with open(stop_times,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=r.get("stop_id"); tid=r.get("trip_id")
            if sid and tid and tid in trip_to_short:
                stop_to_route.setdefault(sid, trip_to_short[tid])

    for s in stops_data.values():
        if s.get("location_type")!="1": continue
        name=clean_name(s.get("stop_name",""))
        if not name or any(x in name.lower() for x in EXCLUDE_SUB): continue
        sid=s["stop_id"]
        lat,lon=float(s.get("stop_lat",0)),float(s.get("stop_lon",0))
        route=stop_to_route.get(sid)
        idx[name]={
            "stop_id":sid,
            "lat":lat,
            "lon":lon,
            "route_short":route
        }
    vlog(f"station_index: {len(idx)} top-level stops")
    return idx

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
def collect_trains(feed: rt.FeedMessage, trip_to_short: dict[str, str]):
    trains=[]; by_route={}; dropped=0
    for e in feed.entity:
        if not e.HasField("vehicle"): continue
        v=e.vehicle
        tripid=v.trip.trip_id if v.HasField("trip") else ""
        rid=v.trip.route_id if v.HasField("trip") else ""

        # Skip non-scheduled
        if tripid.startswith(("NonTimetabled","Test")) or rid.startswith(("NonTimetabled","Test")):
            dropped+=1; continue

        rs=trip_to_short.get(tripid)
        if not rs: continue

        sid=getattr(v,"stop_id","")
        st=int(getattr(v,"current_status",rt.VehiclePosition.IN_TRANSIT_TO))
        lat=getattr(v.position,"latitude",None) if v.HasField("position") else None
        lon=getattr(v.position,"longitude",None) if v.HasField("position") else None

        trains.append((lat,lon,rs,sid,st))
        by_route[rs]=by_route.get(rs,0)+1
    vlog(f"collect_trains kept={len(trains)} dropped={dropped} by_route={len(by_route)}")
    return trains,by_route

# ---------------- FastAPI ----------------
app=FastAPI()
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])

ROUTE_ID_TO_SHORT={}; TRIP_TO_ROUTE_SHORT={}; CHILD_TO_PARENT={}; STATION_INDEX={}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, TRIP_TO_ROUTE_SHORT, CHILD_TO_PARENT, STATION_INDEX
    ROUTE_ID_TO_SHORT=load_route_shortnames("routes.txt")
    TRIP_TO_ROUTE_SHORT=load_trip_to_short("trips.txt","routes.txt")
    CHILD_TO_PARENT=build_child_to_parent("stops.txt")
    STATION_INDEX=load_station_index("stops.txt","stop_times.txt","trips.txt","routes.txt")
    vlog("startup complete")

@app.get("/")
def root(): 
    return {"ok":True,"endpoints":["/states","/trains","/debug","/healthz"]}

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
        "feeds":URLS_VP,
        "stations":len(STATION_INDEX)
    }

@app.get("/trains")
def trains():
    feed,_=fetch_vp_cached()
    trains,_=collect_trains(feed,TRIP_TO_ROUTE_SHORT)
    def st_name(st): return {0:"INCOMING_AT",1:"STOPPED_AT",2:"IN_TRANSIT_TO"}.get(st,"?")
    out=[{"route":rs,"lat":lat,"lon":lon,"stop_id":sid,"status":st_name(st)} for lat,lon,rs,sid,st in trains[:80]]
    return {"total":len(trains),"sample":out}

@app.get("/states")
def states(only: T.Optional[str] = Query(None, description="Comma-separated station names")):
    """
    Full LED state computation per station.
    """
    subset=STATION_INDEX
    if only:
        wanted=[x.strip() for x in only.split(",") if x.strip()]
        subset={n:STATION_INDEX[n] for n in wanted if n in STATION_INDEX}

    feed,_=fetch_vp_cached()
    trains,_=collect_trains(feed,TRIP_TO_ROUTE_SHORT)
    if not trains: return JSONResponse({k:0 for k in subset.keys()})

    VP_INCOMING=rt.VehiclePosition.INCOMING_AT
    VP_STOPPED=rt.VehiclePosition.STOPPED_AT
    out={}; matches=dist_fb=0

    for name,meta in subset.items():
        pid=meta["stop_id"]; slat,slon=meta["lat"],meta["lon"]
        station_route=(meta.get("route_short") or "").upper()
        best=0; best_d=None
        for (tlat,tlon,train_route,child_sid,st) in trains:
            if train_route and station_route and train_route!=station_route: continue
            if not MB_FORCE_DISTANCE and child_sid:
                parent=CHILD_TO_PARENT.get(child_sid) or child_sid
                if parent==pid:
                    if st==VP_STOPPED: best=3; matches+=1; break
                    elif st==VP_INCOMING: best=max(best,2); matches+=1
            if best<3 and tlat is not None and tlon is not None:
                d=hav_m(slat,slon,tlat,tlon)
                if best_d is None or d<best_d: best_d=d
        if best==0 and best_d is not None:
            best=status_from_dist(best_d)
            if best>0: dist_fb+=1
        out[name]=best
    vlog(f"states done active={sum(1 for v in out.values() if v>0)} matches={matches} dist_fb={dist_fb}")
    return JSONResponse(out)

if __name__=="__main__":
    import uvicorn
    uvicorn.run("app:app",host="0.0.0.0",port=int(os.getenv("PORT","8000")))
