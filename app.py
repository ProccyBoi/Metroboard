# app.py — Metroboard (TU-based) with single GTFS source (no static 404s, no stop_times merge)
import os, io, zipfile, asyncio, csv, json, re, shutil
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
POLL_SECONDS  = float(os.getenv("POLL_SECONDS", "12"))

# GitHub release (single authoritative pack)
GH_OWNER = os.getenv("GTFS_GH_OWNER", "")
GH_REPO  = os.getenv("GTFS_GH_REPO",  "")
GH_TAG   = os.getenv("GTFS_GH_TAG",   "")
GH_ASSET = os.getenv("GTFS_GH_ASSET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# DO NOT set GTFS_STATIC_ZIP_URL; we intentionally ignore it to avoid 404 churn
# If you insist on a fallback, set FALLBACK_STATIC_ZIP_URL (must be valid), else leave blank.
FALLBACK_STATIC_ZIP_URL = os.getenv("FALLBACK_STATIC_ZIP_URL", "").strip()

MB_VERBOSE = os.getenv("MB_VERBOSE", "0") in ("1","true","yes")

ENDPOINTS = {
    "tu_trains": "https://api.transport.nsw.gov.au/v2/gtfs/realtime/sydneytrains",
    "tu_metro":  "https://api.transport.nsw.gov.au/v2/gtfs/realtime/metro",
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

@dataclass
class TrainObs:
    trip_id: str
    route_id: str
    route_short: str
    upcoming_stop_ids: List[str]

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
            return f"T{int(s[3])}"
    return None

# Aliases from env (optional)
def _normalize_alias_map(raw: dict) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for k, vals in raw.items():
        nk = normalize_line_code(k) or k
        arr = []
        vv = vals if isinstance(vals, list) else [vals]
        for v in vv:
            nv = normalize_line_code(v) or v
            arr.append(nv)
        if nk not in arr: arr.append(nk)
        out[nk] = list(dict.fromkeys(arr))
    return out

try:
    LINE_ALIASES = _normalize_alias_map(json.loads(os.getenv("LINE_ALIASES_JSON","{}")))
except Exception:
    LINE_ALIASES = {}

STRICT_LINE_MATCH = os.getenv("STRICT_LINE_MATCH","0").lower() in ("1","true","yes")

# Optional explicit overrides
try:
    ROUTE_SHORT_OVERRIDES = json.loads(os.getenv("ROUTE_SHORT_OVERRIDES_JSON","{}"))
except Exception:
    ROUTE_SHORT_OVERRIDES = {}

# -----------------------------
# Static GTFS (routes/trips/stops only)
# -----------------------------
class StaticGTFS:
    def __init__(self, gtfs_dir: str):
        self.routes: Dict[str, Route] = {}
        self.trips:  Dict[str, Trip]  = {}
        self.stops:  Dict[str, Stop]  = {}
        self.parent_to_name: Dict[str,str] = {}
        self.stopname_to_parentid: Dict[str,str] = {}
        self._name_norm_to_parentid: Dict[str,str] = {}
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
        # If already present, use it.
        if all((Path(d)/f).exists() for f in ("routes.txt","trips.txt","stops.txt")):
            return

        data = self._download_release_asset(GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, GITHUB_TOKEN)
        if data is None and FALLBACK_STATIC_ZIP_URL:
            print("[metroboard] GH download unavailable. Using fallback static zip…")
            s=_sess()
            resp = s.get(FALLBACK_STATIC_ZIP_URL, timeout=300)
            if resp.status_code == 200:
                data = resp.content
            else:
                print("[metroboard] WARN: fallback static zip failed", resp.status_code)

        if data:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(d)
            self._promote_inner_gtfs_files(d)
            vlog("GTFS extracted to", d)
        else:
            raise FileNotFoundError("No static GTFS available (GH + fallback both unavailable)")

    def _promote_inner_gtfs_files(self, d: str):
        need = {"routes.txt","trips.txt","stops.txt"}
        root = Path(d)
        if all((root/f).exists() for f in need): return
        for cand in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts)):
            if all((cand/f).exists() for f in need):
                for f in need:
                    src, dst = cand/f, root/f
                    if not dst.exists(): shutil.move(str(src), str(dst))
                return

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
            # cope with BOM in header
            fns=[h.strip().lstrip("\ufeff") for h in (r.fieldnames or [])]
            if "stop_id" not in fns: raise ValueError("stops.txt missing stop_id")
            for row in r:
                sid=(row.get("stop_id") or row.get("\ufeffstop_id") or "").strip()
                name=(row.get("stop_name") or "").strip()
                parent=(row.get("parent_station") or "").strip() or None
                if sid: self.stops[sid]=Stop(sid,name,parent)

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
        for name, parents in name_to_parents.items():
            pid = sorted(parents)[0]
            self.stopname_to_parentid[name] = pid
            self._name_norm_to_parentid[self._norm(name)] = pid

    def parent_of(self, stop_id: str) -> str:
        st = self.stops.get(stop_id)
        if st: return st.parent_station or st.stop_id
        # lenient numeric prefix fallback (e.g. "2000123")
        digits = "".join(ch for ch in stop_id if ch.isdigit())
        return digits or stop_id

    def parent_by_name(self, name: str) -> Optional[str]:
        p = self.stopname_to_parentid.get(name)
        if p: return p
        return self._name_norm_to_parentid.get(self._norm(name))

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
        by_name_line:   Counter[Tuple[str,str]] = Counter()
        by_parent_only: Counter[str] = Counter()
        by_name_only:   Counter[str] = Counter()

        name_norm_by_parent: Dict[str,str] = {}

        for t in self.trains:
            line = t.route_short
            if not line: continue
            seen_pl, seen_nl, seen_p, seen_n = set(), set(), set(), set()
            for sid in t.upcoming_stop_ids:
                parent = self.gtfs.parent_of(sid)
                if not parent: continue
                if parent not in name_norm_by_parent:
                    pname = self.gtfs.parent_to_name.get(parent) or ""
                    name_norm_by_parent[parent] = self.gtfs._norm(pname)
                nm = name_norm_by_parent[parent]
                kpl=(parent,line)
                if kpl not in seen_pl: by_parent_line[kpl]+=1; seen_pl.add(kpl)
                knl=(nm,line)
                if nm and knl not in seen_nl: by_name_line[knl]+=1; seen_nl.add(knl)
                if parent not in seen_p: by_parent_only[parent]+=1; seen_p.add(parent)
                if nm and nm not in seen_n: by_name_only[nm]+=1; seen_n.add(nm)

        def parse_tok(tok: str):
            tok=tok.strip()
            if not tok: return None
            explicit_id=None; name_part=tok
            if "(" in tok and tok.endswith(")"):
                left=tok.rfind("("); maybe=tok[left+1:-1].strip()
                if maybe.isdigit(): explicit_id=maybe; name_part=tok[:left].strip()
            # split base name + suffix
            i=len(name_part)-1
            while i>=0 and (name_part[i].isdigit() or name_part[i].upper()=="M"): i-=1
            base=name_part[:i+1].strip(); sfx=name_part[i+1:].strip()
            line=self._suffix_to_line_code(sfx or "")
            if not line: return None
            parent=explicit_id or self.gtfs.parent_by_name(base)
            norm=self.gtfs._norm(base)
            return parent, line, norm

        out: Dict[str,int]={}
        for tok in tokens:
            parsed=parse_tok(tok)
            if not parsed: out[tok]=0; continue
            parent,line,norm=parsed
            aliases=LINE_ALIASES.get(line,[line])

            total=0
            if parent:
                total=sum(by_parent_line.get((parent,a),0) for a in aliases)
            if not total and norm:
                total=sum(by_name_line.get((norm,a),0) for a in aliases)
            if not total and not STRICT_LINE_MATCH and parent:
                total=by_parent_only.get(parent,0)
            if not total and not STRICT_LINE_MATCH and norm:
                total=by_name_only.get(norm,0)
            out[tok]=total
        return out

