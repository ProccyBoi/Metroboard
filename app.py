# app.py — Metroboard (TU+VP join)
# - Minimal static GTFS (routes/trips/stops) from a single GH asset or one fallback URL
# - Uses TU to infer line short codes (T1..T9/M), VP to locate trains (stop_id or lat/lon)
# - Counts by parent station + line; optional fallback to any-line when STRICT_LINE_MATCH=0
# - No stop_times merge; no multi-GB memory spikes; no 404 loops

import os, io, zipfile, asyncio, csv, json, re, math
from pathlib import Path
from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2 as rt

# -----------------------------
# Config
# -----------------------------
TFNSW_API_KEY = os.getenv("TFNSW_API_KEY", "")
GTFS_DIR      = os.getenv("GTFS_DIR", "./gtfs")
POLL_SECONDS  = float(os.getenv("POLL_SECONDS", "10"))

# GitHub release (single authoritative pack)
GH_OWNER = os.getenv("GTFS_GH_OWNER", "")
GH_REPO  = os.getenv("GTFS_GH_REPO",  "")
GH_TAG   = os.getenv("GTFS_GH_TAG",   "")
GH_ASSET = os.getenv("GTFS_GH_ASSET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Optional single fallback if GH asset is unavailable (must be valid)
FALLBACK_STATIC_ZIP_URL = os.getenv("FALLBACK_STATIC_ZIP_URL", "").strip()

# Distance thresholds for VP geofallback (meters)
D_SOLID = int(os.getenv("MB_D_SOLID", "250"))
D_NEAR  = int(os.getenv("MB_D_NEAR",  "900"))
D_FAR   = int(os.getenv("MB_D_FAR",   "1600"))

MB_VERBOSE = os.getenv("MB_VERBOSE", "0").lower() in ("1","true","yes")

ENDPOINTS_TU = {
    "trains": "https://api.transport.nsw.gov.au/v2/gtfs/realtime/sydneytrains",
    "metro":  "https://api.transport.nsw.gov.au/v2/gtfs/realtime/metro",
}
ENDPOINTS_VP = {
    "trains": "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "metro":  "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
}

def vlog(*a):
    if MB_VERBOSE: print("[metroboard]", *a)

# -----------------------------
# HTTP helpers
# -----------------------------
def _sess():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=0.5, status_forcelist=(500,502,503,504),
        allowed_methods=frozenset(["GET"]), raise_on_status=False)))
    return s

def _fetch_bytes(url: str) -> bytes:
    s=_sess()
    headers={"Accept":"application/x-protobuf"}
    if TFNSW_API_KEY: headers["Authorization"]=f"apikey {TFNSW_API_KEY}"
    r=s.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.content

# -----------------------------
# Models
# -----------------------------
@dataclass(frozen=True)
class Route:
    route_id: str
    short: str

@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str

@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    parent_station: Optional[str]
    lat: Optional[float]
    lon: Optional[float]

@dataclass
class TrainObs:
    route_short: Optional[str]
    parent_guess: Optional[str]

# -----------------------------
# Line helpers
# -----------------------------
def normalize_line_code(val: str) -> Optional[str]:
    if not val: return None
    s = val.strip().upper()
    if s in ("M","M1","METRO","S_M1"): return "M"
    if s.startswith("T") and s[1:].isdigit(): return f"T{int(s[1:])}"
    if s.isdigit(): return f"T{int(s)}"
    if s.startswith("S_T"):
        digits = "".join(ch for ch in s[3:] if ch.isdigit())
        if digits: return f"T{int(digits)}"
    return None

_LINE_PAT = re.compile(r'(?:\b|_)(T)(\d{1,2})(?:\b|_)', re.I)
def infer_line_code_from_text(*texts: str) -> Optional[str]:
    for t in texts:
        if not t: continue
        s = t.upper()
        if "S_M1" in s or " METRO" in s or s.endswith("M1") or s == "M1" or s == "M":
            return "M"
        m = _LINE_PAT.search(s)
        if m: return f"T{int(m.group(2))}"
        if s.startswith("S_T") and len(s) > 3 and s[3].isdigit():
            # FIXED: removed stray bracket
            return f"T{int(s[3])}"
    return None

def route_short_from_ids(route_id: str, route_short: str, trip_id: Optional[str]=None)->Optional[str]:
    cand=(route_short or "").strip()
    if not cand and route_id: cand=route_id
    short=normalize_line_code(cand)
    if not short: short=infer_line_code_from_text(route_id, trip_id)
    return short

