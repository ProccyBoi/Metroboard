# app.py — Metroboard backend (Rail + Metro) - FIXED LINE FILTERING
# - Uses static GTFS (stops.txt, routes.txt, trips.txt, stop_times.txt, agency.txt)
# - Filters to route_type in (1=subway/metro, 2=rail); optional agency allow-list
# - Builds station×route keys like "Central1" (T1) and "CentralM" (M1)
# - GTFS-RT from TfNSW: Sydney Trains + Sydney Metro vehicle positions
# - States logic: prefer VP.stop_id -> parent match; fallback to distance thresholds
# - **FIX**: Only match trains to stations with the EXACT same route_short
# - Endpoints: /, /healthz, /debug, /stations, /states, /trains

import os
import time
import csv
import gzip
import hashlib
import html
import json
import math
import shutil
import re
import random
from datetime import datetime, time as dtime
import typing as T

import requests
from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import quote
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - python <3.9 fallback
    ZoneInfo = None

# ---------------- Config ----------------
API_TOKEN = os.getenv("TFNSW_API_TOKEN")

# Allow override via FEEDS env (comma-separated); default to both feeds
_FEEDS_ENV = os.getenv("FEEDS", "").strip()
if _FEEDS_ENV:
    URLS_VP = [u.strip() for u in _FEEDS_ENV.split(",") if u.strip()]
else:
    URLS_VP = [
        "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
        "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
    ]

# Distance thresholds (meters) for fallback
D_SOLID = int(os.getenv("MB_D_SOLID", "200"))
D_NEAR  = int(os.getenv("MB_D_NEAR",  "800"))
D_FAR   = int(os.getenv("MB_D_FAR",   "1500"))

# Allowlists (empty=set() means all)
AGENCY_NAME_ALLOW: set[str] = {a.strip() for a in os.getenv("MB_AGENCIES", "").split(",") if a.strip()}
ROUTE_SHORT_ALLOW: set[str] = {s.strip().upper() for s in os.getenv("MB_ROUTE_SHORT_ALLOW", "").split(",") if s.strip()}

# Route short-name overrides
ROUTE_SHORT_OVERRIDES_PATH = os.getenv("ROUTE_SHORT_OVERRIDES_PATH", "route_overrides.csv")
ROUTE_SHORT_OVERRIDES_JSON = os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "")
BOARD_SETTINGS_PATH = os.getenv("BOARD_SETTINGS_PATH") or os.path.join("data", "boards.json")

def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")

PERSIST_USER_SETTINGS = _env_flag("PERSIST_USER_SETTINGS", True)
FIRMWARE_OTA_ENABLED = _env_flag("FIRMWARE_OTA_ENABLED", True)
FIRMWARE_MANIFEST_PATH = os.getenv("FIRMWARE_MANIFEST_PATH", os.path.join("firmware", "manifest.json"))
FIRMWARE_PATH = os.getenv("FIRMWARE_PATH", os.path.join("firmware", "metroboard_public.bin"))
FIRMWARE_VERSION = os.getenv("FIRMWARE_VERSION", "").strip()
FIRMWARE_SKETCH_PATH = os.getenv("FIRMWARE_SKETCH_PATH", "metroboard_public.ino")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
BOARD_OVERLAY_PATH = os.getenv("BOARD_OVERLAY_PATH", os.path.join("assets", "board_overlay.png"))
BOARD_OVERLAY_URL = os.getenv("BOARD_OVERLAY_URL", "").strip()

# board_id -> settings dict
BOARD_SETTINGS: dict[str, dict] = {}
BOARD_SETTINGS_VERSION: int = 0
BOARD_LAST_PAYLOAD_AT: dict[str, float] = {}
BOARD_FIRMWARE_STATUS: dict[str, dict] = {}

ANIMATION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("pulse", "Pulse"),
    ("line-pulse", "Line pulse"),
    ("bounce", "Bounce"),
    ("rainbow", "Rainbow"),
    ("comet", "Comet"),
    ("sparkle", "Sparkle"),
    ("theater", "Theater chase"),
    ("wipe", "Line wipe"),
    ("solid", "Solid colour"),
)
ANIMATION_NAMES = {name for name, _label in ANIMATION_OPTIONS}
ANIMATION_CUSTOM_COLOR_NAMES = {"pulse", "sparkle", "theater", "solid"}
BOARD_LAYOUT_VIEWBOX = [0, 0, 861, 850]

# Caching/CORS/logging
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE = os.getenv("MB_VERBOSE", "1") == "1"
MB_RESOLUTION_VERBOSE = os.getenv("MB_RESOLUTION_VERBOSE", "0") == "1"
MB_FORCE_DISTANCE = os.getenv("MB_FORCE_DISTANCE", "0") == "1"
MB_TIMEZONE = os.getenv("MB_TIMEZONE", "Australia/Sydney")
MB_STRICT_ROUTE_MATCH = _env_flag("MB_STRICT_ROUTE_MATCH", True)
MB_GUESS_AMBIGUOUS = _env_flag("MB_GUESS_AMBIGUOUS", True)
MB_ROUTE_NEAREST_MARGIN_M = int(os.getenv("MB_ROUTE_NEAREST_MARGIN_M", "250"))

IGNORED_ROUTE_PREFIXES = {"RTTA"}
NO_STATION_INFERENCE_PREFIXES = IGNORED_ROUTE_PREFIXES | {"CTY"}
ROUTE_PREFIX_DEFAULTS = {
    "NTH": "T9",
}
ROUTE_ID_EXACT_MAP = {
    # Confirmed against the AnyTrip route endpoint used by tfnsw.joshchans.com.
    # These operational route IDs are not present in static GTFS routes.txt.
    "CTY_NC1A": "NRC",
    "CTY_NC2": "NRC",
    "CTY_NW1D": "NRW",
    "CTY_W2A": "WST",
    "CTY_S1C": "STH",
    "CTY_S2A": "STH",
    "CTY_S2B": "STH",
}

# BBox guardrail (None disables)
DEFAULT_SYD_BBOX = (-34.2, 150.5, -33.4, 151.4)
def _bbox_env() -> T.Optional[tuple[float, float, float, float]]:
    if os.getenv("SYD_BBOX_DISABLE") == "1":
        return None
    s = os.getenv("SYD_BBOX")
    if not s:
        return DEFAULT_SYD_BBOX
    try:
        a, b, c, d = [float(x) for x in s.split(",")]
        return (a, b, c, d)
    except Exception:
        return DEFAULT_SYD_BBOX

SYD_BBOX = _bbox_env()

def vlog(*a):
    if MB_VERBOSE:
        print("[metroboard]", *a)

def route_id_prefix(route_id: str) -> str:
    return (route_id or "").split("_", 1)[0].split(".", 1)[0].upper()

def preferred_route_candidate(prefix: str, candidates: T.Iterable[str]) -> T.Optional[str]:
    ordered = [c for c in candidates if c]
    if not ordered:
        return None
    preferred = ROUTE_PREFIX_DEFAULTS.get(prefix)
    if preferred and preferred in ordered:
        return preferred
    return ordered[0]

def _resolve_local_path(path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return path
    if os.path.exists(path):
        return path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, path)

GTFS_DIR = os.getenv("GTFS_DIR", "").strip()

def _gtfs_data_path(filename: str) -> str:
    stem = os.path.splitext(filename)[0].upper()
    override = os.getenv(f"GTFS_{stem}_PATH")
    if filename == "stop_times.txt":
        override = os.getenv("STOP_TIMES_PATH") or override
    if override:
        return override
    if GTFS_DIR:
        candidate = os.path.join(GTFS_DIR, filename)
        if os.path.exists(_resolve_local_path(candidate)):
            return candidate
        if filename == "stop_times.txt" and os.path.exists(_resolve_local_path(candidate + ".gz")):
            return candidate
    candidate = os.path.join("gtfs", filename)
    if os.path.exists(_resolve_local_path(candidate)):
        return candidate
    if filename == "stop_times.txt" and os.path.exists(_resolve_local_path(candidate + ".gz")):
        return candidate
    root_candidate = _resolve_local_path(filename)
    if os.path.exists(root_candidate):
        return root_candidate
    return filename

def _stop_times_has_valid_header(path: str) -> bool:
    required = {"trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"}
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
    return not (required - fields)