# -----------------------------
# GTFS-RT fetch + parse
# -----------------------------
def _fetch_bytes(url: str) -> bytes:
    s=_sess()
    headers={"Accept":"application/x-protobuf"}
    if TFNSW_API_KEY: headers["Authorization"]=f"apikey {TFNSW_API_KEY}"
    r=s.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.content

def fetch_tu(url: str) -> rt.FeedMessage:
    raw=_fetch_bytes(url)
    feed=rt.FeedMessage(); feed.ParseFromString(raw); return feed

def collect_pairs(gtfs: StaticGTFS, feed: rt.FeedMessage, label: str)->List[Tuple[str,str]]:
    out=[]; kept=0
    for e in feed.entity:
        if not e.HasField("trip_update"): continue
        tu=e.trip_update
        tid=tu.trip.trip_id; rid=tu.trip.route_id
        if not rid and tid in gtfs.trips: rid=gtfs.trips[tid].route_id
        if tid and rid: out.append((tid,rid)); kept+=1
    vlog(f"collect_trips {label}: kept={kept}")
    return out

def extract_upcoming(feed: rt.FeedMessage)->Dict[str,List[str]]:
    m={}
    for e in feed.entity:
        if not e.HasField("trip_update"): continue
        tu=e.trip_update; tid=tu.trip.trip_id
        if not tid: continue
        stops=[stu.stop_id for stu in tu.stop_time_update if stu.stop_id]
        if stops: m[tid]=stops
    return m