# Aliases from env (optional)
def _normalize_alias_map(raw: dict) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for k, vals in raw.items():
        nk = normalize_line_code(k) or k
        vv = vals if isinstance(vals, list) else [vals]
        arr = [(normalize_line_code(v) or v) for v in vv]
        if nk not in arr: arr.append(nk)
        out[nk] = list(dict.fromkeys(arr))
    return out

try:
    LINE_ALIASES = _normalize_alias_map(json.loads(os.getenv("LINE_ALIASES_JSON","{}")))
except Exception:
    LINE_ALIASES = {}

STRICT_LINE_MATCH = os.getenv("STRICT_LINE_MATCH","1").lower() in ("1","true","yes")

# -----------------------------
# Minimal static GTFS (routes/trips/stops only)
# -----------------------------
def _merge_minimal_gtfs(zip_bytes: bytes, out_dir: str):
    """Merge ALL instances of routes.txt, trips.txt, stops.txt from the zip (across subfolders)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()

        def want(fname: str, target: str) -> bool:
            nl = fname.lower()
            return nl == target or nl.endswith("/" + target)

        plan = {
            "routes.txt": {"key": ["route_id"], "rows": [], "seen": set(), "headers": []},
            "trips.txt":  {"key": ["trip_id"],  "rows": [], "seen": set(), "headers": []},
            "stops.txt":  {"key": ["stop_id"],  "rows": [], "seen": set(), "headers": []},
        }

        for target in plan.keys():
            members = [n for n in names if want(n, target)]
            if not members:
                raise RuntimeError(f"{target} not found anywhere in GTFS zip")
            for m in members:
                with z.open(m) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                    rdr = csv.DictReader(text)
                    if not rdr.fieldnames:
                        continue
                    hdrs = [h for h in rdr.fieldnames if h is not None]
                    plan[target]["headers"] = list(dict.fromkeys(plan[target]["headers"] + hdrs))
                    for row in rdr:
                        k = tuple((row.get(kf) or "").strip() for kf in plan[target]["key"])
                        if not all(k) or k in plan[target]["seen"]:
                            continue
                        plan[target]["seen"].add(k)
                        plan[target]["rows"].append({(kk or "").strip(): (vv or "").strip() for kk, vv in row.items()})

        for target, agg in plan.items():
            keys = [h for h in plan[target]["key"] if h in agg["headers"]]
            others = [h for h in agg["headers"] if h not in keys]
            headers = keys + others
            with (Path(out_dir)/target).open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=headers)
                w.writeheader()
                for row in agg["rows"]:
                    for h in headers: row.setdefault(h, "")
                    w.writerow(row)

    vlog(f"merged routes={len(plan['routes.txt']['rows'])}, trips={len(plan['trips.txt']['rows'])}, stops={len(plan['stops.txt']['rows'])}")

class StaticGTFS:
    def __init__(self, gtfs_dir: str):
        self.routes: Dict[str, Route] = {}
        self.trips:  Dict[str, Trip]  = {}
        self.stops:  Dict[str, Stop]  = {}
        self.parent_to_name: Dict[str,str] = {}
        self._name_norm_to_parentid: Dict[str,str] = {}
        self.parent_coords: Dict[str, Tuple[Optional[float],Optional[float]]] = {}
        self.available = False

        self._ensure_dir_or_download(gtfs_dir)
        try:
            self._load(gtfs_dir)
            self._build_name_parent_index()
            self.available = True
            vlog("static GTFS loaded from", gtfs_dir)
        except Exception as e:
            print("[metroboard] WARN: static GTFS not available:", e)

    @staticmethod
    def _download_release_asset(owner, repo, tag, asset_name, token) -> Optional[bytes]:
        if not (owner and repo and tag and asset_name and token):
            return None
        api_base = f"https://api.github.com/repos/{owner}/{repo}"
        s = _sess()
        h_json = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        r = s.get(f"{api_base}/releases/tags/{tag}", headers=h_json, timeout=60)
        if r.status_code != 200:
            print("[metroboard] WARN: GH release fetch failed", r.status_code, r.text[:200])
            return None
        data = r.json()
        asset = next((a for a in data.get("assets",[]) if a.get("name")==asset_name), None)
        if not asset:
            print(f"[metroboard] WARN: asset {asset_name} not found on tag {tag}")
            return None
        asset_id = asset["id"]
        h_bin = {"Authorization": f"token {token}", "Accept": "application/octet-stream"}
        dl = s.get(f"{api_base}/releases/assets/{asset_id}", headers=h_bin, timeout=300)
        if dl.status_code != 200:
            print("[metroboard] WARN: GH asset download failed", dl.status_code)
            return None
        return dl.content

    def _ensure_dir_or_download(self, d: str):
        Path(d).mkdir(parents=True, exist_ok=True)
        if all((Path(d)/f).exists() for f in ("routes.txt","trips.txt","stops.txt")):
            return
        data = self._download_release_asset(GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, GITHUB_TOKEN)
        if data is None and FALLBACK_STATIC_ZIP_URL:
            print("[metroboard] GH download unavailable. Using fallback static zip…")
            s=_sess(); resp = s.get(FALLBACK_STATIC_ZIP_URL, timeout=300)
            if resp.status_code == 200:
                data = resp.content
            else:
                print("[metroboard] WARN: fallback static zip failed", resp.status_code)
        if data:
            _merge_minimal_gtfs(data, d)
        else:
            raise FileNotFoundError("No static GTFS available (GH + fallback both unavailable)")

    def _load(self, d: str):
        def op(p: str):
            q = Path(d)/p
            if not q.exists(): raise FileNotFoundError(f"Missing {p} in {d}")
            return q.open(newline="", encoding="utf-8-sig")

        with op("routes.txt") as f:
            r=csv.DictReader(f)
            for row in r:
                rid = (row.get("route_id") or "").strip()
                short = (row.get("route_short_name") or "").strip()
                if rid: self.routes[rid] = Route(rid, short)

        with op("trips.txt") as f:
            r=csv.DictReader(f)
            for row in r:
                tid=(row.get("trip_id") or "").strip()
                rid=(row.get("route_id") or "").strip()
                if tid and rid: self.trips[tid] = Trip(tid, rid)

        with op("stops.txt") as f:
            r=csv.DictReader(f)
            fns=[h.strip().lstrip("\ufeff") for h in (r.fieldnames or [])]
            if "stop_id" not in fns: raise ValueError("stops.txt missing stop_id")
            for row in r:
                sid=(row.get("stop_id") or row.get("\ufeffstop_id") or "").strip()
                name=(row.get("stop_name") or "").strip()
                parent=(row.get("parent_station") or "").strip() or None
                lat=None; lon=None
                try:
                    lat=float(row.get("stop_lat","") or "nan")
                    lon=float(row.get("stop_lon","") or "nan")
                    if math.isnan(lat) or math.isnan(lon): lat=None; lon=None
                except Exception:
                    lat=lon=None
                if sid: self.stops[sid]=Stop(sid,name,parent,lat,lon)

    def _norm(self, s: str)->str:
        s=s.strip().lower()
        for suf in (" station"," railway station"):
            if s.endswith(suf): s=s[:-len(suf)]
        s=s.replace("&","and")
        s="".join(ch for ch in s if ch.isalnum() or ch.isspace())
        return " ".join(s.split())

    def _build_name_parent_index(self):
        name_to_parents: Dict[str, Set[str]] = defaultdict(set)
        for st in self.stops.values():
            parent = st.parent_station or st.stop_id
            name_to_parents[st.name].add(parent)
            self.parent_to_name.setdefault(parent, st.name)

        # parent coords prefer parent rows; otherwise use any child's coords
        for st in self.stops.values():
            pid = st.parent_station or st.stop_id
            if pid not in self.parent_coords:
                self.parent_coords[pid] = (st.lat, st.lon)
            else:
                plat, plon = self.parent_coords[pid]
                if (plat is None or plon is None) and (st.lat is not None and st.lon is not None):
                    self.parent_coords[pid] = (st.lat, st.lon)

        self._name_norm_to_parentid = {}
        for name, parents in name_to_parents.items():
            pid = sorted(parents)[0]
            self._name_norm_to_parentid[self._norm(name)] = pid

    def parent_of(self, stop_id: str) -> Optional[str]:
        st = self.stops.get(stop_id)
        if st: return st.parent_station or st.stop_id
        digits = "".join(ch for ch in stop_id if ch.isdigit())
        return digits or None

    def parent_by_name(self, name: str) -> Optional[str]:
        return self._name_norm_to_parentid.get(self._norm(name))

# -----------------------------
# Geo helpers
# -----------------------------
def _valid_ll(x,y): 
    try: x=float(x); y=float(y)
    except: return False
    return -90<=x<=90 and -180<=y<=180

def haversine_m(a,b,c,d):
    try: a=float(a); b=float(b); c=float(c); d=float(d)
    except: return float("inf")
    if not (_valid_ll(a,b) and _valid_ll(c,d)): return float("inf")
    from math import radians,sin,cos,asin,sqrt
    R=6371000.0; da=radians(c-a); db=radians(d-b)
    t=sin(da/2)**2+cos(radians(a))*cos(radians(c))*sin(db/2)**2
    return 2*R*asin(sqrt(max(0.0,min(1.0,t))))

# -----------------------------
# Live state & matching
# -----------------------------
class LiveState:
    def __init__(self, gtfs: StaticGTFS):
        self.gtfs = gtfs
        self.trains: List[TrainObs] = []

    def set_trains(self, trains: List[TrainObs]): self.trains = trains

    @staticmethod
    def _suffix_to_line_code(sfx: str)->Optional[str]:
        s=sfx.strip().upper()
        if s=="M": return "M"
        if s.startswith("T") and s[1:].isdigit(): return s
        if s.isdigit(): return f"T{s}"
        return None

    def counts_for_tokens(self, tokens: List[str]) -> Dict[str,int]:
        by_parent_line: Counter[Tuple[str,str]] = Counter()
        by_parent_only: Counter[str] = Counter()

        for t in self.trains:
            if not t.parent_guess: continue
            if t.route_short:
                by_parent_line[(t.parent_guess, t.route_short)] += 1
            by_parent_only[t.parent_guess] += 1

        def parse_tok(tok: str):
            tok=tok.strip()
            if not tok: return None
            # "Central4" or "Central4 (2000…)"
            explicit_id=None; name_part=tok
            if "(" in tok and tok.endswith(")"):
                left=tok.rfind("("); maybe=tok[left+1:-1].strip()
                if maybe.isdigit(): explicit_id=maybe; name_part=tok[:left].strip()
            i=len(name_part)-1
            while i>=0 and (name_part[i].isdigit() or name_part[i].upper()=="M"): i-=1
            base=name_part[:i+1].strip(); sfx=name_part[i+1:].strip()
            line=self._suffix_to_line_code(sfx or "")
            if not line: return None
            parent=explicit_id or self.gtfs.parent_by_name(base)
            return parent, line

        out: Dict[str,int]={}
        for tok in tokens:
            parsed=parse_tok(tok)
            if not parsed: out[tok]=0; continue
            parent,line=parsed
            aliases=LINE_ALIASES.get(line,[line])

            total=0
            if parent:
                total=sum(by_parent_line.get((parent,a),0) for a in aliases)
                if not total and not STRICT_LINE_MATCH:
                    total=by_parent_only.get(parent,0)
            out[tok]=total
        return out

# -----------------------------
# TU + VP fetch/parse
# -----------------------------
def fetch_tu(url: str) -> rt.FeedMessage:
    feed=rt.FeedMessage(); feed.ParseFromString(_fetch_bytes(url)); return feed

def fetch_vp(url: str) -> rt.FeedMessage:
    feed=rt.FeedMessage(); feed.ParseFromString(_fetch_bytes(url)); return feed

def build_line_map_from_tu(gtfs: StaticGTFS, feed: rt.FeedMessage, label: str) -> Dict[str,str]:
    """trip_id -> short line code (T#/M) derived from TU route_id/short."""
    m: Dict[str,str] = {}
    kept = 0
    for e in feed.entity:
        if not e.HasField("trip_update"): continue
        tu = e.trip_update
        tid = tu.trip.trip_id
        rid = tu.trip.route_id
        if not tid: continue
        short = route_short_from_ids(rid, gtfs.routes.get(rid, Route(rid,"")).short if rid in gtfs.routes else "", tid)
        if short:
            m[tid] = short
            kept += 1
    vlog(f"line_map_from_tu {label}: trips_with_label={kept}")
    return m

def infer_parent_for_vp(gtfs: StaticGTFS, stop_id: Optional[str], lat: Optional[float], lon: Optional[float]) -> Optional[str]:
    # 1) exact via stop_id -> parent
    if stop_id:
        p = gtfs.parent_of(stop_id)
        if p: return p
    # 2) geofallback nearest parent within FAR
    if lat is None or lon is None: return None
    best=None; bestd=float("inf")
    for pid,(slat,slon) in gtfs.parent_coords.items():
        if slat is None or slon is None: continue
        d = haversine_m(lat, lon, slat, slon)
        if d < bestd:
            bestd = d; best = pid
    if best is not None and bestd <= D_FAR:
        return best
    return None

def collect_from_vp(gtfs: StaticGTFS, feed: rt.FeedMessage, line_by_trip: Dict[str,str], label: str) -> List[TrainObs]:
    keep=0; out: List[TrainObs]=[]
    for e in feed.entity:
        if not e.HasField("vehicle"): continue
        v=e.vehicle
        tid = v.trip.trip_id  if v.HasField("trip") else ""
        rid = v.trip.route_id if v.HasField("trip") else ""
        rs  = line_by_trip.get(tid) or route_short_from_ids(rid, gtfs.routes.get(rid, Route(rid,"")).short if rid in gtfs.routes else "", tid)

        sid = getattr(v, "stop_id", "") or (v.stop_id if hasattr(v,"stop_id") else "")
        lat = v.position.latitude  if v.HasField("position") else None
        lon = v.position.longitude if v.HasField("position") else None

        parent = infer_parent_for_vp(gtfs, sid, lat, lon)
        if parent:
            keep += 1
            out.append(TrainObs(route_short=rs, parent_guess=parent))
    vlog(f"collect_vp {label}: kept={keep}")
    return out

# -----------------------------
# Background loop
# -----------------------------
async def refresh_loop(state: LiveState):
    if not TFNSW_API_KEY:
        print("[metroboard] WARN: TFNSW_API_KEY not set; realtime fetch will fail.")
    while True:
        try:
            tu_trains, tu_metro, vp_trains, vp_metro = await asyncio.gather(
                asyncio.to_thread(fetch_tu, ENDPOINTS_TU["trains"]),
                asyncio.to_thread(fetch_tu, ENDPOINTS_TU["metro"]),
                asyncio.to_thread(fetch_vp, ENDPOINTS_VP["trains"]),
                asyncio.to_thread(fetch_vp, ENDPOINTS_VP["metro"]),
            )
            # line map from TU (authoritative for line labels)
            line_map = {}
            line_map.update(build_line_map_from_tu(state.gtfs, tu_trains, "trains"))
            line_map.update(build_line_map_from_tu(state.gtfs, tu_metro,  "metro"))

            # vehicles from VP (location/stop)
            obs = collect_from_vp(state.gtfs, vp_trains, line_map, "trains") \
                + collect_from_vp(state.gtfs, vp_metro,  line_map, "metro")

            state.set_trains(obs)
            vlog("refresh: vehicles=", len(state.trains), " static=", state.gtfs.available)
        except Exception as e:
            print("[metroboard] refresh error:", e)
        await asyncio.sleep(POLL_SECONDS)

# -----------------------------
# FastAPI app
# -----------------------------
def _boot_gtfs(dirpath: str) -> StaticGTFS:
    Path(dirpath).mkdir(parents=True, exist_ok=True)
    if not all((Path(dirpath)/f).exists() for f in ("routes.txt","trips.txt","stops.txt")):
        data = StaticGTFS._download_release_asset(GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, GITHUB_TOKEN)
        if data is None and FALLBACK_STATIC_ZIP_URL:
            print("[metroboard] GH download unavailable. Using fallback static zip…")
            s=_sess(); r=s.get(FALLBACK_STATIC_ZIP_URL, timeout=300)
            if r.status_code==200: data=r.content
            else: print("[metroboard] WARN: fallback static zip failed", r.status_code)
        if data:
            _merge_minimal_gtfs(data, dirpath)
            print("[metroboard] extracted minimal GTFS to", dirpath)
        else:
            raise FileNotFoundError("No static GTFS available (GH + fallback both unavailable)")
    return StaticGTFS(dirpath)

gtfs = _boot_gtfs(GTFS_DIR)
state = LiveState(gtfs)
app = FastAPI(title="Metroboard (TU+VP)")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(refresh_loop(state))
    print("Application startup complete.")

@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "vehicles_cached": len(state.trains),
        "static_gtfs_available": state.gtfs.available,
        "gtfs_dir": GTFS_DIR,
    }

@app.get("/states")
async def states(only: Optional[str] = Query(default=None)):
    if not only or not state.gtfs.available:
        return JSONResponse({})
    tokens=[t for t in only.split(",") if t.strip()]
    return JSONResponse(state.counts_for_tokens(tokens))

# run: uvicorn app:app --host 0.0.0.0 --port 8000