def _inflate_stop_times_from_gzip(path: str) -> bool:
    gz_path = os.getenv("STOP_TIMES_GZ_PATH") or (path + ".gz")
    if not os.path.exists(gz_path):
        return False
    tmp = path + ".inflate"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(gz_path, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
    os.replace(tmp, path)
    return True

def validate_stop_times_file(path: str) -> None:
    required = {"trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"}
    if not _stop_times_has_valid_header(path):
        _inflate_stop_times_from_gzip(path)
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = set(reader.fieldnames or [])
    except FileNotFoundError:
        raise RuntimeError(f"Missing GTFS stop_times file: {path}")
    missing = required - fields
    if missing:
        found = ", ".join(reader.fieldnames or [])
        raise RuntimeError(
            f"Invalid GTFS stop_times file {path}: missing {sorted(missing)}; found [{found}]"
        )

def _load_board_settings() -> dict[str, dict]:
    if not PERSIST_USER_SETTINGS:
        vlog("persisted board settings disabled; skipping load")
        return {}
    settings_path = _resolve_local_path(BOARD_SETTINGS_PATH)
    legacy_path = _resolve_local_path("boards.json")
    if not os.path.exists(settings_path) and settings_path != legacy_path and os.path.exists(legacy_path):
        try:
            settings_dir = os.path.dirname(settings_path)
            if settings_dir:
                os.makedirs(settings_dir, exist_ok=True)
            shutil.copy2(legacy_path, settings_path)
            vlog(f"migrated legacy boards.json from {legacy_path} to {settings_path}")
        except Exception as e:
            vlog(f"WARN: failed to migrate legacy boards.json: {e}")
    if not os.path.exists(settings_path):
        vlog(f"WARN: boards.json not found at {settings_path}")
        return {}
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            normalized: dict[str, dict] = {}
            # Backward-compat: older boards.json stored brightness as a dict.
            for bid, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                b = cfg.get("brightness")
                if isinstance(b, dict):
                    # Prefer live brightness, fall back to animation.
                    live = b.get("live")
                    anim = b.get("animation")
                    if isinstance(live, (int, float)):
                        cfg["brightness"] = int(live)
                    elif isinstance(anim, (int, float)):
                        cfg["brightness"] = int(anim)
                elif isinstance(b, (int, float)):
                    cfg["brightness"] = int(b)
                normalized_id = _normalize_board_id(bid or cfg.get("board_id"))
                if not normalized_id:
                    continue
                cfg["board_id"] = normalized_id
                normalized[normalized_id] = _normalize_board_config(normalized_id, cfg)
            vlog(f"boards.json loaded: {len(normalized)} boards from {settings_path}")
            return normalized
    except Exception as e:
        vlog("WARN: failed to load boards.json:", e)
    return {}

def _save_board_settings() -> None:
    global BOARD_SETTINGS_VERSION
    BOARD_SETTINGS_VERSION += 1
    if not PERSIST_USER_SETTINGS:
        vlog("persisted board settings disabled; skipping save")
        return
    settings_path = _resolve_local_path(BOARD_SETTINGS_PATH)
    try:
        settings_dir = os.path.dirname(settings_path)
        if settings_dir:
            os.makedirs(settings_dir, exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(BOARD_SETTINGS, f, indent=2, sort_keys=True)
        vlog(f"boards.json saved to {settings_path}")
    except Exception as e:
        vlog("WARN: failed to save boards.json:", e)

def _now_local() -> datetime:
    """Current time in configured timezone; falls back to UTC if TZ invalid/unavailable."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(MB_TIMEZONE))
        except Exception:
            pass
    return datetime.utcnow()

def _iso_utc_from_ts(ts: T.Optional[float]) -> T.Optional[str]:
    if ts is None:
        return None
    return datetime.utcfromtimestamp(ts).isoformat() + "Z"

def _feed_age_seconds() -> T.Optional[float]:
    if _FEED_TS == 0:
        return None
    return max(0.0, time.time() - _FEED_TS)

def _board_status_snapshot(board_id: str) -> dict:
    last = BOARD_LAST_PAYLOAD_AT.get(board_id)
    fw = BOARD_FIRMWARE_STATUS.get(board_id, {})
    cfg = BOARD_SETTINGS.get(board_id, {}) or {}
    ota = cfg.get("ota", {}) or {}
    now = time.time()
    return {
        "board_id": board_id,
        "last_payload_at": _iso_utc_from_ts(last),
        "last_payload_age_seconds": None if last is None else max(0.0, now - last),
        "settings_version": BOARD_SETTINGS_VERSION,
        "feed_age_seconds": _feed_age_seconds(),
        "firmware": {
            "current": fw.get("current"),
            "build": fw.get("build"),
            "last_ota_result": fw.get("last_ota_result"),
            "last_ota_target": fw.get("last_ota_target"),
            "last_ota_at": _iso_utc_from_ts(fw.get("last_ota_at")),
            "last_ota_age_seconds": None if fw.get("last_ota_at") is None else max(0.0, now - fw.get("last_ota_at")),
            "seen_at": _iso_utc_from_ts(fw.get("seen_at")),
            "seen_age_seconds": None if fw.get("seen_at") is None else max(0.0, now - fw.get("seen_at")),
            "last_manifest_at": _iso_utc_from_ts(fw.get("last_manifest_at")),
            "last_manifest_age_seconds": None if fw.get("last_manifest_at") is None else max(0.0, now - fw.get("last_manifest_at")),
        },
        "ota": {
            "enabled": bool(ota.get("enabled", True)),
            "target_version": ota.get("target_version") or "",
            "filename": ota.get("filename") or "",
        },
    }

def _record_board_firmware_status(
    board_id: str,
    current: T.Optional[str] = None,
    build: T.Optional[str] = None,
    manifest_check: bool = False,
    ota_result: T.Optional[str] = None,
    ota_target: T.Optional[str] = None,
) -> None:
    if not board_id:
        return
    status = BOARD_FIRMWARE_STATUS.setdefault(board_id, {})
    now = time.time()
    if current:
        status["current"] = str(current).strip()
        status["seen_at"] = now
    if build:
        status["build"] = str(build).strip()[:80]
        status["seen_at"] = now
    if ota_result:
        status["last_ota_result"] = str(ota_result).strip()[:80]
        status["last_ota_at"] = now
    if ota_target:
        status["last_ota_target"] = str(ota_target).strip()[:40]
        status["last_ota_at"] = now
    if manifest_check:
        status["last_manifest_at"] = now

def _public_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    proto = proto.split(",")[0].strip()
    host = host.split(",")[0].strip()
    if host.endswith(".koyeb.app"):
        proto = "https"
    return f"{proto}://{host}".rstrip("/")

def _firmware_manifest_config() -> tuple[str, str]:
    version = FIRMWARE_VERSION
    path = FIRMWARE_PATH
    manifest_path = _resolve_local_path(FIRMWARE_MANIFEST_PATH)
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                version = str(data.get("version") or version).strip()
                filename = data.get("filename") or data.get("path")
                if filename:
                    filename = str(filename)
                    if os.path.isabs(filename):
                        path = filename
                    else:
                        path = os.path.join(os.path.dirname(manifest_path), filename)
        except Exception as e:
            vlog(f"WARN: failed to load firmware manifest {manifest_path}: {e}")
    return version, path

def _firmware_dir() -> str:
    manifest_path = _resolve_local_path(FIRMWARE_MANIFEST_PATH)
    directory = os.path.dirname(manifest_path)
    return directory or _resolve_local_path("firmware")

def _board_overlay_file_path() -> str:
    return _resolve_local_path(BOARD_OVERLAY_PATH)

def _board_overlay_url() -> str:
    if BOARD_OVERLAY_URL:
        return BOARD_OVERLAY_URL
    if BOARD_OVERLAY_PATH and os.path.exists(_board_overlay_file_path()):
        return "/board_overlay"
    return ""

def _safe_firmware_filename(filename: T.Optional[str]) -> str:
    filename = (filename or "").strip()
    if not filename or "/" in filename or "\\" in filename:
        return ""
    base = os.path.basename(filename)
    if base != filename or not base.lower().endswith(".bin"):
        return ""
    return base

def _firmware_metadata_for_path(version: str, path: str) -> dict:
    path = _resolve_local_path(path)
    if not FIRMWARE_OTA_ENABLED:
        return {"available": False, "reason": "ota_disabled", "version": version}
    if not version:
        return {"available": False, "reason": "missing_version", "version": version}
    if not os.path.exists(path):
        return {"available": False, "reason": "missing_binary", "version": version, "path": path}
    size = os.path.getsize(path)
    if size <= 0:
        return {"available": False, "reason": "empty_binary", "version": version, "path": path}

    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)

    return {
        "available": True,
        "version": version,
        "path": path,
        "filename": os.path.basename(path),
        "size": size,
        "md5": md5.hexdigest(),
        "sha256": sha256.hexdigest(),
        "mtime": os.path.getmtime(path),
    }

def _firmware_metadata_for_filename(filename: str, version: str = "") -> dict:
    filename = _safe_firmware_filename(filename)
    if not filename:
        return {"available": False, "reason": "invalid_filename", "version": version}
    path = os.path.join(_firmware_dir(), filename)
    return _firmware_metadata_for_path(version, path)

def _firmware_metadata() -> dict:
    version, path = _firmware_manifest_config()
    return _firmware_metadata_for_path(version, path)

def _version_parts(version: T.Optional[str]) -> T.Optional[tuple[int, ...]]:
    value = (version or "").strip()
    if not value:
        return None
    parts = re.findall(r"\d+", value)
    if not parts:
        return None
    return tuple(int(part) for part in parts)

def _firmware_update_available(current: str, target: T.Optional[str]) -> bool:
    target_version = (target or "").strip()
    if not target_version:
        return False
    current_version = (current or "").strip()
    current_parts = _version_parts(current_version)
    target_parts = _version_parts(target_version)
    if current_parts is not None and target_parts is not None:
        width = max(len(current_parts), len(target_parts))
        return target_parts + (0,) * (width - len(target_parts)) > current_parts + (0,) * (width - len(current_parts))
    return current_version != target_version

def _available_firmware_files() -> list[dict]:
    directory = _firmware_dir()
    if not os.path.isdir(directory):
        return []
    out: list[dict] = []
    for name in sorted(os.listdir(directory)):
        filename = _safe_firmware_filename(name)
        if not filename:
            continue
        path = os.path.join(directory, filename)
        meta = _firmware_metadata_for_path("file", path)
        out.append({
            "filename": filename,
            "available": bool(meta.get("available")),
            "size": meta.get("size"),
            "md5": meta.get("md5"),
            "sha256": meta.get("sha256"),
            "mtime": meta.get("mtime"),
        })
    return out

def _active_firmware_metadata_for_board(cfg: dict) -> dict:
    ota = cfg.get("ota", {}) or {}
    if not ota.get("enabled", True):
        return {"available": False, "reason": "board_ota_disabled", "version": ota.get("target_version", "")}

    filename = _safe_firmware_filename(ota.get("filename"))
    target_version = str(ota.get("target_version") or "").strip()
    if filename:
        meta = _firmware_metadata_for_filename(filename, target_version)
        if meta.get("available"):
            return meta
        return meta

    return _firmware_metadata()

def _parse_hhmm(s: str) -> T.Optional[dtime]:
    try:
        return datetime.strptime(s, "%H:%M").time()
    except Exception:
        return None

def _night_mode_effect(brightness: int, nm: dict) -> tuple[int, bool]:
    """
    Returns (effective_brightness, active_flag) based on night-mode settings.
    If parsing fails or disabled, returns (brightness, False).
    """
    if not nm or not nm.get("enabled"):
        return brightness, False

    start = _parse_hhmm(nm.get("start", ""))
    end = _parse_hhmm(nm.get("end", ""))
    if not start or not end:
        return brightness, False

    now_local = _now_local().time()
    if start <= end:
        active = start <= now_local < end
    else:
        # Window crosses midnight
        active = now_local >= start or now_local < end

    if not active:
        return brightness, False

    action = (nm.get("action") or "").lower()
    dim_level = int(nm.get("dim_level", 0))
    dim_level = max(0, min(255, dim_level))

    if action == "off":
        return 0, True
    return dim_level, True

def _default_board_settings(board_id: str) -> dict:
    return {
        "board_id": board_id,
        "brightness": 16,
        "mode": "live",  # "live", "animation", or "test"
        "night_mode": {
            "enabled": False,
            "start": "23:00",
            "end": "06:00",
            "action": "off",  # "off" or "dim"
            "dim_level": 20
        },
        "animation": {
            "name": "pulse",
            "speed": 120,
            "color": [255, 80, 0]
        },
        "test_mode": {
            "states": {},
            "updated_at": None,
        },
        "ota": {
            "enabled": True,
            "target_version": "",
            "filename": "",
        },
    }

def _normalize_board_config(board_id: str, cfg: T.Optional[dict]) -> dict:
    base = _default_board_settings(board_id)
    if not isinstance(cfg, dict):
        return base

    for key, value in cfg.items():
        if key in ("night_mode", "animation", "test_mode", "ota") and isinstance(value, dict):
            merged = dict(base.get(key, {}))
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value

    base["board_id"] = board_id
    if base.get("mode") not in ("live", "animation", "test"):
        base["mode"] = "live"
    animation = base.get("animation")
    if not isinstance(animation, dict):
        animation = {}
    name = str(animation.get("name", "pulse")).strip().lower()
    if name not in ANIMATION_NAMES:
        name = "pulse"
    animation["name"] = name
    base["animation"] = animation
    try:
        base["brightness"] = max(0, min(150, int(base.get("brightness", 16))))
    except Exception:
        base["brightness"] = 16
    return base

def _normalize_board_id(board_id: T.Optional[str]) -> str:
    return (board_id or "").strip().upper()

def _get_or_create_board(board_id: str) -> dict:
    board_id = _normalize_board_id(board_id)
    if not board_id:
        vlog("invalid board_id: empty")
        raise ValueError("empty board_id")
    if board_id not in BOARD_SETTINGS:
        vlog(f"invalid board_id: {board_id} not in BOARD_SETTINGS")
        raise ValueError("unknown board_id")
    BOARD_SETTINGS[board_id] = _normalize_board_config(board_id, BOARD_SETTINGS[board_id])
    return BOARD_SETTINGS[board_id]

_SKETCH_SEQUENCE_CACHE: T.Optional[dict[str, list[str]]] = None

MAJOR_BOARD_LABELS = {
    "Bankstown",
    "Blacktown",
    "Bondi Junction",
    "Campbelltown",
    "Central",
    "Chatswood",
    "Cronulla",
    "Epping",
    "Glenfield",
    "Hornsby",
    "Hurstville",
    "Leppington",
    "Lidcombe",
    "Liverpool",
    "Macarthur",
    "North Sydney",
    "Parramatta",
    "Penrith",
    "Redfern",
    "Richmond",
    "Strathfield",
    "Sutherland",
    "Sydenham",
    "Tallawong",
    "Town Hall",
    "Waterfall",
    "Wynyard",
}

BOARD_LINE_SPECS = [
    {
        "line": "T1",
        "suffix": "1",
        "color": "#F39C12",
        "segments": [
            {
                "array_names": ["T1_SEQ"],
                "start": "Berowra",
                "end": "Emu Plains",
                "label_side": -1,
                "path": [
                    [433, 10], [433, 95], [497, 160], [582, 250],
                    [634, 314], [650, 350], [646, 398], [618, 428],
                    [585, 425], [435, 425], [371, 435], [268, 337],
                    [215, 327], [73, 337],
                ],
                "anchors": {
                    "Berowra": [433, 10],
                    "Hornsby": [433, 95],
                    "Chatswood": [582, 250],
                    "North Sydney": [634, 314],
                    "Wynyard": [650, 350],
                    "Town Hall": [642, 394],
                    "Central": [618, 420],
                    "Redfern": [585, 425],
                    "Strathfield": [435, 425],
                    "Lidcombe": [371, 435],
                    "Parramatta": [268, 337],
                    "Blacktown": [215, 327],
                    "Emu Plains": [73, 337],
                },
            },
            {
                "array_names": ["T1_SEQ"],
                "start": "Marayong",
                "end": "Richmond",
                "label_side": 1,
                "path": [
                    [132, 289], [132, 280], [132, 87],
                ],
                "anchors": {
                    "Marayong": [132, 289],
                    "Richmond": [132, 87],
                },
            },
        ],
    },
    {
        "line": "T5",
        "suffix": "5",
        "color": "#D81B60",
        "segments": [
            {
                "array_names": ["T5_SEQ"],
                "label_side": -1,
                "path": [
                    [121, 87], [121, 282], [215, 327], [268, 337],
                    [243, 424], [238, 563], [238, 663], [34, 665],
                ],
                "anchors": {
                    "Richmond": [121, 87],
                    "Blacktown": [215, 327],
                    "Parramatta": [268, 337],
                    "Cabramatta": [238, 506],
                    "Liverpool": [238, 563],
                    "Glenfield": [238, 663],
                    "Leppington": [34, 665],
                },
            },
        ],
    },
    {
        "line": "T2",
        "suffix": "2",
        "color": "#00ACC1",
        "segments": [
            {
                "array_names": ["T2_SEG1", "T2_SEG2"],
                "label_side": 1,
                "path": [
                    [34, 675], [238, 675], [238, 663], [238, 563],
                    [238, 506], [243, 424], [268, 337], [371, 435],
                    [435, 435], [585, 425], [615, 412], [640, 392],
                    [650, 350], [685, 374], [650, 430],
                ],
                "anchors": {
                    "Leppington": [34, 675],
                    "Glenfield": [238, 663],
                    "Liverpool": [238, 563],
                    "Cabramatta": [238, 506],
                    "Parramatta": [268, 337],
                    "Lidcombe": [371, 435],
                    "Strathfield": [435, 435],
                    "Redfern": [585, 425],
                    "Central": [615, 412],
                    "Town Hall": [640, 392],
                    "Wynyard": [650, 350],
                    "Circular Quay": [675, 330],
                    "St James": [685, 374],
                    "Museum": [650, 430],
                },
            },
        ],
    },
    {
        "line": "T3",
        "suffix": "3",
        "color": "#FB8C00",
        "segments": [
            {
                "array_names": ["T3_SEQ"],
                "label_side": -1,
                "path": [
                    [238, 563], [238, 506], [325, 506], [371, 435],
                    [435, 445], [585, 425], [615, 412], [640, 392],
                    [650, 350], [675, 330], [685, 374], [650, 430],
                ],
                "anchors": {
                    "Liverpool": [238, 563],
                    "Cabramatta": [238, 506],
                    "Regents Park": [325, 506],
                    "Lidcombe": [371, 435],
                    "Strathfield": [435, 445],
                    "Redfern": [585, 425],
                    "Central": [615, 412],
                    "Town Hall": [640, 392],
                    "Wynyard": [650, 350],
                    "Circular Quay": [675, 330],
                    "St James": [685, 374],
                    "Museum": [650, 430],
                },
            },
        ],
    },
    {
        "line": "T4",
        "suffix": "4",
        "color": "#0D47A1",
        "segments": [
            {
                "array_names": ["T4_SEQ"],
                "start": "Waterfall",
                "end": "Loftus",
                "label_side": 1,
                "path": [[583, 842], [583, 782]],
                "anchors": {
                    "Waterfall": [583, 842],
                    "Loftus": [583, 782],
                },
            },
            {
                "array_names": ["T4_SEQ"],
                "start": "Cronulla",
                "end": "Bondi Junction",
                "label_side": -1,
                "path": [
                    [743, 773], [640, 773], [583, 766], [583, 625],
                    [583, 500], [585, 425], [615, 412], [650, 390],
                    [770, 354],
                ],
                "anchors": {
                    "Cronulla": [743, 773],
                    "Sutherland": [583, 766],
                    "Hurstville": [583, 625],
                    "Wolli Creek": [583, 500],
                    "Sydenham": [583, 475],
                    "Redfern": [585, 425],
                    "Central": [615, 412],
                    "Town Hall": [642, 392],
                    "Martin Place": [670, 385],
                    "Bondi Junction": [770, 354],
                },
            },
        ],
    },
    {
        "line": "T6",
        "suffix": "6",
        "color": "#A0522D",
        "segments": [
            {
                "array_names": ["T6_SEQ"],
                "label_side": 1,
                "path": [[382, 556], [325, 506], [371, 435]],
                "anchors": {
                    "Bankstown": [382, 556],
                    "Regents Park": [325, 506],
                    "Lidcombe": [371, 435],
                },
            },
        ],
    },
    {
        "line": "T7",
        "suffix": "7",
        "color": "#9E9E9E",
        "segments": [
            {
                "array_names": ["T7_SEQ"],
                "label_side": -1,
                "path": [[371, 435], [371, 354]],
                "anchors": {
                    "Lidcombe": [371, 435],
                    "Olympic Park": [371, 354],
                },
            },
        ],
    },
    {
        "line": "T8",
        "suffix": "8",
        "color": "#006400",
        "segments": [
            {
                "array_names": ["T8_SEQ"],
                "label_side": 1,
                "path": [
                    [258, 805], [258, 742], [238, 663], [455, 624],
                    [582, 500], [582, 475], [600, 430], [610, 500],
                    [653, 515], [652, 456], [630, 410], [650, 350],
                    [675, 330], [685, 374], [650, 430],
                ],
                "anchors": {
                    "Macarthur": [258, 805],
                    "Campbelltown": [258, 742],
                    "Glenfield": [238, 663],
                    "Revesby": [455, 624],
                    "Sydenham": [582, 475],
                    "Redfern": [600, 430],
                    "Wolli Creek": [610, 500],
                    "Domestic Airport": [653, 515],
                    "Green Square": [652, 456],
                    "Central": [630, 410],
                    "Town Hall": [640, 392],
                    "Wynyard": [650, 350],
                    "Circular Quay": [675, 330],
                    "St James": [685, 374],
                    "Museum": [650, 430],
                },
            },
        ],
    },
    {
        "line": "T9",
        "suffix": "9",
        "color": "#E53935",
        "segments": [
            {
                "array_names": ["T9_SEQ"],
                "label_side": 1,
                "path": [
                    [433, 95], [433, 205], [433, 385], [435, 425],
                    [585, 425], [615, 412], [640, 392], [650, 350],
                    [634, 314], [582, 250], [520, 178],
                ],
                "anchors": {
                    "Hornsby": [433, 95],
                    "Epping": [433, 205],
                    "Strathfield": [433, 385],
                    "Redfern": [585, 425],
                    "Central": [615, 412],
                    "Town Hall": [640, 392],
                    "Wynyard": [650, 350],
                    "North Sydney": [634, 314],
                    "Chatswood": [582, 250],
                    "Gordon": [520, 178],
                },
            },
        ],
    },
    {
        "line": "M1",
        "suffix": "M",
        "color": "#26C6DA",
        "segments": [
            {
                "array_names": ["M1_SEQ"],
                "label_side": -1,
                "path": [
                    [178, 210], [433, 210], [520, 238], [582, 250],
                    [615, 314], [625, 365], [675, 390], [665, 410],
                    [615, 412], [640, 456], [583, 475], [382, 556],
                ],
                "anchors": {
                    "Tallawong": [178, 210],
                    "Epping": [433, 210],
                    "Chatswood": [582, 250],
                    "Barangaroo": [625, 365],
                    "Martin Place": [675, 390],
                    "Gadigal": [665, 410],
                    "Central": [615, 412],
                    "Sydenham": [583, 475],
                    "Bankstown": [382, 556],
                },
            },
        ],
    },
]

def _load_firmware_sequences() -> dict[str, list[str]]:
    global _SKETCH_SEQUENCE_CACHE
    if _SKETCH_SEQUENCE_CACHE is not None:
        return _SKETCH_SEQUENCE_CACHE

    path = _resolve_local_path(FIRMWARE_SKETCH_PATH)
    sequences: dict[str, list[str]] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                body = f.read()
            pattern = re.compile(
                r"const\s+char\s*\*\s*const\s+(\w+)\s*\[\]\s*=\s*\{(.*?)\};",
                re.S,
            )
            for name, block in pattern.findall(body):
                sequences[name] = [
                    bytes(value, "utf-8").decode("unicode_escape")
                    for value in re.findall(r'"((?:\\.|[^"\\])*)"', block)
                ]
        except Exception as e:
            vlog(f"WARN: failed to load firmware station sequences from {path}: {e}")
    else:
        vlog(f"WARN: firmware sketch not found for board layout: {path}")

    _SKETCH_SEQUENCE_CACHE = sequences
    return sequences

def _point_at_distance(points: list[list[float]], target: float) -> list[float]:
    if not points:
        return [0.0, 0.0]
    if len(points) == 1:
        return [float(points[0][0]), float(points[0][1])]

    walked = 0.0
    for a, b in zip(points, points[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        seg = math.hypot(bx - ax, by - ay)
        if seg <= 0:
            continue
        if walked + seg >= target:
            t = (target - walked) / seg
            return [ax + (bx - ax) * t, ay + (by - ay) * t]
        walked += seg
    last = points[-1]
    return [float(last[0]), float(last[1])]

def _point_angle_at_distance(points: list[list[float]], target: float) -> tuple[float, float, float]:
    if not points:
        return (0.0, 0.0, 0.0)
    if len(points) == 1:
        return (float(points[0][0]), float(points[0][1]), 0.0)

    walked = 0.0
    last_angle = 0.0
    for a, b in zip(points, points[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        seg = math.hypot(bx - ax, by - ay)
        if seg <= 0:
            continue
        last_angle = math.degrees(math.atan2(by - ay, bx - ax))
        if walked + seg >= target:
            t = (target - walked) / seg
            return (ax + (bx - ax) * t, ay + (by - ay) * t, last_angle)
        walked += seg
    last = points[-1]
    return (float(last[0]), float(last[1]), last_angle)

def _layout_points_for_names(names: list[str], path: list[list[float]]) -> list[list[float]]:
    if not names:
        return []
    if len(names) == 1:
        return [_point_at_distance(path, 0.0)]
    distances = [
        math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        for a, b in zip(path, path[1:])
    ]
    total = sum(distances)
    if total <= 0:
        return [_point_at_distance(path, 0.0) for _ in names]
    return [_point_at_distance(path, total * idx / (len(names) - 1)) for idx in range(len(names))]

def _path_length(points: list[list[float]]) -> float:
    return sum(
        math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        for a, b in zip(points, points[1:])
    )

def _distance_on_path_for_point(points: list[list[float]], point: list[float]) -> float:
    if not points:
        return 0.0
    if len(points) == 1:
        return 0.0

    px, py = float(point[0]), float(point[1])
    walked = 0.0
    best_dist = float("inf")
    best_walked = 0.0
    for a, b in zip(points, points[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        vx, vy = bx - ax, by - ay
        seg_len_sq = vx * vx + vy * vy
        if seg_len_sq <= 0:
            continue
        t = ((px - ax) * vx + (py - ay) * vy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        cx, cy = ax + vx * t, ay + vy * t
        dist = math.hypot(px - cx, py - cy)
        if dist < best_dist:
            best_dist = dist
            best_walked = walked + math.sqrt(seg_len_sq) * t
        walked += math.sqrt(seg_len_sq)
    return best_walked

def _layout_positions_for_names(
    names: list[str],
    path: list[list[float]],
    anchors: T.Optional[dict[str, list[float]]] = None,
) -> list[dict[str, float]]:
    if not names:
        return []
    if len(names) == 1:
        x, y, angle = _point_angle_at_distance(path, 0.0)
        return [{"x": x, "y": y, "angle": angle}]
    total = _path_length(path)
    if total <= 0:
        return [
            {"x": _point_at_distance(path, 0.0)[0], "y": _point_at_distance(path, 0.0)[1], "angle": 0.0}
            for _ in names
        ]

    anchor_points: dict[int, float] = {}
    if anchors:
        for name, point in anchors.items():
            if name in names:
                anchor_points[names.index(name)] = _distance_on_path_for_point(path, point)
    anchor_points.setdefault(0, 0.0)
    anchor_points.setdefault(len(names) - 1, total)

    if len(anchor_points) > 2:
        out = [None] * len(names)
        ordered = sorted(anchor_points.items())
        for (left_idx, left_dist), (right_idx, right_dist) in zip(ordered, ordered[1:]):
            span = right_idx - left_idx
            if span <= 0:
                continue
            for idx in range(left_idx, right_idx + 1):
                if out[idx] is not None:
                    continue
                t = (idx - left_idx) / span
                dist = left_dist + (right_dist - left_dist) * t
                x, y, angle = _point_angle_at_distance(path, dist)
                out[idx] = {"x": x, "y": y, "angle": angle}
        return [
            item if item is not None else {"x": _point_at_distance(path, 0.0)[0], "y": _point_at_distance(path, 0.0)[1], "angle": 0.0}
            for item in out
        ]

    out = []
    for idx in range(len(names)):
        x, y, angle = _point_angle_at_distance(path, total * idx / (len(names) - 1))
        out.append({"x": x, "y": y, "angle": angle})
    return out

def _readable_label_angle(angle: float) -> float:
    angle = ((angle + 180.0) % 360.0) - 180.0
    if abs(angle) < 18.0:
        return -42.0
    if abs(abs(angle) - 90.0) < 18.0:
        return 0.0
    if angle > 90.0:
        angle -= 180.0
    elif angle < -90.0:
        angle += 180.0
    return angle

def _segment_names(sequences: dict[str, list[str]], segment: dict) -> list[str]:
    names: list[str] = []
    for array_name in segment.get("array_names", []):
        names.extend(sequences.get(array_name, []))
    if not names:
        return []

    start_name = segment.get("start")
    end_name = segment.get("end")
    start = 0
    end = len(names) - 1
    if start_name in names:
        start = names.index(start_name)
    if end_name in names:
        end = names.index(end_name)
    if start > end:
        return []
    return names[start : end + 1]

def _board_layout_model() -> dict:
    sequences = _load_firmware_sequences()
    lines = []
    for spec in BOARD_LINE_SPECS:
        stations = []
        paths = []
        for segment in spec.get("segments", []):
            names = _segment_names(sequences, segment)
            path = segment.get("path", [])
            if not names or not path:
                continue
            paths.append(path)
            positions = _layout_positions_for_names(names, path, segment.get("anchors"))
            side = -1.0 if segment.get("label_side", -1) < 0 else 1.0
            for name, pos in zip(names, positions):
                angle = float(pos["angle"])
                rad = math.radians(angle)
                label_angle = _readable_label_angle(angle)
                dx = -math.sin(rad) * side * 11.0
                dy = math.cos(rad) * side * 11.0
                stations.append({
                    "name": name,
                    "key": f"{name}{spec['suffix']}",
                    "x": round(pos["x"], 2),
                    "y": round(pos["y"], 2),
                    "labelAngle": round(label_angle, 1),
                    "labelDx": round(dx, 2),
                    "labelDy": round(dy, 2),
                    "major": name in MAJOR_BOARD_LABELS,
                })
        lines.append({
            "line": spec["line"],
            "suffix": spec["suffix"],
            "color": spec["color"],
            "paths": paths,
            "stations": stations,
        })
    return {
        "viewBox": BOARD_LAYOUT_VIEWBOX,
        "title": "Sydney Rail Network",
        "overlayUrl": _board_overlay_url(),
        "lines": lines,
    }

def _board_layout_keys() -> set[str]:
    keys: set[str] = set()
    for line in _board_layout_model()["lines"]:
        for station in line["stations"]:
            keys.add(station["key"])
    return keys

EXCLUDE_SUB = ("wharf", "light rail", "b-line", "interchange", "motorway", " m2")
SHORT_RE = re.compile(r"\b([TM]\d{1,2})\b", re.I)

# ---------------- Small helpers ----------------
def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def _valid_ll(x: float, y: float) -> bool:
    try:
        x = float(x); y = float(y)
    except Exception:
        return False
    return -90 <= x <= 90 and -180 <= y <= 180

def hav_m(a: float, b: float, c: float, d: float) -> float:
    try:
        a = float(a); b = float(b); c = float(c); d = float(d)
    except Exception:
        return float("inf")
    if not (_valid_ll(a, b) and _valid_ll(c, d)):
        return float("inf")
    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0
    da = radians(c - a)
    db = radians(d - b)
    t = sin(da / 2) ** 2 + cos(radians(a)) * cos(radians(c)) * sin(db / 2) ** 2
    return 2 * R * asin(sqrt(max(0.0, min(1.0, t))))

def within_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    S, W, N, E = bbox
    return (S <= lat <= N) and (W <= lon <= E)

def is_num_parent(s: str) -> bool:
    return s.isdigit()

def status_from_dist(m: T.Optional[float]) -> int:
    if m is None:
        return 0
    if m <= D_SOLID:
        return 3  # solid/stopped
    if m <= D_NEAR:
        return 2  # approaching/medium
    if m <= D_FAR:
        return 1  # in area/dim
    return 0

def clean_name(n: str) -> str:
    n = (n or "").strip()
    for suf in (" Station", " Railway Station"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n

def _is_stationish(n: str) -> bool:
    n = (n or "").strip().lower()
    if any(s in n for s in EXCLUDE_SUB):
        return False
    return n.endswith(" station") or n.endswith(" railway station")

def load_whitelist(path: str) -> tuple[set[str], set[str]]:
    sid_w: set[str] = set()
    name_w: set[str] = set()
    if not path or not os.path.exists(path):
        return sid_w, name_w
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            token = line.split(",", 1)[0].strip()
            if token.isdigit():
                sid_w.add(token)
            else:
                name_w.add(clean_name(token).lower())
    return sid_w, name_w

# ---------------- Route short names (with overrides) ----------------
def derive_route_short(route_short_name: str | None, route_long_name: str | None) -> str:
    rs = (route_short_name or "").strip()
    if rs:
        return rs.upper()
    rl = (route_long_name or "").strip()
    if rl:
        m = SHORT_RE.search(rl)
        if m:
            return m.group(1).upper()
    return ""

def load_route_overrides(path: str, env_json: str) -> dict[str, str]:
    out: dict[str, str] = {}
    # CSV file
    try:
        if path and os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    rid = (row.get("route_id") or "").strip()
                    rs = (row.get("route_short_name") or "").strip().upper()
                    if rid and rs:
                        out[rid] = rs
            vlog(f"route_overrides: loaded {len(out)} from file")
    except Exception as e:
        vlog(f"WARN: override file: {e}")
    # Env JSON
    if env_json:
        try:
            obj = json.loads(env_json)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    k = (k or "").strip()
                    v = (v or "").strip().upper()
                    if k and v:
                        out[k] = v
            vlog("route_overrides: merged env JSON")
        except Exception as e:
            vlog(f"WARN: override env: {e}")
    return out

def load_route_shortnames(routes_path: str = "routes.txt") -> dict[str, str]:
    ov = load_route_overrides(ROUTE_SHORT_OVERRIDES_PATH, ROUTE_SHORT_OVERRIDES_JSON)
    out: dict[str, str] = {}
    with open(routes_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            if not rid:
                continue
            rs = ov.get(rid) or derive_route_short(r.get("route_short_name"), r.get("route_long_name"))
            if rs:
                out[rid] = rs
    vlog(f"route_short: {len(out)} (ov={len(ov)})")
    return out

def load_trip_route_shortnames(
    trips_path: str = "trips.txt",
    routes_path: str = "routes.txt",
) -> dict[str, str]:
    route_id_to_short = load_route_shortnames(routes_path)
    out: dict[str, str] = {}
    with open(trips_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tid = (r.get("trip_id") or "").strip()
            rid = (r.get("route_id") or "").strip()
            rs = (route_id_to_short.get(rid, "") or "").upper()
            if tid and rs:
                out[tid] = rs
    vlog(f"trip_route_short: {len(out)}")
    return out

# ---------------- Static GTFS filters: allowed parents & pairs ----------------
def require_allowed_parents(
    stops="stops.txt",
    routes="routes.txt",
    trips="trips.txt",
    stop_times="stop_times.txt",
    agency="agency.txt",
    types=(1, 2),
    agencies_allowed: set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: set[str] = ROUTE_SHORT_ALLOW,
) -> set[str]:
    missing = [p for p in (stops, routes, trips, stop_times, agency) if not os.path.exists(p)]
    if missing:
        raise RuntimeError("Missing GTFS: " + ", ".join(missing))

    ag_name: dict[str, str] = {}
    with open(agency, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ag_name[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    route_meta: dict[str, tuple[int, str, str]] = {}
    with open(routes, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                rtype = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                rtype = 3
            rshort = (r.get("route_short_name") or "").strip()
            agid = (r.get("agency_id") or "").strip()
            route_meta[rid] = (rtype, rshort, ag_name.get(agid, ""))

    trip_route: dict[str, str] = {}
    with open(trips, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    stops_map: dict[str, dict] = {}
    parents: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parents.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row:
            return None
        p = (row.get("parent_station") or "").strip()
        if p:
            return p
        return sid if sid in parents else None

    allowed: set[str] = set()
    seen_types, seen_ag = set(), set()
    with open(stop_times, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = trip_route.get((r.get("trip_id") or "").strip(), "")
            rtype, rshort, ag = route_meta.get(rid, (None, "", ""))
            if rtype is None:
                continue
            seen_types.add(rtype); seen_ag.add(ag)
            if types and rtype not in types:
                continue
            if agencies_allowed and ag not in agencies_allowed:
                continue
            if route_short_allow and rshort and rshort.upper() not in route_short_allow:
                continue
            p = parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p):
                allowed.add(p)

    vlog("routes types seen", sorted(seen_types), "agencies", sorted(seen_ag))
    if allowed:
        vlog(f"parents by trips: {len(allowed)}")
        return allowed

    # fallback: names
    fb: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("location_type") or "0").strip() != "1":
                continue
            pid = (r.get("stop_id") or "").strip()
            if not is_num_parent(pid):
                continue
            if _is_stationish((r.get("stop_name") or "")):
                fb.add(pid)
    if not fb:
        raise RuntimeError("No parent stations via fallback.")
    vlog(f"parents by names: {len(fb)}")
    return fb

def _known_tm(route_id_to_short: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for rs in route_id_to_short.values():
        u = (rs or "").upper().strip()
        if re.fullmatch(r"[TM]\d{1,2}", u):
            out.add(u)
    return out

def require_allowed_pairs(
    stops="stops.txt",
    routes="routes.txt",
    trips="trips.txt",
    stop_times="stop_times.txt",
    agency="agency.txt",
    types=(1, 2),
    agencies_allowed: set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: set[str] = ROUTE_SHORT_ALLOW,
    allowed_parents_for_fallback: T.Optional[set[str]] = None,
) -> set[tuple[str, str]]:
    ag_name: dict[str, str] = {}
    with open(agency, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ag_name[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    rt_meta: dict[str, tuple[int, str, str]] = {}
    with open(routes, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                rtype = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                rtype = 3
            agid = (r.get("agency_id") or "").strip()
            ag = ag_name.get(agid, "")
            rlong = (r.get("route_long_name") or "").strip()
            rt_meta[rid] = (rtype, ag, rlong)

    trip_route: dict[str, str] = {}
    with open(trips, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    stops_map: dict[str, dict] = {}
    parents: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parents.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row:
            return None
        p = (row.get("parent_station") or "").strip()
        if p:
            return p
        return sid if sid in parents else None

    route_id_to_short = load_route_shortnames(routes)

    pairs: set[tuple[str, str]] = set()
    missing_short_seen: dict[str, int] = {}

    with open(stop_times, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = trip_route.get((r.get("trip_id") or "").strip(), "")
            rtype, ag, _ = rt_meta.get(rid, (None, "", ""))
            if rtype is None:
                continue
            if types and rtype not in types:
                continue
            if agencies_allowed and ag not in agencies_allowed:
                continue
            rshort = (route_id_to_short.get(rid, "") or "").upper()
            if not rshort:
                if len(missing_short_seen) < 20:
                    missing_short_seen[rid] = missing_short_seen.get(rid, 0) + 1
                continue
            if route_short_allow and rshort not in route_short_allow:
                continue
            p = parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p):
                pairs.add((p, rshort))

    if pairs:
        vlog(f"pairs strict: {len(pairs)}")
        return pairs

    # permissive fallback
    if os.getenv("MB_PERMISSIVE_PAIRS", "1") != "0":
        known = _known_tm(route_id_to_short)
        if route_short_allow:
            known &= {x.upper() for x in route_short_allow}
        if not known:
            vlog("WARN: no T*/M* for fallback")
            return set()
        if not allowed_parents_for_fallback:
            allowed_parents_for_fallback = {
                sid for sid, row in stops_map.items()
                if (row.get("location_type") or "0").strip() == "1" and is_num_parent(sid)
            }
        fan = {(pid, rs) for pid in allowed_parents_for_fallback for rs in known}
        vlog(f"pairs fallback: {len(fan)}")
        return fan

    if missing_short_seen:
        vlog("WARN: zero pairs; sample route_ids with no short code:")
        for rid in list(missing_short_seen.keys())[:10]:
            meta = rt_meta.get(rid, (None, "", ""))
            vlog(f"  - rid={rid}  agency={meta[1]!r}  rtype={meta[0]} (add override)")
    raise RuntimeError("No station×route pairs; supply overrides or enable fallback.")

# ---------------- Build station index (per station × route) ----------------
def route_suffix(rs: str) -> str:
    rs = (rs or "").upper()
    if rs.startswith("T") and len(rs) >= 2:
        return rs[1:]
    if rs.startswith("M"):
        return "M"
    return rs or "?"

def load_station_index(
    stops="stops.txt",
    bbox: T.Optional[tuple[float, float, float, float]] = SYD_BBOX,
    sid_whitelist: set[str] = set(),
    name_whitelist: set[str] = set(),
    allowed_parents: T.Optional[set[str]] = None,
    allowed_pairs: T.Optional[set[tuple[str, str]]] = None,
) -> dict[str, dict]:
    if allowed_parents is None or allowed_pairs is None:
        raise RuntimeError("allowed_parents and allowed_pairs are required")

    parents: dict[str, dict] = {}
    kids: dict[str, list[tuple[float, float]]] = {}

    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            lt = (r.get("location_type") or "0").strip()
            sid = (r.get("stop_id") or "").strip()
            parent = (r.get("parent_station") or "").strip()
            name_raw = (r.get("stop_name") or "").strip()
            lat_s, lon_s = r.get("stop_lat"), r.get("stop_lon")
            lat = float(lat_s) if lat_s else None
            lon = float(lon_s) if lon_s else None

            if lt == "1":
                if is_num_parent(sid) and sid in allowed_parents and not any(x in name_raw.lower() for x in EXCLUDE_SUB):
                    parents[sid] = {"name": name_raw, "lat": lat, "lon": lon}
            else:
                if parent and lat is not None and lon is not None:
                    if (bbox is None) or within_bbox(lat, lon, bbox):
                        kids.setdefault(parent, []).append((lat, lon))

    by_parent: dict[str, set[str]] = {}
    for pid, rs in allowed_pairs:
        by_parent.setdefault(pid, set()).add(rs)

    idx: dict[str, dict] = {}
    for pid, meta in parents.items():
        base = clean_name(meta["name"])
        lat, lon = meta["lat"], meta["lon"]
        if (lat is None or lon is None) and pid in kids and kids[pid]:
            lat = sum(t[0] for t in kids[pid]) / len(kids[pid])
            lon = sum(t[1] for t in kids[pid]) / len(kids[pid])
        if lat is None or lon is None:
            continue
        if (sid_whitelist or name_whitelist):
            if not (pid in sid_whitelist or base.lower() in name_whitelist):
                continue
        if SYD_BBOX is not None and not within_bbox(lat, lon, SYD_BBOX) and pid not in kids:
            continue

        for rs in sorted(by_parent.get(pid, set())):
            suf = route_suffix(rs)
            key = f"{base}{suf}"
            if key in idx:
                key = f"{base}{suf} ({pid})"
            idx[key] = {"stop_id": pid, "name": key, "lat": lat, "lon": lon, "route_short": rs, "base_name": base}

    return idx

# ---------------- Child→Parent map for VP.stop_id resolution ----------------
def build_child_to_parent(stops="stops.txt") -> dict[str, str]:
    out: dict[str, str] = {}
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            pid = (r.get("parent_station") or "").strip()
            lt = (r.get("location_type") or "0").strip()
            if sid and pid and is_num_parent(pid):
                out[sid] = pid
            if sid and lt == "1" and is_num_parent(sid):
                out.setdefault(sid, sid)
    vlog(f"child→parent: {len(out)}")
    return out

def extract_station_name_from_stop_id(stop_id: str) -> T.Optional[str]:
    """
    Extract station name from TfNSW location-based stop_ids.
    Examples:
      "Blacktown.Seven Hills 2 Loc" -> "Seven Hills"
      "NorthShore.Lindfield 3 Loc" -> "Lindfield"
      "Sydney.Circular Quay 1 Loc" -> "Circular Quay"
      "Glenfield.GFD Dn Ctn Exit Loc" -> None (code pattern)
      "Lidcombe.AY997/548 Loc" -> None (code pattern)
    """
    if not stop_id or "." not in stop_id:
        return None
    
    # Split on dot: "Area.StationInfo"
    parts = stop_id.split(".", 1)
    if len(parts) < 2:
        return None
    
    station_part = parts[1].strip()
    
    # Remove common suffixes and numeric indicators
    for suffix in [" Loc", " Entry", " Exit", " Dep", " Arr", " Dn", " Up", " Ctn"]:
        station_part = station_part.replace(suffix, "")
    
    # Remove trailing numbers (platform indicators): " 1", " 2", etc.
    station_part = re.sub(r'\s+\d+$', '', station_part)
    
    station_part = station_part.strip()
    
    # Skip if empty or too short
    if len(station_part) < 3:
        return None
    
    # Skip if it looks like a code (starts with 2-3 capital letters followed by numbers or special chars)
    if re.match(r'^[A-Z]{2,3}[\d/]', station_part):
        return None
    
    # Skip if it contains slashes with numbers (technical location codes)
    if '/' in station_part and any(c.isdigit() for c in station_part):
        return None
    
    # Clean up extra spaces
    station_part = " ".join(station_part.split())
    
    return station_part if station_part else None

def station_parent_from_realtime_stop_id(stop_id: str) -> T.Optional[str]:
    parent = CHILD_TO_PARENT.get(stop_id) or (stop_id if is_num_parent(stop_id) else None)
    if parent:
        return parent
    extracted_name = extract_station_name_from_stop_id(stop_id)
    if extracted_name:
        return STATION_NAME_TO_PARENT.get(extracted_name)
    return None

def route_nearest_station_names(
    route_short: str,
    lat: T.Optional[float],
    lon: T.Optional[float],
) -> set[str]:
    if lat is None or lon is None:
        return set()

    route_short = (route_short or "").upper()
    best_d: T.Optional[float] = None
    distances: list[tuple[str, float]] = []

    for name, meta in STATION_INDEX.items():
        if (meta.get("route_short") or "").upper() != route_short:
            continue
        d = hav_m(meta["lat"], meta["lon"], lat, lon)
        if best_d is None or d < best_d:
            best_d = d
        distances.append((name, d))

    if best_d is None:
        return set()
    return {name for name, d in distances if d <= best_d + MB_ROUTE_NEAREST_MARGIN_M}

# ---------------- GTFS-RT: fetch & parse ----------------
_FEED_CACHE: T.Optional[rt.FeedMessage] = None
_FEED_TS = 0.0

def fetch_vp_cached() -> tuple[rt.FeedMessage, float]:
    global _FEED_CACHE, _FEED_TS
    now = time.time()
    if _FEED_CACHE is not None and (now - _FEED_TS) < FEED_CACHE_SEC:
        return _FEED_CACHE, (now - _FEED_TS)

    if not API_TOKEN:
        raise RuntimeError("TFNSW_API_TOKEN is not configured; live feeds unavailable")

    hdr = {
        "Authorization": f"apikey {API_TOKEN}",
        "Accept": "application/x-protobuf",
        "Cache-Control": "no-cache",
        "User-Agent": "metroboard/1.0",
    }
    s = _session()
    time.sleep(random.uniform(0, 0.25))  # stagger

    merged = rt.FeedMessage()
    merged.header.gtfs_realtime_version = "2.0"
    total = 0

    for url in URLS_VP:
        try:
            r = s.get(url, headers=hdr, timeout=20)
            r.raise_for_status()
            feed = rt.FeedMessage()
            feed.ParseFromString(r.content)
            veh = sum(1 for e in feed.entity if e.HasField("vehicle"))
            merged.entity.extend(feed.entity)
            total += veh
            vlog(f"fetched {url}: vehicles={veh}")
        except Exception as e:
            vlog("WARN fetch", url, e)

    _FEED_CACHE = merged
    _FEED_TS = now
    vlog("feed merged vehicles", total)
    return merged, 0.0

def load_station_route_pairs(path: str = "station_line_pairs.csv") -> dict[str, set[str]]:
    """Load parent_stop_id -> set of route_short from station_line_pairs.csv"""
    pairs: dict[str, set[str]] = {}
    if not os.path.exists(path):
        vlog(f"WARN: {path} not found, route inference will be limited")
        return pairs
    
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = (row.get("parent_stop_id") or "").strip()
                rs = (row.get("route_short") or "").strip().upper()
                if pid and rs:
                    pairs.setdefault(pid, set()).add(rs)
        vlog(f"Loaded {len(pairs)} station->route mappings from {path}")
    except Exception as e:
        vlog(f"WARN: Failed to load {path}: {e}")
    
    return pairs

def load_stop_route_pairs(
    stops="stops.txt",
    routes="routes.txt",
    trips="trips.txt",
    stop_times="stop_times.txt",
    types=(1, 2),
    route_short_allow: set[str] = ROUTE_SHORT_ALLOW,
) -> dict[str, set[str]]:
    """Load child stop_id -> set of route_short from static GTFS stop_times."""
    route_id_to_short = load_route_shortnames(routes)
    route_types: dict[str, int] = {}
    with open(routes, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                route_types[rid] = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                route_types[rid] = 3

    trip_route: dict[str, str] = {}
    with open(trips, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    stops_seen: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            if sid:
                stops_seen.add(sid)

    pairs: dict[str, set[str]] = {}
    with open(stop_times, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            if not sid or sid not in stops_seen:
                continue
            rid = trip_route.get((r.get("trip_id") or "").strip(), "")
            if types and route_types.get(rid) not in types:
                continue
            rs = (route_id_to_short.get(rid, "") or "").upper()
            if not rs:
                continue
            if route_short_allow and rs not in route_short_allow:
                continue
            pairs.setdefault(sid, set()).add(rs)

    vlog(f"Loaded {len(pairs)} stop->route mappings from static GTFS")
    return pairs

FORCE_T1_PARENT_STOPS = {
    "207310",  # Pymble
    "207410",  # Turramurra
    "207420",  # Warrawee
    "207610",  # Wahroonga
    "207710",  # Waitara
}

def collect_trains(feed: rt.FeedMessage, route_id_to_short: dict[str, str]) -> tuple[
    list[tuple[T.Optional[float], T.Optional[float], T.Optional[str], str, int]],
    dict[str, int],
    dict[str, str],
]:
    """Return (trains, counts, route_debug)."""
    trains = []
    by_route = {}
    missing_routes = {}
    route_debug = {}

    # Route IDs in the realtime feed use short operational prefixes.
    # Only unambiguous prefixes are mapped directly; ambiguous prefixes must be
    # resolved against the reported station or left unknown.
    ROUTE_PREFIX_MAP = {
        # Core Sydney Trains lines
        "BMT": ["BMT"],          # Blue Mountains Line
        "NTH": ["T9"],           # Northern Line
        "WST": ["T1"],           # Western Line  
        "APS": ["T8"],           # Airport & South Line - FIXED! Was wrongly T1/T9
        "CTY": [],               # NSW TrainLink exact IDs handled above
        "IWL": ["T2"],           # Inner West & Leppington
        "LPP": ["T2"],           # Leppington specific
        "T2": ["T2"],            # Direct T2 code
        "T3": ["T3"],            # Bankstown Line
        "ESI": ["T4"],           # Eastern Suburbs & Illawarra
        "CMB": ["T5"],           # Cumberland Line
        "T6": ["T6"],            # Lidcombe & Bankstown (new)
        "OLY": ["T7"],           # Olympic Park Line
        "T7": ["T7"],            # Olympic Park
        "APT": ["T8"],           # Airport Line
        "SHL": ["SHL"],          # Southern Highlands Line
        "AIRS": ["T8"],          # Airport & South
        "AIR": ["T8"],           # Airport
        "ESS": ["T8"],           # Eastern Suburbs South
        "T8": ["T8"],            # Direct T8
        "NSN": ["T1"],           # North Shore Line
        
        # Regional lines
        "SCO": ["SCO"],          # South Coast Line
        "CCN": ["CCN"],          # Central Coast & Newcastle
        "SOUTHCOAST": ["SCO"],
        "CENTRALCOAST": ["CCN"],
        "WOLLONGONG": ["SCO"],
        
        # Metro
        "SM": ["M1"], 
        "SMNW": ["M1"],          # Sydney Metro Northwest
        "M1": ["M1"],            # Metro M1
        "M": ["M1"],      # Catch generic "M" prefixes
        "METRO": ["M1"],
        
    }

    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        tripid = v.trip.trip_id if v.HasField("trip") else ""
        sid = getattr(v, "stop_id", "")

        # Resolve stop_id to parent station ID early for use in disambiguation
        parent = None
        if sid and STATION_ROUTE_PAIRS:
            parent = station_parent_from_realtime_stop_id(sid)

        # METHOD 1: Use exact static GTFS route/trip mapping when the feed provides it.
        rs = (route_id_to_short.get(rid, "") or "").upper()
        if not rs and tripid:
            rs = (TRIP_ID_TO_SHORT.get(tripid, "") or "").upper()

        # METHOD 2: Extract T/M code from route_id or trip_id
        if not rs:
            for src in (rid, tripid):
                if not src:
                    continue
                m = re.search(r"\b([TM]\d{1,2})\b", src, re.I)
                if m:
                    rs = m.group(1).upper()
                    break

        # METHOD 2.5: Resolve full realtime operational route IDs that static
        # GTFS does not list, but public departure data exposes directly.
        if not rs and rid:
            rs = ROUTE_ID_EXACT_MAP.get(rid.upper(), "")

        # METHOD 3: Use prefix mapping with station-based disambiguation
        if not rs and rid:
            prefix = route_id_prefix(rid)
            candidates = []
            
            # Try exact match first
            if prefix in ROUTE_PREFIX_MAP:
                candidates = ROUTE_PREFIX_MAP[prefix]
            
            # If we have candidates, use station info to pick the right one.
            if candidates and len(candidates) > 1 and parent and parent in STATION_ROUTE_PAIRS:
                # Filter candidates to only those that serve this station
                possible = set(candidates) & STATION_ROUTE_PAIRS[parent]
                if len(possible) == 1:
                    rs = list(possible)[0]
                    if MB_RESOLUTION_VERBOSE:
                        vlog(f"Disambiguated {rid} at {parent}: {candidates} -> {rs}")
                elif possible:
                    # Multiple matches - only accept strong hints. Do not hash/guess.
                    if any(x in rid.upper() for x in ["AIR", "SHL", "APT", "ESS", "AIRS"]) and "T8" in possible:
                        rs = "T8"
                    elif parent in FORCE_T1_PARENT_STOPS and "T1" in possible:
                        rs = "T1"
                    elif any(x in rid.upper() for x in ["NSN", "NTH"]) and "T9" in possible and "T1" in possible:
                        rs = "T9" if MB_GUESS_AMBIGUOUS else None
                    elif MB_GUESS_AMBIGUOUS:
                        rs = preferred_route_candidate(prefix, [c for c in candidates if c in possible])
                    if MB_RESOLUTION_VERBOSE and rs:
                        vlog(f"Multi-route disambiguation {rid} at {parent}: {possible} -> {rs}")
                else:
                    rs = preferred_route_candidate(prefix, candidates) if MB_GUESS_AMBIGUOUS else None
            elif candidates:
                rs = candidates[0] if len(candidates) == 1 else (
                    preferred_route_candidate(prefix, candidates) if MB_GUESS_AMBIGUOUS else None
                )

        allow_station_inference = route_id_prefix(rid) not in NO_STATION_INFERENCE_PREFIXES

        # METHOD 4: Infer from exact platform only when static GTFS says it is unique.
        if not rs and allow_station_inference and sid and sid in STOP_ROUTE_PAIRS:
            possible_routes = STOP_ROUTE_PAIRS[sid]
            if len(possible_routes) == 1:
                rs = next(iter(possible_routes))
                if MB_RESOLUTION_VERBOSE:
                    vlog(f"Inferred route {rs} from stop_id {sid} (unique platform)")

        # METHOD 5: Infer from stop_id using station_line_pairs.csv
        # Only used as absolute last resort
        if not rs and allow_station_inference and parent and parent in STATION_ROUTE_PAIRS:
            possible_routes = STATION_ROUTE_PAIRS[parent]
            if len(possible_routes) == 1:
                rs = list(possible_routes)[0]
                if MB_RESOLUTION_VERBOSE:
                    vlog(f"Inferred route {rs} from stop {parent} (only option)")
            elif len(possible_routes) > 1:
                # Only choose from a multi-line station when the feed carries a strong route hint.
                if any(x in rid.upper() for x in ["AIR", "SHL", "APT", "ESS"]) and "T8" in possible_routes:
                    rs = "T8"
                    if MB_RESOLUTION_VERBOSE:
                        vlog(f"Inferred route {rs} from stop {parent} (keyword from {possible_routes})")
                elif rid and route_id_prefix(rid) not in IGNORED_ROUTE_PREFIXES and MB_GUESS_AMBIGUOUS:
                    prefix = route_id_prefix(rid)
                    rs = preferred_route_candidate(prefix, sorted(possible_routes))
                    if MB_RESOLUTION_VERBOSE and rs:
                        vlog(f"Guessed route {rs} from stop {parent} ({possible_routes})")

        if parent in FORCE_T1_PARENT_STOPS and rs == "T9":
            rs = "T1"

        status = int(getattr(v, "current_status", rt.VehiclePosition.IN_TRANSIT_TO))
        lat = getattr(v.position, "latitude", None) if v.HasField("position") else None
        lon = getattr(v.position, "longitude", None) if v.HasField("position") else None

        rs = rs or None
        route_debug[rid] = rs
        if not rs and rid and route_id_prefix(rid) not in IGNORED_ROUTE_PREFIXES:
            missing_routes[rid] = missing_routes.get(rid, 0) + 1

        if rs and ROUTE_SHORT_ALLOW and rs not in ROUTE_SHORT_ALLOW:
            continue

        trains.append((lat, lon, rs, sid, status))
        if rs:
            by_route[rs] = by_route.get(rs, 0) + 1

    vlog(f"collect_trains kept={len(trains)}, routes={len(by_route)}, null_routes={len([t for t in trains if t[2] is None])}")
    if missing_routes:
        vlog(f"Missing route mappings (top 10): {dict(sorted(missing_routes.items(), key=lambda x: -x[1])[:10])}")
    
    return trains, by_route, route_debug


# ---------------- FastAPI app ----------------
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

if CORS_ALLOW_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

ROUTE_ID_TO_SHORT: dict[str, str] = {}
TRIP_ID_TO_SHORT: dict[str, str] = {}
STATION_INDEX: dict[str, dict] = {}
CHILD_TO_PARENT: dict[str, str] = {}
STATION_ROUTE_PAIRS: dict[str, set[str]] = {}
STOP_ROUTE_PAIRS: dict[str, set[str]] = {}
STATION_NAME_TO_PARENT: dict[str, str] = {}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, TRIP_ID_TO_SHORT, STATION_INDEX, CHILD_TO_PARENT, BOARD_SETTINGS, STATION_ROUTE_PAIRS, STOP_ROUTE_PAIRS, STATION_NAME_TO_PARENT

    BOARD_SETTINGS = _load_board_settings()

    stops_path = _gtfs_data_path("stops.txt")
    routes_path = _gtfs_data_path("routes.txt")
    trips_path = _gtfs_data_path("trips.txt")
    stop_times_path = _gtfs_data_path("stop_times.txt")
    agency_path = _gtfs_data_path("agency.txt")

    validate_stop_times_file(stop_times_path)

    ROUTE_ID_TO_SHORT = load_route_shortnames(routes_path)
    TRIP_ID_TO_SHORT = load_trip_route_shortnames(trips_path, routes_path)
    STATION_ROUTE_PAIRS = load_station_route_pairs("station_line_pairs.csv")
    STOP_ROUTE_PAIRS = load_stop_route_pairs(stops_path, routes_path, trips_path, stop_times_path)
    CHILD_TO_PARENT = build_child_to_parent(stops_path)
    
    allowed_parents = require_allowed_parents(
        stops=stops_path,
        routes=routes_path,
        trips=trips_path,
        stop_times=stop_times_path,
        agency=agency_path,
        types=(1, 2),
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW,
    )

    if STATION_ROUTE_PAIRS:
        allowed_parents |= set(STATION_ROUTE_PAIRS.keys())
        allowed_pairs = {
            (pid, rs) for pid, routes in STATION_ROUTE_PAIRS.items() for rs in routes
        }
        vlog(f"allowed_pairs from station_line_pairs: {len(allowed_pairs)} (stations={len(STATION_ROUTE_PAIRS)})")
    else:
        allowed_pairs = require_allowed_pairs(
            stops=stops_path,
            routes=routes_path,
            trips=trips_path,
            stop_times=stop_times_path,
            agency=agency_path,
            types=(1, 2),
            agencies_allowed=AGENCY_NAME_ALLOW,
            route_short_allow=ROUTE_SHORT_ALLOW,
            allowed_parents_for_fallback=allowed_parents,
        )

    wl_path = os.getenv("STATION_WHITELIST_PATH", "stations_whitelist.txt")
    sid_w, name_w = load_whitelist(wl_path)

    STATION_INDEX = load_station_index(
        stops_path,
        bbox=SYD_BBOX,
        sid_whitelist=sid_w,
        name_whitelist=name_w,
        allowed_parents=allowed_parents,
        allowed_pairs=allowed_pairs,
    )

    for key, meta in STATION_INDEX.items():
        base_name = meta.get("base_name")
        pid = meta.get("stop_id")
        if base_name and pid:
            # Store clean name without route suffix
            STATION_NAME_TO_PARENT[base_name] = pid
    
    vlog(f"station_name→parent: {len(STATION_NAME_TO_PARENT)}")
    
    if not STATION_INDEX:
        raise RuntimeError("No stations after filtering")
    vlog(f"stations loaded: {len(STATION_INDEX)}")

@app.get("/")
def root():
    return {"ok": True}

@app.get("/healthz")
def healthz():
    age = None if _FEED_TS == 0 else (time.time() - _FEED_TS)
    return {
        "status": "ok",
        "stations": len(STATION_INDEX),
        "stop_route_pairs": len(STOP_ROUTE_PAIRS),
        "trains_last_fetch_s": age,
    }

@app.get("/login", response_class=HTMLResponse)
def login_get(error: T.Optional[str] = Query(None)):
    error_message = ""
    if error == "missing":
        error_message = "Enter the 5-character board ID to continue."
    elif error == "not-found":
        error_message = "We couldn't find that board ID. Double-check the code and try again."
    error_html = ""
    if error_message:
        error_html = f'<div class="error" role="alert">{error_message}</div>'
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Metroboard Login</title>
        <style>
          :root {
            color-scheme: light dark;
            --bg: #0b0e14;
            --panel: #131923;
            --border: #263042;
            --accent: #ff6b3d;
            --muted: #8a96a8;
            --text: #edf2f7;
            --shadow: 0 18px 40px rgba(4, 8, 16, 0.45);
            --line-blue: #2f80ed;
            --line-green: #27ae60;
            --line-red: #eb5757;
            --line-yellow: #f2c94c;
            --line-purple: #9b51e0;
          }
          * { box-sizing: border-box; }
          body {
            margin: 0;
            font-family: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top, #141b2b 0%, var(--bg) 55%);
            color: var(--text);
            min-height: 100vh;
          }
          .page {
            max-width: 880px;
            margin: 0 auto;
            padding: 32px 20px 60px;
          }
          .hero {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 24px;
            padding: 28px 30px;
            border-radius: 20px;
            background: linear-gradient(135deg, rgba(255,107,61,0.16), rgba(19,25,35,0.98));
            border: 1px solid rgba(255,255,255,0.06);
            box-shadow: var(--shadow);
          }
          .hero__title {
            font-size: 28px;
            font-weight: 700;
            letter-spacing: 0.02em;
          }
          .hero__subtitle { color: var(--muted); margin-top: 4px; }
          .hero__badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            padding: 6px 10px;
            border-radius: 999px;
            background: rgba(255,255,255,0.08);
            color: var(--text);
            margin-top: 12px;
          }
          .metro-strip {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 8px;
            margin: 22px 0;
          }
          .metro-strip span {
            height: 6px;
            border-radius: 999px;
            display: block;
          }
          .line-blue { background: var(--line-blue); }
          .line-green { background: var(--line-green); }
          .line-red { background: var(--line-red); }
          .line-yellow { background: var(--line-yellow); }
          .line-purple { background: var(--line-purple); }
          .card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 24px;
            box-shadow: var(--shadow);
          }
          label.field {
            display: flex;
            flex-direction: column;
            gap: 8px;
            color: var(--muted);
            font-size: 14px;
          }
          label.field input {
            background: rgba(255,255,255,0.04);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px 14px;
            font-size: 16px;
            outline: none;
            color-scheme: dark;
          }
          .helper {
            margin: 8px 0 0;
            font-size: 13px;
            color: var(--muted);
          }
          .error {
            margin-top: 14px;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid rgba(235, 87, 87, 0.55);
            background: rgba(235, 87, 87, 0.12);
            color: #f5c2c2;
            font-size: 14px;
          }
          button.primary {
            margin-top: 18px;
            width: 100%;
            background: var(--accent);
            color: #0d0f14;
            border: none;
            border-radius: 12px;
            padding: 12px 18px;
            font-weight: 700;
            font-size: 15px;
            cursor: pointer;
            box-shadow: 0 12px 30px rgba(255,107,61,0.35);
          }
          button.primary:hover {
            transform: translateY(-1px);
            box-shadow: 0 16px 32px rgba(255,107,61,0.4);
          }
          @media (max-width: 720px) {
            .hero { flex-direction: column; align-items: flex-start; }
          }
        </style>
      </head>
      <body>
        <main class="page">
          <div class="hero">
            <div>
              <div class="hero__title">Metroboard</div>
              <div class="hero__subtitle">Sign in to manage your board.</div>
              <div class="hero__badge">Train + Metro network · Live sync</div>
            </div>
          </div>
          <div class="metro-strip" aria-hidden="true">
            <span class="line-blue"></span>
            <span class="line-green"></span>
            <span class="line-red"></span>
            <span class="line-yellow"></span>
            <span class="line-purple"></span>
          </div>

          <section class="card">
            <form method="post" action="/login">
              <label class="field">Board ID
                <input type="text" name="board_id" minlength="5" maxlength="5" required placeholder="e.g. PZ23T" />
              </label>
              <p class="helper">Use the 5 character code printed on your card.</p>
              __ERROR_HTML__
              <button class="primary" type="submit">Open board</button>
            </form>
          </section>
        </main>
      </body>
    </html>
    """
    return html.replace("__ERROR_HTML__", error_html)

@app.post("/login")
async def login_post(board_id: str = Form(...)):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return RedirectResponse("/login?error=missing", status_code=303)
    if board_id not in BOARD_SETTINGS:
        vlog(f"login rejected: board_id {board_id} not found in BOARD_SETTINGS")
        return RedirectResponse("/login?error=not-found", status_code=303)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


def _render_board_settings_console(board_id: str, cfg: dict) -> str:
    b = min(150, int(cfg.get("brightness", 16)))
    mode = cfg.get("mode", "live")
    nm = cfg.get("night_mode", {}) or {}
    an = cfg.get("animation", {}) or {}
    test_mode = cfg.get("test_mode", {}) or {}
    ota = cfg.get("ota", {}) or {}

    an_color = an.get("color", [255, 80, 0])
    if not isinstance(an_color, list) or len(an_color) < 3:
        an_color = [255, 80, 0]
    an_color_hex = "#%02X%02X%02X" % (
        max(0, min(255, int(an_color[0]))),
        max(0, min(255, int(an_color[1]))),
        max(0, min(255, int(an_color[2]))),
    )

    def _firmware_size_label(size: T.Any) -> str:
        try:
            n = int(size)
        except Exception:
            return ""
        if n >= 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        if n >= 1024:
            return f"{n / 1024:.0f} KB"
        return f"{n} B"

    ota_filename = _safe_firmware_filename(ota.get("filename"))
    firmware_options = ['<option value="">Use global manifest</option>']
    for item in _available_firmware_files():
        filename = item.get("filename") or ""
        selected = " selected" if filename == ota_filename else ""
        size_label = _firmware_size_label(item.get("size"))
        label = f"{filename} ({size_label})" if size_label else filename
        firmware_options.append(
            f'<option value="{html.escape(filename)}"{selected}>{html.escape(label)}</option>'
        )

    test_states_json = json.dumps(
        _sanitize_test_states(test_mode.get("states", {})),
        separators=(",", ":"),
    )
    global_fw = _firmware_metadata()
    active_fw = _active_firmware_metadata_for_board(cfg)
    global_fw_text = (
        f"{global_fw.get('version')} / {global_fw.get('filename')}"
        if global_fw.get("available")
        else f"Unavailable: {global_fw.get('reason')}"
    )
    active_fw_text = (
        f"{active_fw.get('version')} / {active_fw.get('filename')}"
        if active_fw.get("available")
        else f"Unavailable: {active_fw.get('reason')}"
    )
    anim_name = str(an.get("name", "pulse")).strip().lower()
    if anim_name not in ANIMATION_NAMES:
        anim_name = "pulse"
    animation_options = []
    for name, label in ANIMATION_OPTIONS:
        selected = " selected" if name == anim_name else ""
        animation_options.append(f'<option value="{name}"{selected}>{html.escape(label)}</option>')
    custom_color_names_json = json.dumps(sorted(ANIMATION_CUSTOM_COLOR_NAMES), separators=(",", ":"))

    page = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Metroboard __BOARD_ID__</title>
    <style>
      :root { color-scheme: dark; --bg:#0d1117; --panel:#151b24; --panel2:#101720; --border:#2a3442; --text:#edf2f7; --muted:#93a4b8; --accent:#f59e0b; }
      * { box-sizing: border-box; }
      body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,Segoe UI,system-ui,-apple-system,sans-serif; }
      main { max-width:1180px; margin:0 auto; padding:24px 18px 56px; }
      a { color:var(--accent); }
      .topbar { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:14px; }
      h1 { margin:0; font-size:24px; letter-spacing:0; }
      h3 { margin:0; font-size:17px; }
      h4 { margin:0 0 8px; font-size:14px; }
      code { padding:2px 6px; border:1px solid var(--border); border-radius:6px; background:rgba(255,255,255,.05); }
      .sub,.muted,.note { color:var(--muted); }
      .sub { margin-top:5px; font-size:14px; }
      .note { font-size:13px; margin-top:8px; }
      .card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:20px; margin-top:14px; }
      .section-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
      .tag { background:rgba(255,255,255,.06); border:1px solid var(--border); border-radius:8px; padding:6px 10px; color:var(--muted); font-size:13px; }
      .status-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; }
      .status-item { border:1px solid var(--border); border-radius:8px; padding:12px 14px; background:rgba(255,255,255,.025); }
      .status-label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
      .status-value { margin-top:6px; font-size:19px; font-weight:750; }
      .status-sub { margin-top:4px; color:var(--muted); font-size:13px; }
      .layout-grid { display:grid; grid-template-columns:minmax(0,1fr) 270px; gap:14px; }
      .board-shell { background:#07090d; border:1px solid #303946; border-radius:8px; overflow:hidden; min-height:520px; }
      #boardSvg { display:block; width:100%; aspect-ratio:861/850; min-height:520px; }
      .board-overlay { opacity:.78; pointer-events:none; }
      body.overlay-off .board-overlay { display:none; }
      .network-title { fill:#d7dee7; font-size:46px; font-weight:750; letter-spacing:0; }
      .line-path { fill:none; stroke-width:6; stroke-linecap:round; stroke-linejoin:round; opacity:.58; }
      .station-node { fill:#2a2f38; stroke:#a9b4c1; stroke-width:2; cursor:default; transition:opacity 120ms ease, fill 120ms ease, r 120ms ease, stroke-width 120ms ease; }
      body[data-mode="test"] .station-node { cursor:pointer; }
      body[data-mode="test"] .station-node:hover, body[data-mode="test"] .station-node:focus { stroke:#fff; stroke-width:3; outline:none; }
      .station-node.state-1 { fill:var(--active-color,var(--line-color)); opacity:.34; }
      .station-node.state-2 { fill:var(--active-color,var(--line-color)); opacity:.68; }
      .station-node.state-3 { fill:var(--active-color,var(--line-color)); opacity:1; r:7; filter:drop-shadow(0 0 7px var(--active-color,var(--line-color))); }
      .station-label { fill:#d7e0ea; font-size:8px; pointer-events:none; paint-order:stroke; stroke:#07090d; stroke-width:3px; stroke-linejoin:round; opacity:.84; }
      .station-label.is-major { fill:#ffffff; font-size:10px; font-weight:750; opacity:.95; }
      body.labels-off .station-label { display:none; }
      .line-badge { fill:#111820; stroke:#c6d2df; stroke-width:1.5; }
      .line-badge-text { fill:#f4f7fb; font-size:14px; font-weight:800; text-anchor:middle; dominant-baseline:central; }
      .sim-side { border:1px solid var(--border); border-radius:8px; padding:12px; background:var(--panel2); }
      .sim-summary { display:grid; gap:4px; padding:10px 0 2px; }
      .sim-summary strong { font-size:22px; }
      .state-legend { display:grid; gap:7px; margin-top:12px; padding-top:12px; border-top:1px solid var(--border); color:var(--muted); font-size:13px; }
      .legend-row { display:flex; align-items:center; gap:8px; }
      .legend-dot { width:11px; height:11px; border-radius:999px; display:inline-block; border:1px solid rgba(255,255,255,.22); background:#2a2f38; }
      .legend-dot.state-1 { background:#667892; }
      .legend-dot.state-2 { background:#d39b2c; }
      .legend-dot.state-3 { background:#25d366; box-shadow:0 0 8px rgba(37,211,102,.65); }
      .sim-controls { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:12px; }
      button.control { border:1px solid var(--border); border-radius:8px; background:rgba(255,255,255,.05); color:var(--text); padding:9px 10px; font-weight:650; cursor:pointer; }
      button.control:hover { border-color:var(--accent); }
      button.control[data-line] { border-left:4px solid var(--line-color); }
      .sim-controls.is-disabled button { opacity:.55; cursor:not-allowed; }
      .mode-options { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; margin-top:12px; }
      .option { border:1px solid var(--border); border-radius:8px; padding:12px 14px; display:flex; gap:10px; align-items:center; background:rgba(255,255,255,.02); }
      .two-col { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }
      .row { display:flex; flex-wrap:wrap; gap:14px; margin-top:10px; }
      label.field { display:flex; flex-direction:column; gap:6px; min-width:220px; flex:1 1 220px; color:var(--muted); font-size:14px; }
      label.field input,label.field select { background:rgba(255,255,255,.04); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:10px 12px; font-size:15px; outline:none; color-scheme:dark; }
      label.field select option { background:#0f1420; color:var(--text); }
      .slider-row { display:flex; align-items:center; gap:12px; margin-top:12px; }
      input[type=range] { flex:1; accent-color:var(--accent); }
      .pill { display:inline-flex; background:rgba(245,158,11,.15); color:var(--accent); padding:6px 10px; border-radius:999px; font-weight:700; font-size:13px; }
      .color-picker { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:8px 12px; border-radius:8px; border:1px solid var(--border); background:rgba(255,255,255,.04); cursor:pointer; }
      .color-picker__input { width:44px; height:36px; padding:0; border:none; background:transparent; cursor:pointer; }
      .color-picker__value { display:inline-flex; align-items:center; gap:8px; font-size:14px; color:var(--text); font-weight:650; }
      .color-picker__swatch { width:18px; height:18px; border-radius:5px; border:1px solid rgba(255,255,255,.2); box-shadow:0 0 8px rgba(0,0,0,.4); }
      .color-picker__cta { font-size:12px; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }
      .hidden { display:none; }
      .actions { display:flex; justify-content:flex-end; margin-top:16px; }
      button.primary { background:var(--accent); color:#111; border:none; border-radius:8px; padding:12px 18px; font-weight:800; cursor:pointer; }
      button.primary:hover { filter:brightness(1.08); }
      @media (max-width:880px) { .layout-grid{grid-template-columns:1fr;} .topbar{align-items:flex-start; flex-direction:column;} }
      @media (max-width:640px) { main{padding:18px 10px 42px;} .card{padding:14px;} .sim-controls{grid-template-columns:1fr;} }
    </style>
  </head>
  <body data-mode="__MODE__">
    <main>
      <div class="topbar">
        <div>
          <h1>Metroboard Control</h1>
          <div class="sub">Board <code>__BOARD_ID__</code> - live, test, animation and OTA deployment controls</div>
        </div>
        <div><span class="tag" id="boardSyncTag">Checking</span> <a href="/login">Switch board</a></div>
      </div>

      <section class="card">
        <div class="section-head"><h3>Board Status</h3><span class="tag">Heartbeat</span></div>
        <div class="status-grid">
          <div class="status-item"><div class="status-label">Last check-in</div><div class="status-value" id="lastPayloadAge">--</div><div class="status-sub" id="lastPayloadAt">Waiting for board</div></div>
          <div class="status-item"><div class="status-label">Train feed age</div><div class="status-value" id="feedAge">--</div><div class="status-sub">TfNSW cache age</div></div>
          <div class="status-item"><div class="status-label">Settings version</div><div class="status-value" id="settingsVersion">__SETTINGS_VERSION__</div><div class="status-sub">Updates after saving</div></div>
          <div class="status-item"><div class="status-label">Firmware</div><div class="status-value" id="firmwareCurrent">--</div><div class="status-sub" id="firmwareSub">Waiting for board</div></div>
          <div class="status-item"><div class="status-label">OTA target</div><div class="status-value" id="otaTarget">__ACTIVE_FW_TEXT__</div><div class="status-sub">Per-board deployment</div></div>
        </div>
      </section>

      <form id="settingsForm" method="post" action="/board/__BOARD_ID__">
        <input type="hidden" id="test_states_json" name="test_states_json" value="__TEST_STATES_ATTR__" />

        <section class="card">
          <div class="section-head"><h3>Board Simulator</h3><span class="tag" id="simStateTag">Loading layout</span></div>
          <div class="layout-grid">
            <div class="board-shell"><svg id="boardSvg" viewBox="0 0 861 850" role="img" aria-label="Metroboard simulator"></svg></div>
            <div class="sim-side">
              <h4>Test Lights</h4>
              <p class="muted">Select Test lights mode, then click stations or use line controls. Save settings to apply to the physical board.</p>
              <div class="sim-summary">
                <div><strong id="activeStationCount">--</strong><span class="muted"> station LEDs lit</span></div>
                <div class="note" id="simUpdatedAt">Waiting for layout</div>
              </div>
              <label class="option" style="margin-top:12px;"><input id="labelToggle" type="checkbox" checked /><div><strong>Station labels</strong><p class="muted">Show station names on the simulator.</p></div></label>
              <label class="option hidden" id="overlayToggleOption" style="margin-top:8px;"><input id="overlayToggle" type="checkbox" checked /><div><strong>Board artwork</strong><p class="muted">Show the configured artwork under the station targets.</p></div></label>
              <div class="sim-controls" id="testControls">
                <button class="control" type="button" data-action="all-off">All off</button>
                <button class="control" type="button" data-action="all-on">All on</button>
              </div>
              <div class="state-legend" aria-label="Station state legend">
                <div class="legend-row"><span class="legend-dot"></span><span>Off</span></div>
                <div class="legend-row"><span class="legend-dot state-1"></span><span>Nearby</span></div>
                <div class="legend-row"><span class="legend-dot state-2"></span><span>Approaching</span></div>
                <div class="legend-row"><span class="legend-dot state-3"></span><span>At station</span></div>
              </div>
              <p class="note" id="testModeHint">Station clicks edit test lights only while Test lights mode is selected.</p>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="section-head"><h3>Display Mode</h3><span class="tag">Live, Test, Animation</span></div>
          <div class="mode-options">
            <label class="option"><input type="radio" name="mode" value="live" __MODE_LIVE_CHECKED__ /><div><strong>Live trains</strong><p class="muted">Use the network feed to light up stations.</p></div></label>
            <label class="option"><input type="radio" name="mode" value="test" __MODE_TEST_CHECKED__ /><div><strong>Test lights</strong><p class="muted">Display manually selected LEDs from the simulator.</p></div></label>
            <label class="option"><input type="radio" name="mode" value="animation" __MODE_ANIMATION_CHECKED__ /><div><strong>Animation</strong><p class="muted">Ignore live trains and play the configured effect.</p></div></label>
          </div>
        </section>

        <section class="card">
          <div class="section-head"><h3>Brightness</h3><span class="tag">0-150</span></div>
          <div class="slider-row"><input id="brightness" type="range" min="0" max="150" name="brightness" value="__BRIGHTNESS__" /><div class="pill" id="brightnessValue">__BRIGHTNESS__</div></div>
          <p class="note">Applies to the entire board; night mode can override this later.</p>
        </section>

        <section class="card">
          <div class="section-head"><h3>Night Mode</h3><span class="tag">Schedule</span></div>
          <label class="option"><input id="nm_enabled" type="checkbox" name="nm_enabled" __NM_ENABLED__ /><div><strong>Enable night mode</strong><p class="muted" id="nightStatus">Disabled</p></div></label>
          <div class="two-col" style="margin-top:12px;">
            <label class="field">Start time<input data-night-field type="time" name="nm_start" value="__NM_START__" /></label>
            <label class="field">End time<input data-night-field type="time" name="nm_end" value="__NM_END__" /></label>
          </div>
          <div class="two-col" style="margin-top:10px;">
            <label class="field">Action<select data-night-field name="nm_action" id="nm_action"><option value="off" __NM_ACTION_OFF_SELECTED__>Turn off</option><option value="dim" __NM_ACTION_DIM_SELECTED__>Dim</option></select></label>
            <label class="field">Night brightness<input data-night-field type="number" name="nm_dim" min="0" max="255" value="__NM_DIM__" /></label>
          </div>
        </section>

        <section class="card">
          <div class="section-head"><h3>Animation</h3><span class="tag" id="animationTag">Mode: __AN_NAME_TITLE__</span></div>
          <div class="row">
            <label class="field">Name<select name="an_name" id="an_name">__ANIMATION_OPTIONS__</select></label>
            <label class="field">Frame delay, ms<input type="number" name="an_speed" value="__AN_SPEED__" min="20" max="5000" /></label>
            <label class="field" id="pulseColorField">Effect colour<div class="color-picker"><input class="color-picker__input" type="color" id="an_color" name="an_color" value="__AN_COLOR_HEX__" /><span class="color-picker__value"><span class="color-picker__swatch" id="pulseColorSwatch" style="background:__AN_COLOR_HEX__;"></span><span id="pulseColorValue">__AN_COLOR_HEX__</span></span><span class="color-picker__cta">Change</span></div></label>
          </div>
          <p class="note" id="pulseColorNote"></p>
        </section>

        <section class="card">
          <div class="section-head"><h3>OTA Deployment</h3><span class="tag">Per-board target</span></div>
          <label class="option"><input id="ota_enabled" type="checkbox" name="ota_enabled" __OTA_ENABLED__ /><div><strong>Allow OTA updates for this board</strong><p class="muted">Disable this to hold a board on its current firmware.</p></div></label>
          <div class="two-col" style="margin-top:12px;">
            <label class="field">Firmware binary<select name="ota_filename" id="ota_filename">__FIRMWARE_OPTIONS__</select></label>
            <label class="field">Target version<input type="text" name="ota_target_version" value="__OTA_TARGET_VERSION__" placeholder="__GLOBAL_FW_VERSION__" maxlength="40" /></label>
          </div>
          <p class="note">Global manifest: __GLOBAL_FW_TEXT__. Active target for this board: __ACTIVE_FW_TEXT__.</p>
        </section>

        <div class="actions"><button class="primary" type="submit">Save settings</button></div>
      </form>
    </main>
    <script>
      (function() {
        const boardId = "__BOARD_ID__";
        let testStates = __TEST_STATES_JSON__;
        let layoutModel = null;
        let stationKeys = [];
        const nodeByKey = new Map();
        const brightness = document.getElementById("brightness");
        const brightnessValue = document.getElementById("brightnessValue");
        const nmToggle = document.getElementById("nm_enabled");
        const nightFields = document.querySelectorAll("[data-night-field]");
        const nightStatus = document.getElementById("nightStatus");
        const nmAction = document.getElementById("nm_action");
        const startEl = document.querySelector('input[name="nm_start"]');
        const endEl = document.querySelector('input[name="nm_end"]');
        const anName = document.getElementById("an_name");
        const pulseColorField = document.getElementById("pulseColorField");
        const pulseColorNote = document.getElementById("pulseColorNote");
        const pulseColorInput = document.getElementById("an_color");
        const pulseColorValue = document.getElementById("pulseColorValue");
        const pulseColorSwatch = document.getElementById("pulseColorSwatch");
        const boardSyncTag = document.getElementById("boardSyncTag");
        const lastPayloadAge = document.getElementById("lastPayloadAge");
        const lastPayloadAt = document.getElementById("lastPayloadAt");
        const feedAge = document.getElementById("feedAge");
        const settingsVersion = document.getElementById("settingsVersion");
        const firmwareCurrent = document.getElementById("firmwareCurrent");
        const firmwareSub = document.getElementById("firmwareSub");
        const otaTarget = document.getElementById("otaTarget");
        const svg = document.getElementById("boardSvg");
        const simStateTag = document.getElementById("simStateTag");
        const testControls = document.getElementById("testControls");
        const testStatesInput = document.getElementById("test_states_json");
        const labelToggle = document.getElementById("labelToggle");
        const overlayToggle = document.getElementById("overlayToggle");
        const overlayToggleOption = document.getElementById("overlayToggleOption");
        const activeStationCount = document.getElementById("activeStationCount");
        const simUpdatedAt = document.getElementById("simUpdatedAt");
        const customColorAnimations = new Set(__AN_CUSTOM_COLOR_NAMES__);
        const stateLabels = ["Off", "Nearby", "Approaching", "At station"];

        const currentMode = () => document.querySelector('input[name="mode"]:checked')?.value || "live";
        const syncBrightness = () => brightnessValue.textContent = brightness.value;
        const syncTestStateInput = () => { testStatesInput.value = JSON.stringify(testStates); };
        brightness.addEventListener("input", syncBrightness);
        syncBrightness();
        syncTestStateInput();

        const formatTime = (v) => {
          if (!v || !v.includes(":")) return v || "-";
          const [h, m] = v.split(":");
          const hr = parseInt(h, 10);
          const suffix = hr >= 12 ? "PM" : "AM";
          const hour12 = ((hr + 11) % 12) + 1;
          return `${hour12}:${m} ${suffix}`;
        };
        const syncNight = () => {
          const on = nmToggle.checked;
          nightFields.forEach(el => el.disabled = !on);
          const action = nmAction.value === "off" ? "Turn off" : "Dim to " + (document.querySelector('input[name="nm_dim"]').value || "20");
          nightStatus.textContent = on ? `${formatTime(startEl.value)} to ${formatTime(endEl.value)} - ${action}` : "Disabled";
        };
        nmToggle.addEventListener("change", syncNight);
        nightFields.forEach(el => el.addEventListener("input", syncNight));
        syncNight();

        const syncPulse = () => {
          const value = (pulseColorInput.value || "").toUpperCase();
          pulseColorValue.textContent = value;
          pulseColorSwatch.style.background = value;
          const usesCustomColor = customColorAnimations.has(anName.value);
          pulseColorField.classList.toggle("hidden", !usesCustomColor);
          pulseColorNote.textContent = usesCustomColor ? "This animation uses the selected effect colour." : "This animation uses line colours or generated rainbow colours.";
          document.getElementById("animationTag").textContent = "Mode: " + (anName.options[anName.selectedIndex]?.textContent || anName.value);
        };
        pulseColorInput.addEventListener("input", syncPulse);
        anName.addEventListener("change", syncPulse);
        syncPulse();

        const formatAge = (seconds) => {
          if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "--";
          const s = Math.max(0, Math.round(Number(seconds)));
          if (s < 60) return s + "s";
          const m = Math.floor(s / 60);
          const rem = s % 60;
          if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
          const h = Math.floor(m / 60);
          const mins = m % 60;
          return mins ? `${h}h ${mins}m` : `${h}h`;
        };

        const syncActiveColor = () => {
          const color = currentMode() === "animation" && customColorAnimations.has(anName.value)
            ? (pulseColorInput.value || "#FF5000")
            : "";
          nodeByKey.forEach(nodes => {
            nodes.forEach(node => {
              if (color) node.style.setProperty("--active-color", color);
              else node.style.removeProperty("--active-color");
            });
          });
        };

        const setNodeState = (key, state) => {
          const nodes = nodeByKey.get(key);
          if (!nodes) return;
          const normalized = Math.max(0, Math.min(3, Number(state || 0)));
          nodes.forEach(node => {
            node.classList.remove("state-0", "state-1", "state-2", "state-3");
            node.classList.add("state-" + normalized);
            node.dataset.state = String(normalized);
            node.setAttribute("aria-label", `${node.dataset.titleBase}: ${stateLabels[normalized]}`);
            const title = node.querySelector("title");
            if (title) title.textContent = `${node.dataset.titleBase}: ${stateLabels[normalized]}`;
          });
        };
        const applyStates = (states) => {
          syncActiveColor();
          const normalizedStates = {};
          stationKeys.forEach(key => {
            const state = Math.max(0, Math.min(3, Number(states[key] || 0)));
            normalizedStates[key] = state;
            setNodeState(key, state);
          });
          const active = stationKeys.filter(key => normalizedStates[key] > 0).length;
          const modeLabel = currentMode() === "test" ? "Test" : (currentMode() === "animation" ? "Animation" : "Live");
          if (simStateTag) simStateTag.textContent = `${modeLabel}: ${active}/${stationKeys.length} lit`;
          if (activeStationCount) activeStationCount.textContent = `${active}/${stationKeys.length}`;
          if (simUpdatedAt) simUpdatedAt.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
        };

        const buildTestControls = (model) => {
          testControls.querySelectorAll("[data-line]").forEach(btn => btn.remove());
          model.lines.forEach(line => {
            const btn = document.createElement("button");
            btn.className = "control";
            btn.type = "button";
            btn.dataset.action = "line-toggle";
            btn.dataset.line = line.line;
            btn.style.setProperty("--line-color", line.color);
            btn.textContent = line.line;
            btn.addEventListener("click", () => {
              if (currentMode() !== "test") return;
              const keys = Array.from(new Set(line.stations.map(st => st.key)));
              const anyOff = keys.some(key => Number(testStates[key] || 0) < 3);
              keys.forEach(key => testStates[key] = anyOff ? 3 : 0);
              syncTestStateInput();
              applyStates(testStates);
            });
            testControls.appendChild(btn);
          });
        };

        const toggleTestStation = (key) => {
          if (currentMode() !== "test") return;
          const current = Number(testStates[key] || 0);
          testStates[key] = current >= 3 ? 0 : 3;
          syncTestStateInput();
          applyStates(testStates);
        };

        const buildAnimationPreviewStates = () => {
          const keys = stationKeys;
          const states = {};
          if (!keys.length) return states;
          const speed = Math.max(80, Number(document.querySelector('input[name="an_speed"]')?.value || 180));
          const step = Math.floor(Date.now() / speed);
          const name = anName.value;
          if (name === "solid" || name === "rainbow") {
            keys.forEach(key => states[key] = 3);
            return states;
          }
          if (name === "sparkle") {
            keys.forEach((key, index) => states[key] = ((index * 17 + step * 13) % 29) < 4 ? 3 : 0);
            return states;
          }
          if (name === "theater") {
            keys.forEach((key, index) => states[key] = ((index + step) % 3) === 0 ? 3 : 0);
            return states;
          }
          if (name === "wipe") {
            const edge = step % (keys.length + 1);
            keys.forEach((key, index) => states[key] = index < edge ? 3 : 0);
            return states;
          }
          if (name === "bounce") {
            const period = Math.max(1, (keys.length - 1) * 2);
            let pos = step % period;
            if (pos >= keys.length) pos = period - pos;
            keys.forEach((key, index) => {
              const d = Math.abs(index - pos);
              states[key] = d === 0 ? 3 : (d === 1 ? 2 : (d === 2 ? 1 : 0));
            });
            return states;
          }
          const pos = step % keys.length;
          keys.forEach((key, index) => {
            const d = (index - pos + keys.length) % keys.length;
            states[key] = d === 0 ? 3 : (d === 1 ? 2 : (d === 2 ? 1 : 0));
          });
          return states;
        };

        const renderLayout = (model) => {
          const ns = "http://www.w3.org/2000/svg";
          svg.innerHTML = "";
          nodeByKey.clear();
          stationKeys = [];
          const viewBox = model.viewBox || [0, 0, 861, 850];
          svg.style.aspectRatio = `${viewBox[2]} / ${viewBox[3]}`;
          svg.setAttribute("viewBox", viewBox.join(" "));
          const bg = document.createElementNS(ns, "rect");
          bg.setAttribute("x", viewBox[0]);
          bg.setAttribute("y", viewBox[1]);
          bg.setAttribute("width", viewBox[2]);
          bg.setAttribute("height", viewBox[3]);
          bg.setAttribute("fill", "#07090d");
          svg.appendChild(bg);
          if (model.overlayUrl) {
            const overlay = document.createElementNS(ns, "image");
            overlay.setAttribute("class", "board-overlay");
            overlay.setAttribute("x", viewBox[0]);
            overlay.setAttribute("y", viewBox[1]);
            overlay.setAttribute("width", viewBox[2]);
            overlay.setAttribute("height", viewBox[3]);
            overlay.setAttribute("preserveAspectRatio", "none");
            overlay.setAttribute("href", model.overlayUrl);
            overlay.setAttributeNS("http://www.w3.org/1999/xlink", "href", model.overlayUrl);
            svg.appendChild(overlay);
          }
          if (overlayToggleOption) overlayToggleOption.classList.toggle("hidden", !model.overlayUrl);
          const title = document.createElementNS(ns, "text");
          title.setAttribute("x", "44");
          title.setAttribute("y", "66");
          title.setAttribute("class", "network-title");
          title.textContent = model.title || "Sydney Rail Network";
          svg.appendChild(title);
          model.lines.forEach(line => {
            (line.paths || [line.path]).filter(Boolean).forEach(path => {
              const poly = document.createElementNS(ns, "polyline");
              poly.setAttribute("class", "line-path");
              poly.setAttribute("points", path.map(p => p.join(",")).join(" "));
              poly.setAttribute("stroke", line.color);
              svg.appendChild(poly);
            });
          });
          model.lines.forEach(line => {
            if (!line.stations.length) return;
            const first = line.stations[0];
            const badge = document.createElementNS(ns, "rect");
            badge.setAttribute("x", first.x - 15);
            badge.setAttribute("y", first.y - 22);
            badge.setAttribute("width", "30");
            badge.setAttribute("height", "22");
            badge.setAttribute("rx", "3");
            badge.setAttribute("class", "line-badge");
            svg.appendChild(badge);
            const badgeText = document.createElementNS(ns, "text");
            badgeText.setAttribute("x", first.x);
            badgeText.setAttribute("y", first.y - 11);
            badgeText.setAttribute("class", "line-badge-text");
            badgeText.textContent = line.line;
            svg.appendChild(badgeText);
            line.stations.forEach(station => {
              if (!stationKeys.includes(station.key)) stationKeys.push(station.key);
              const label = document.createElementNS(ns, "text");
              const labelX = Number(station.x) + Number(station.labelDx || 0);
              const labelY = Number(station.y) + Number(station.labelDy || 0);
              label.setAttribute("x", labelX);
              label.setAttribute("y", labelY);
              label.setAttribute("class", "station-label" + (station.major ? " is-major" : ""));
              label.setAttribute("text-anchor", "middle");
              label.setAttribute("dominant-baseline", "central");
              label.setAttribute("transform", `rotate(${Number(station.labelAngle || 0)} ${labelX} ${labelY})`);
              label.textContent = station.name;
              svg.appendChild(label);
              const circle = document.createElementNS(ns, "circle");
              circle.setAttribute("cx", station.x);
              circle.setAttribute("cy", station.y);
              circle.setAttribute("r", "5");
              circle.setAttribute("class", "station-node state-0");
              circle.setAttribute("tabindex", "0");
              circle.setAttribute("role", "button");
              circle.dataset.key = station.key;
              circle.dataset.line = line.line;
              circle.dataset.titleBase = `${station.name} ${line.line}`;
              circle.style.setProperty("--line-color", line.color);
              const titleEl = document.createElementNS(ns, "title");
              titleEl.textContent = `${station.name} ${line.line}: Off`;
              circle.appendChild(titleEl);
              circle.addEventListener("click", () => {
                toggleTestStation(station.key);
              });
              circle.addEventListener("keydown", (event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                toggleTestStation(station.key);
              });
              if (!nodeByKey.has(station.key)) nodeByKey.set(station.key, []);
              nodeByKey.get(station.key).push(circle);
              svg.appendChild(circle);
            });
          });
          buildTestControls(model);
        };

        testControls.addEventListener("click", (event) => {
          const btn = event.target.closest("button[data-action]");
          if (!btn || currentMode() !== "test") return;
          const action = btn.dataset.action;
          if (action === "all-off" || action === "all-on") {
            stationKeys.forEach(key => testStates[key] = action === "all-on" ? 3 : 0);
            syncTestStateInput();
            applyStates(testStates);
          }
        });

        const fetchStatesInBatches = async () => {
          const merged = {};
          for (let i = 0; i < stationKeys.length; i += 90) {
            const keys = stationKeys.slice(i, i + 90);
            const qs = keys.map(encodeURIComponent).join(",");
            const resp = await fetch(`/board_payload?board_id=${encodeURIComponent(boardId)}&only=${qs}`, { cache: "no-store" });
            if (!resp.ok) throw new Error("status " + resp.status);
            const data = await resp.json();
            Object.assign(merged, data.states || {});
          }
          return merged;
        };
        const refreshSimulatorStates = async () => {
          if (!layoutModel) return;
          if (currentMode() === "test") {
            applyStates(testStates);
            return;
          }
          if (currentMode() === "animation") {
            applyStates(buildAnimationPreviewStates());
            return;
          }
          if (!stationKeys.length) return;
          try {
            applyStates(await fetchStatesInBatches());
          } catch (err) {
            if (simStateTag) simStateTag.textContent = "Simulator unavailable";
          }
        };
        const syncModeUi = () => {
          const isTest = currentMode() === "test";
          document.body.dataset.mode = currentMode();
          testControls.classList.toggle("is-disabled", !isTest);
          if (isTest) {
            applyStates(testStates);
          } else if (currentMode() === "animation") {
            refreshSimulatorStates();
          } else {
            applyStates({});
            refreshSimulatorStates();
          }
        };
        document.querySelectorAll('input[name="mode"]').forEach(el => el.addEventListener("change", syncModeUi));
        [anName, pulseColorInput, document.querySelector('input[name="an_speed"]')].forEach(el => {
          if (!el) return;
          el.addEventListener("input", () => {
            if (currentMode() === "animation") refreshSimulatorStates();
          });
          el.addEventListener("change", () => {
            if (currentMode() === "animation") refreshSimulatorStates();
          });
        });
        labelToggle.addEventListener("change", () => {
          document.body.classList.toggle("labels-off", !labelToggle.checked);
        });
        if (overlayToggle) {
          overlayToggle.addEventListener("change", () => {
            document.body.classList.toggle("overlay-off", !overlayToggle.checked);
          });
        }

        const loadLayout = async () => {
          try {
            const resp = await fetch("/board_layout", { cache: "no-store" });
            if (!resp.ok) throw new Error("status " + resp.status);
            layoutModel = await resp.json();
            renderLayout(layoutModel);
            syncModeUi();
            refreshSimulatorStates();
          } catch (err) {
            if (simStateTag) simStateTag.textContent = "Layout unavailable";
          }
        };
        const syncBoardStatus = async () => {
          try {
            const resp = await fetch(`/board_status?board_id=${encodeURIComponent(boardId)}`, { cache: "no-store" });
            if (!resp.ok) throw new Error("status " + resp.status);
            const data = await resp.json();
            const payloadAge = data.last_payload_age_seconds;
            lastPayloadAge.textContent = formatAge(payloadAge);
            lastPayloadAt.textContent = data.last_payload_at ? new Date(data.last_payload_at).toLocaleString() : "No board check-in yet";
            feedAge.textContent = formatAge(data.feed_age_seconds);
            settingsVersion.textContent = data.settings_version ?? "--";
            firmwareCurrent.textContent = data.firmware?.current || "--";
            const build = data.firmware?.build ? ` - ${data.firmware.build}` : "";
            const otaResult = data.firmware?.last_ota_result ? `; OTA ${data.firmware.last_ota_result}` : "";
            firmwareSub.textContent = data.firmware?.seen_age_seconds == null ? "No firmware report yet" : `Seen ${formatAge(data.firmware.seen_age_seconds)} ago${build}${otaResult}`;
            otaTarget.textContent = data.ota?.enabled ? (data.ota?.target_version || "Global") : "Held";
            boardSyncTag.textContent = payloadAge === null || payloadAge === undefined ? "No check-in" : "Updated " + formatAge(payloadAge) + " ago";
          } catch (err) {
            boardSyncTag.textContent = "Status unavailable";
          }
        };
        loadLayout();
        syncBoardStatus();
        window.setInterval(syncBoardStatus, 5000);
        window.setInterval(refreshSimulatorStates, 4000);
        window.setInterval(() => {
          if (currentMode() === "animation") refreshSimulatorStates();
        }, 220);
      })();
    </script>
  </body>
</html>
"""
    replacements = {
        "__BOARD_ID__": html.escape(board_id),
        "__MODE__": html.escape(str(mode)),
        "__SETTINGS_VERSION__": str(BOARD_SETTINGS_VERSION),
        "__ACTIVE_FW_TEXT__": html.escape(active_fw_text),
        "__GLOBAL_FW_TEXT__": html.escape(global_fw_text),
        "__GLOBAL_FW_VERSION__": html.escape(str(global_fw.get("version") or "Global manifest")),
        "__TEST_STATES_JSON__": test_states_json,
        "__TEST_STATES_ATTR__": html.escape(test_states_json, quote=True),
        "__MODE_LIVE_CHECKED__": "checked" if mode == "live" else "",
        "__MODE_TEST_CHECKED__": "checked" if mode == "test" else "",
        "__MODE_ANIMATION_CHECKED__": "checked" if mode == "animation" else "",
        "__BRIGHTNESS__": str(b),
        "__NM_ENABLED__": "checked" if nm.get("enabled") else "",
        "__NM_START__": html.escape(str(nm.get("start", "23:00"))),
        "__NM_END__": html.escape(str(nm.get("end", "06:00"))),
        "__NM_ACTION_OFF_SELECTED__": "selected" if nm.get("action", "off") == "off" else "",
        "__NM_ACTION_DIM_SELECTED__": "selected" if nm.get("action") == "dim" else "",
        "__NM_DIM__": str(max(0, min(255, int(nm.get("dim_level", 20))))),
        "__AN_NAME_TITLE__": html.escape(dict(ANIMATION_OPTIONS).get(anim_name, anim_name.title())),
        "__ANIMATION_OPTIONS__": "\n".join(animation_options),
        "__AN_CUSTOM_COLOR_NAMES__": custom_color_names_json,
        "__AN_SPEED__": str(max(20, min(5000, int(an.get("speed", 120))))),
        "__AN_COLOR_HEX__": an_color_hex,
        "__OTA_ENABLED__": "checked" if ota.get("enabled", True) else "",
        "__FIRMWARE_OPTIONS__": "\n".join(firmware_options),
        "__OTA_TARGET_VERSION__": html.escape(str(ota.get("target_version") or "")),
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


@app.get("/board/{board_id}", response_class=HTMLResponse)
def board_settings_page(board_id: str):
    board_id = _normalize_board_id(board_id)
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return HTMLResponse("Invalid board_id", status_code=400)

    return _render_board_settings_console(board_id, cfg)

@app.post("/board/{board_id}")
async def board_settings_save(
    board_id: str,
    mode: str = Form("live"),
    brightness: int = Form(16),
    nm_enabled: T.Optional[str] = Form(None),
    nm_start: str = Form("23:00"),
    nm_end: str = Form("06:00"),
    nm_action: str = Form("off"),
    nm_dim: int = Form(20),
    an_name: str = Form("pulse"),
    an_speed: int = Form(120),
    an_color: str = Form("#FF5000"),
    test_states_json: str = Form("{}"),
    ota_enabled: T.Optional[str] = Form(None),
    ota_target_version: str = Form(""),
    ota_filename: str = Form(""),
):
    board_id = _normalize_board_id(board_id)
    cfg = _get_or_create_board(board_id)

    mode = mode if mode in ("live", "animation", "test") else "live"
    brightness = max(0, min(150, brightness))
    nm_dim = max(0, min(255, nm_dim))

    def _parse_hex_color(s: str) -> list[int]:
        s = s.lstrip("#")
        if len(s) == 3: s = "".join(c*2 for c in s)
        if len(s) != 6: return [255, 80, 0]
        return [int(s[i:i+2], 16) for i in (0, 2, 4)]

    cfg["mode"] = mode
    cfg["brightness"] = brightness
    cfg["night_mode"] = {
        "enabled": bool(nm_enabled),
        "start": nm_start,
        "end": nm_end,
        "action": nm_action,
        "dim_level": nm_dim,
    }
    an_name = str(an_name or "pulse").strip().lower()
    if an_name not in ANIMATION_NAMES:
        an_name = "pulse"
    cfg["animation"] = {
        "name": an_name,
        "speed": max(20, min(5000, int(an_speed))),
        "color": _parse_hex_color(an_color),
    }
    cfg["test_mode"] = {
        "states": _sanitize_test_states(test_states_json),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    safe_ota_filename = _safe_firmware_filename(ota_filename)
    if ota_filename and not safe_ota_filename:
        safe_ota_filename = ""
    target_version = str(ota_target_version or "").strip()[:40]
    if safe_ota_filename and not target_version:
        target_version = _firmware_manifest_config()[0]
    cfg["ota"] = {
        "enabled": bool(ota_enabled),
        "target_version": target_version,
        "filename": safe_ota_filename,
    }

    BOARD_SETTINGS[board_id] = cfg
    _save_board_settings()

    return RedirectResponse(f"/board/{board_id}", status_code=303)

@app.get("/debug")
def debug():
    feed, _ = fetch_vp_cached()
    trains, by_route, route_debug = collect_trains(feed, ROUTE_ID_TO_SHORT)
    return {
        "stations_count": len(STATION_INDEX),
        "stop_route_pairs": len(STOP_ROUTE_PAIRS),
        "bbox": list(SYD_BBOX) if SYD_BBOX else None,
        "feeds": URLS_VP,
        "total_trains": len(trains),
        "routes_found": sorted(by_route.keys()),
        "routes_missing": [
            r for r, v in route_debug.items()
            if r and v is None and route_id_prefix(r) not in IGNORED_ROUTE_PREFIXES
        ],
        "routes_ignored": [
            r for r, v in route_debug.items()
            if r and v is None and route_id_prefix(r) in IGNORED_ROUTE_PREFIXES
        ],
        "routes_resolved": route_debug,
    }

@app.get("/trains")
def trains():
    """Debug endpoint to see raw train data"""
    feed, age = fetch_vp_cached()
    trains, _, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)

    # rt.VehiclePosition.VehicleStopStatus:
    # INCOMING_AT=0, STOPPED_AT=1, IN_TRANSIT_TO=2
    def status_name(st: int) -> str:
        return {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}.get(st, "?")

    out = []
    # Also collect raw route_ids from feed for debugging
    raw_routes = set()
    
    for e in feed.entity[:50]:  # Sample first 50
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        if rid:
            raw_routes.add(rid)
    
    for lat, lon, rs, sid, st in trains[:50]:
        out.append({
            "route": rs,
            "lat": lat,
            "lon": lon,
            "stop_id": sid,
            "status": status_name(st),
            "parent_station": CHILD_TO_PARENT.get(sid, sid) if sid else None,
        })

    return {
        "feed_age_seconds": age, 
        "total_trains": len(trains),
        "trains_with_routes": len([t for t in trains if t[2] is not None]),
        "trains_without_routes": len([t for t in trains if t[2] is None]),
        "sample_raw_route_ids": sorted(list(raw_routes)[:20]),
        "sample_trains": out
    }

def _as_no_store(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/board_settings")
def board_settings_api(board_id: str = Query(...)):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return _as_no_store(JSONResponse({"error": "missing board_id"}, status_code=400))
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return _as_no_store(JSONResponse({"error": "invalid board_id"}, status_code=400))
    payload = dict(cfg)
    payload["settings_version"] = BOARD_SETTINGS_VERSION
    return _as_no_store(JSONResponse(payload))

@app.get("/board_status")
def board_status_api(board_id: str = Query(...)):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return _as_no_store(JSONResponse({"error": "missing board_id"}, status_code=400))
    try:
        _get_or_create_board(board_id)
    except ValueError:
        return _as_no_store(JSONResponse({"error": "invalid board_id"}, status_code=400))
    return _as_no_store(JSONResponse(_board_status_snapshot(board_id)))


@app.get("/board_layout")
def board_layout_api():
    return _as_no_store(JSONResponse(_board_layout_model()))


@app.get("/board_overlay")
def board_overlay_api():
    path = _board_overlay_file_path()
    if not BOARD_OVERLAY_PATH or not os.path.exists(path) or not os.path.isfile(path):
        return JSONResponse({"error": "board_overlay_not_found"}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/firmware_options")
def firmware_options_api(board_id: str = Query(...)):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return _as_no_store(JSONResponse({"error": "missing board_id"}, status_code=400))
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return _as_no_store(JSONResponse({"error": "invalid board_id"}, status_code=400))
    global_manifest = dict(_firmware_metadata())
    global_manifest.pop("path", None)
    return _as_no_store(JSONResponse({
        "files": _available_firmware_files(),
        "current_target": cfg.get("ota", {}) or {},
        "global_manifest": global_manifest,
    }))


@app.get("/firmware_manifest")
def firmware_manifest_api(
    request: Request,
    board_id: str = Query(...),
    current: T.Optional[str] = Query(None),
    build: T.Optional[str] = Query(None),
    record: bool = Query(True),
):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return _as_no_store(JSONResponse({"error": "missing board_id"}, status_code=400))
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return _as_no_store(JSONResponse({"error": "invalid board_id"}, status_code=400))

    if record:
        _record_board_firmware_status(board_id, current=current, build=build, manifest_check=True)

    meta = _active_firmware_metadata_for_board(cfg)
    current_version = (current or "").strip()
    update_available = bool(meta.get("available")) and _firmware_update_available(current_version, meta.get("version"))
    reason = None
    if not meta.get("available"):
        reason = meta.get("reason")
    elif not update_available:
        reason = "not_newer"
    ota_cfg = cfg.get("ota", {}) or {}

    payload = {
        "ota_enabled": FIRMWARE_OTA_ENABLED and bool(ota_cfg.get("enabled", True)),
        "update_available": update_available,
        "current": current_version,
        "version": meta.get("version"),
        "reason": reason,
        "deployment": {
            "filename": ota_cfg.get("filename") or "",
            "target_version": ota_cfg.get("target_version") or "",
        },
    }

    if meta.get("available"):
        encoded_board = quote(board_id, safe="")
        encoded_version = quote(str(meta["version"]), safe="")
        encoded_filename = quote(str(meta["filename"]), safe="")
        payload.update({
            "filename": meta["filename"],
            "size": meta["size"],
            "md5": meta["md5"],
            "sha256": meta["sha256"],
            "url": (
                f"{_public_base_url(request)}/firmware.bin"
                f"?board_id={encoded_board}&version={encoded_version}"
                f"&filename={encoded_filename}"
            ),
        })

    return _as_no_store(JSONResponse(payload))


@app.get("/firmware.bin")
def firmware_binary_api(
    board_id: str = Query(...),
    version: T.Optional[str] = Query(None),
    filename: T.Optional[str] = Query(None),
):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return JSONResponse({"error": "missing board_id"}, status_code=400)
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return JSONResponse({"error": "invalid board_id"}, status_code=400)

    if filename:
        meta = _firmware_metadata_for_filename(filename, version or "")
    else:
        meta = _active_firmware_metadata_for_board(cfg)
    if not meta.get("available"):
        return JSONResponse({"error": "firmware_unavailable", "reason": meta.get("reason")}, status_code=404)
    if version and version.strip() != meta.get("version"):
        return JSONResponse({"error": "firmware_version_not_found"}, status_code=404)

    headers = {
        "Cache-Control": "no-store",
        "X-Firmware-Version": str(meta["version"]),
        "X-Firmware-MD5": str(meta["md5"]),
        "X-Firmware-SHA256": str(meta["sha256"]),
    }
    return FileResponse(
        meta["path"],
        media_type="application/octet-stream",
        filename=meta["filename"],
        headers=headers,
    )


def _sanitize_test_states(raw: T.Any) -> dict[str, int]:
    if isinstance(raw, str):
        if len(raw) > 20000:
            return {}
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}

    valid_keys = set(STATION_INDEX.keys()) | _board_layout_keys()
    out: dict[str, int] = {}
    for key, value in raw.items():
        if len(out) >= 600:
            break
        key = str(key).strip()
        if key not in valid_keys:
            continue
        try:
            state = int(value)
        except Exception:
            continue
        out[key] = max(0, min(3, state))
    return out

def _calculate_test_states_for_subset(cfg: dict, subset: dict[str, dict]) -> dict[str, int]:
    states = ((cfg.get("test_mode") or {}).get("states") or {})
    return {key: max(0, min(3, int(states.get(key, 0) or 0))) for key in subset.keys()}

def _station_subset(only: T.Optional[str]) -> dict[str, dict]:
    subset = STATION_INDEX
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        layout_keys = _board_layout_keys()
        subset = {}
        for n in wanted:
            if n in STATION_INDEX:
                subset[n] = STATION_INDEX[n]
            elif n in layout_keys:
                route_short = "M" if n.endswith("M") else n[-1:]
                subset[n] = {
                    "name": n,
                    "stop_id": None,
                    "lat": 0.0,
                    "lon": 0.0,
                    "route_short": route_short,
                }
    return subset


def _calculate_states_for_subset(subset: dict[str, dict]) -> dict[str, int]:
    if not subset:
        return {}

    feed, _ = fetch_vp_cached()
    trains, _by_route, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)

    if MB_VERBOSE:
        vlog(f"processing trains={len(trains)} stations={len(subset)}")

    if not trains:
        return {k: 0 for k in subset.keys()}

    VP_INCOMING = rt.VehiclePosition.INCOMING_AT
    VP_STOPPED = rt.VehiclePosition.STOPPED_AT

    out: dict[str, int] = {}
    matches = 0
    dist_fb = 0
    name_matches = 0  # NEW: track name-based matches
    nearest_by_train: dict[int, set[str]] = {}

    for name, meta in subset.items():
        pid = meta["stop_id"]
        slat, slon = meta["lat"], meta["lon"]
        station_route = (meta.get("route_short") or "").upper()

        best = 0
        best_d = None

        for train_idx, (tlat, tlon, train_route, child_sid, st) in enumerate(trains):
            if train_route is None:
                if MB_STRICT_ROUTE_MATCH:
                    continue
            elif train_route != station_route:
                continue

            parent = None
            if not MB_FORCE_DISTANCE and child_sid:
                # Try 1: Direct parent lookup (numeric GTFS stop_ids)
                parent = CHILD_TO_PARENT.get(child_sid) or (child_sid if is_num_parent(child_sid) else None)
                
                # Try 2: Extract station name from location-based stop_ids (TfNSW format)
                if not parent or parent != pid:
                    extracted_name = extract_station_name_from_stop_id(child_sid)
                    if extracted_name:
                        parent = STATION_NAME_TO_PARENT.get(extracted_name)
                        if parent == pid:
                            name_matches += 1
                
                if parent == pid:
                    if st == VP_STOPPED:
                        best = 3
                        matches += 1
                        break
                    elif st == VP_INCOMING:
                        best = max(best, 2)
                        matches += 1

            if best < 3 and tlat is not None and tlon is not None:
                if parent != pid:
                    if train_idx not in nearest_by_train:
                        nearest_by_train[train_idx] = route_nearest_station_names(station_route, tlat, tlon)
                    nearest = nearest_by_train[train_idx]
                    if nearest and name not in nearest:
                        continue
                d = hav_m(slat, slon, tlat, tlon)
                if best_d is None or d < best_d:
                    best_d = d

        if best == 0 and best_d is not None:
            best = status_from_dist(best_d)
            if best > 0:
                dist_fb += 1

        out[name] = best


    if MB_VERBOSE:
        act = sum(1 for v in out.values() if v > 0)
        vlog(f"result active={act}/{len(subset)} matches={matches} (name_based={name_matches}) distance_fb={dist_fb}")

    return out


@app.get("/stations")
def stations():
    # Sorted by name for stable UI
    return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(
    only: T.Optional[str] = Query(None, description="Comma-separated station names"),
    board_id: T.Optional[str] = Query(None, description="Board ID (5-char code)")
):
    """
    Returns status for each station.
    If board_id is provided, we require that board to exist in boards.json.
    """
    cfg = None
    if board_id is not None:
        board_id = _normalize_board_id(board_id)
        if not board_id or board_id not in BOARD_SETTINGS:
            return JSONResponse({"error": "unknown board_id"}, status_code=403)
        cfg = _get_or_create_board(board_id)

    subset = _station_subset(only)
    if only and not subset:
        return JSONResponse({})

    if cfg and cfg.get("mode") == "test":
        return JSONResponse(_calculate_test_states_for_subset(cfg, subset))

    return JSONResponse(_calculate_states_for_subset(subset))


@app.get("/board_payload")
def board_payload(
    board_id: str = Query(..., description="Board ID (5-char code)"),
    only: T.Optional[str] = Query(None, description="Comma-separated station names"),
    fw: T.Optional[str] = Query(None, description="Firmware version reported by the board"),
    build: T.Optional[str] = Query(None, description="Firmware build label reported by the board"),
    ota: T.Optional[str] = Query(None, description="Last OTA result reported by the board"),
    ota_target: T.Optional[str] = Query(None, description="Last OTA target version reported by the board"),
):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return JSONResponse({"error": "missing board_id"}, status_code=400)
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return JSONResponse({"error": "invalid board_id"}, status_code=400)

    now_ts = time.time()
    previous_seen_at = BOARD_LAST_PAYLOAD_AT.get(board_id)
    BOARD_LAST_PAYLOAD_AT[board_id] = now_ts
    _record_board_firmware_status(
        board_id,
        current=fw,
        build=build,
        ota_result=ota,
        ota_target=ota_target,
    )

    subset = _station_subset(only)
    mode = cfg.get("mode", "live")
    if mode == "test":
        states_payload = _calculate_test_states_for_subset(cfg, subset)
    else:
        states_payload = _calculate_states_for_subset(subset)

    base_brightness = int(cfg.get("brightness", 16))
    nm_cfg = cfg.get("night_mode", {}) or {}
    effective_brightness, nm_active = _night_mode_effect(base_brightness, nm_cfg)

    return JSONResponse({
        "board_id": board_id,
        "mode": mode,
        "brightness": base_brightness,
        "brightness_effective": effective_brightness,
        "night_mode": nm_cfg,
        "night_mode_active": nm_active,
        "animation": cfg.get("animation", {}),
        "states": states_payload,
        "settings_version": BOARD_SETTINGS_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "feed_age_seconds": _feed_age_seconds(),
        "board_seen_at": _iso_utc_from_ts(now_ts),
        "board_previous_seen_at": _iso_utc_from_ts(previous_seen_at),
        "board_previous_seen_age_seconds": (
            None if previous_seen_at is None else max(0.0, now_ts - previous_seen_at)
        ),
    })

@app.get("/debug_names")
def debug_names():
    """Debug endpoint to see name extraction from realtime stop_ids"""
    feed, age = fetch_vp_cached()
    trains, _, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)
    
    extractions = []
    for lat, lon, route, stop_id, status in trains[:50]:
        if stop_id:
            extracted = extract_station_name_from_stop_id(stop_id)
            parent = STATION_NAME_TO_PARENT.get(extracted) if extracted else None
            extractions.append({
                "stop_id": stop_id,
                "extracted_name": extracted,
                "mapped_parent": parent,
                "route": route
            })
    
    return {
        "name_to_parent_mapping_size": len(STATION_NAME_TO_PARENT),
        "sample_mappings": list(STATION_NAME_TO_PARENT.items())[:20],
        "extraction_samples": extractions
    }

# ---------------- run (dev) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