def route_short_from_ids(route_id: str, route_short: str, trip_id: Optional[str]=None)->Optional[str]:
    cand=(route_short or "").strip()
    if route_id and route_id in ROUTE_SHORT_OVERRIDES:
        cand=ROUTE_SHORT_OVERRIDES[route_id]
    if not cand and route_id: cand=route_id
    short=normalize_line_code(cand)
    if not short: short=infer_line_code_from_text(route_id, trip_id)
    return short

def build_obs(gtfs: StaticGTFS, pairs: List[Tuple[str,str]], upcoming: Dict[str,List[str]])->List[TrainObs]:
    out=[]
    for tid,rid in pairs:
        r=gtfs.routes.get(rid)
        short=route_short_from_ids(rid, r.short if r else "", trip_id=tid)
        stops=upcoming.get(tid, [])
        if not short or not stops: continue
        out.append(TrainObs(tid, rid, short, stops))
    return out

# -----------------------------
# Background loop
# -----------------------------
async def refresh_loop(state: LiveState):
    if not TFNSW_API_KEY:
        print("[metroboard] WARN: TFNSW_API_KEY not set; realtime fetch will fail.")
    while True:
        try:
            trains_feed, metro_feed = await asyncio.gather(
                asyncio.to_thread(fetch_tu, ENDPOINTS["tu_trains"]),
                asyncio.to_thread(fetch_tu, ENDPOINTS["tu_metro"]),
            )
            p_tr = collect_pairs(state.gtfs, trains_feed, "trains")
            p_me = collect_pairs(state.gtfs, metro_feed,  "metro")
            u_tr = extract_upcoming(trains_feed)
            u_me = extract_upcoming(metro_feed)
            state.set_trains(build_obs(state.gtfs, p_tr, u_tr) + build_obs(state.gtfs, p_me, u_me))
            vlog("refresh: trains=", len(state.trains), " static=", state.gtfs.available)
        except Exception as e:
            print("[metroboard] refresh error:", e)
        await asyncio.sleep(POLL_SECONDS)

# -----------------------------
# FastAPI app
# -----------------------------
gtfs = StaticGTFS(GTFS_DIR)
state = LiveState(gtfs)
app = FastAPI(title="Metroboard")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(refresh_loop(state))
    print("Application startup complete.")

@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "trains_cached": len(state.trains),
        "static_gtfs_available": state.gtfs.available,
        "gtfs_dir": GTFS_DIR,
    }

@app.get("/states")
async def states(only: Optional[str] = Query(default=None)):
    if not only or not state.gtfs.available:
        return JSONResponse({})
    tokens=[t for t in only.split(",") if t.strip()]
    return JSONResponse(state.counts_for_tokens(tokens))

# run with: uvicorn app:app --host 0.0.0.0 --port 8000
