"""
Project Ark — Flask backend
---------------------------
Offline RAG pipeline + web-based content management:

    client  ->  Flask (this file)  ->  Kiwix-serve (local .zim files)
                                   ->  Ollama (local LLM)
                                   ->  client

    admin   ->  Flask /admin       ->  Kiwix OPDS catalog (internet)
                                   ->  Chunked resumable downloader
                                   ->  SSD (zims/ + library.xml + config.json)

Runs on port 80 so the router's captive portal can redirect directly to it.
The venv python is granted CAP_NET_BIND_SERVICE by install.sh.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ---------- Config ----------
KIWIX_BASE   = os.environ.get("ARK_KIWIX_URL", "http://127.0.0.1:8080")
LLM_BASE     = os.environ.get("ARK_LLM_URL", "http://127.0.0.1:8001")
LLM_MODEL    = os.environ.get("ARK_LLM_MODEL", "google/gemma-4-E2B-it")
ARK_DATA_DIR = os.environ.get("ARK_DATA_DIR", "/mnt/ssd-ark/ark-data")
VERBOSE      = os.environ.get("ARK_VERBOSE", "").lower() in ("1", "true", "yes")

ZIM_DIR      = os.path.join(ARK_DATA_DIR, "zims")
LIBRARY_XML  = os.path.join(ARK_DATA_DIR, "library.xml")
CONFIG_PATH  = os.path.join(ARK_DATA_DIR, "config.json")
KIWIX_MANAGE = "/opt/kiwix/kiwix-manage"

OPDS_CATALOG = "https://library.kiwix.org/catalog/v2/entries"
CHUNK_SIZE   = 8192
HTTP_TIMEOUT = (5, 60)

MAX_CONTEXT_WORDS = 1500

SYSTEM_PROMPT = (
    "You are an emergency offline survival assistant running on a local device "
    "with no internet. Answer using ONLY the provided article context. "
    "The user may use abbreviations, slang, or informal language — interpret "
    "them generously (e.g. 'CPR' = cardiopulmonary resuscitation, "
    "'broken arm' = fracture management). "
    "If the article content is clearly NOT about the topic the user is asking "
    "about, reply EXACTLY with 'IRRELEVANT_ARTICLE' and nothing else. "
    "Otherwise: be concise, use bullet points, prioritize actionable steps. "
    "Cite which article you used at the end of your answer."
)

# ---------------------------------------------------------------------------
# Natural-language → search-term mapping
# ---------------------------------------------------------------------------
# Kiwix search is keyword-based.  Users in a survival situation will type
# natural language ("how do I set a broken arm", "my kid is choking").
# We need to bridge the gap WITHOUT an extra LLM call (too slow on Pi).
#
# Strategy (all instant, no LLM):
#   1. Strip stop words → core keywords
#   2. Expand abbreviations (CPR → cardiopulmonary resuscitation)
#   3. Map everyday phrases to Wikipedia article titles (concept synonyms)
#   4. Search Kiwix with multiple query variations, merge & deduplicate
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset(
    "a an the is are was were be been being do does did doing have has had "
    "having i me my we our you your he she it they them his her its their "
    "this that these those am will would shall should can could may might "
    "must to of in for on with at by from as into through about between "
    "how what when where why who which whom whose if then than so very "
    "just really also still even much many some any no not don't doesn't "
    "didn't won't can't couldn't shouldn't wouldn't get got please help "
    "need want know think going go".split()
)

ABBREVIATIONS: dict[str, str] = {
    "cpr": "cardiopulmonary resuscitation",
    "aed": "automated external defibrillator",
    "ppe": "personal protective equipment",
    "ems": "emergency medical services",
    "otc": "over-the-counter medication",
    "iv": "intravenous therapy",
    "bp": "blood pressure",
    "hr": "heart rate",
    "ob": "obstetrics childbirth",
    "er": "emergency room first aid",
    "uti": "urinary tract infection",
    "std": "sexually transmitted infection",
    "hvac": "heating ventilation air conditioning",
    "emp": "electromagnetic pulse",
    "mre": "meal ready to eat",
    "sop": "standard operating procedure",
    "diy": "do it yourself repair",
    "ac": "air conditioning",
}

# Map everyday/informal phrases to the Wikipedia (or iFixit) article titles
# that actually contain the information.  Keys are lowercased substrings that
# will be checked against the query; values are extra search terms injected
# alongside the original query.
CONCEPT_SYNONYMS: dict[str, list[str]] = {
    # Medical / first aid
    "broken arm":     ["fracture bone splint"],
    "broken leg":     ["fracture bone splint"],
    "broken bone":    ["fracture bone first aid"],
    "sprain":         ["sprain strain first aid"],
    "choking":        ["choking first aid heimlich"],
    "heart attack":   ["myocardial infarction first aid"],
    "stroke":         ["stroke cerebrovascular first aid"],
    "bleeding":       ["hemorrhage wound first aid"],
    "cut":            ["wound laceration first aid"],
    "burn":           ["burn first aid treatment"],
    "snake bite":     ["snakebite envenomation first aid"],
    "bee sting":      ["insect sting allergy anaphylaxis"],
    "allergic":       ["anaphylaxis allergy epinephrine"],
    "drowning":       ["drowning rescue resuscitation"],
    "hypothermia":    ["hypothermia cold exposure"],
    "heat stroke":    ["heat stroke hyperthermia"],
    "dehydration":    ["dehydration oral rehydration"],
    "diarrhea":       ["diarrhea oral rehydration"],
    "infection":      ["infection wound antiseptic"],
    "fever":          ["fever antipyretic treatment"],
    "concussion":     ["concussion traumatic brain injury"],
    "tourniquet":     ["tourniquet hemorrhage control"],
    "stitch":         ["suture wound closure"],
    "dislocate":      ["dislocation joint reduction"],
    "frostbite":      ["frostbite cold injury"],
    "poison":         ["poisoning first aid treatment"],
    "overdose":       ["drug overdose first aid"],
    "seizure":        ["seizure epilepsy first aid"],
    "asthma":         ["asthma attack inhaler"],
    "diabetic":       ["diabetic emergency hypoglycemia"],
    "deliver a baby": ["childbirth emergency delivery"],
    "giving birth":   ["childbirth emergency delivery"],
    "chest pain":     ["chest pain cardiac emergency"],
    "unconscious":    ["unconsciousness recovery position"],
    "not breathing":  ["respiratory arrest resuscitation"],
    "shock":          ["shock medical emergency"],
    # Water / shelter / survival
    "purify water":   ["water purification treatment"],
    "clean water":    ["water purification filtration"],
    "boil water":     ["water purification boiling"],
    "filter water":   ["water filter purification"],
    "start a fire":   ["fire making friction ignition"],
    "make fire":      ["fire making survival"],
    "shelter":        ["emergency shelter survival"],
    "signal for help":["distress signal rescue"],
    "lost in woods":  ["wilderness survival navigation"],
    "compass":        ["navigation compass orientation"],
    "edible plant":   ["foraging wild edible plants"],
    "fishing":        ["fishing survival food"],
    "trap":           ["trapping hunting survival"],
    "knot":           ["knot tying rope"],
    "rope":           ["knot rope cordage"],
    # Repair / tech
    "phone screen":   ["screen replacement repair"],
    "cracked screen": ["screen replacement repair"],
    "flat tire":      ["tire repair puncture"],
    "car won't start":["automobile troubleshooting battery"],
    "generator":      ["electric generator portable"],
    "solar panel":    ["solar panel photovoltaic"],
    "battery":        ["battery charging maintenance"],
    "radio":          ["radio communication emergency"],
    "sewing":         ["sewing repair textile"],
}

# Curated offline content catalog. `kiwix_name` + `kiwix_flavour` are used
# to resolve the current download URL via the Kiwix OPDS v2 catalog.
RESOURCE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "wikipedia_maxi",
        "name": "Wikipedia (English, Full)",
        "description": "Complete English Wikipedia with all images. The big one.",
        "category": "Reference",
        "approx_size_gb": 124.0,
        "kiwix_name": "wikipedia_en_all",
        "kiwix_flavour": "maxi",
    },
    {
        "id": "wikipedia_nopic",
        "name": "Wikipedia (English, Text Only)",
        "description": "Full English Wikipedia without images. Much smaller footprint.",
        "category": "Reference",
        "approx_size_gb": 52.0,
        "kiwix_name": "wikipedia_en_all",
        "kiwix_flavour": "nopic",
    },
    {
        "id": "wikimed",
        "name": "WikiMed Medicine",
        "description": "All medical articles from Wikipedia with images. Critical for triage.",
        "category": "Medical",
        "approx_size_gb": 2.2,
        "kiwix_name": "wikipedia_en_medicine",
        "kiwix_flavour": "maxi",
    },
    {
        "id": "ifixit",
        "name": "iFixit Repair Guides",
        "description": "Full iFixit repair library — electronics, appliances, tools.",
        "category": "Skills",
        "approx_size_gb": 3.6,
        "kiwix_name": "ifixit_en_all",
        "kiwix_flavour": "",
    },
    {
        "id": "gutenberg",
        "name": "Project Gutenberg",
        "description": "~70,000 public-domain books. Literature, manuals, reference.",
        "category": "Library",
        "approx_size_gb": 221.0,
        "kiwix_name": "gutenberg_en_all",
        "kiwix_flavour": "",
    },
]

DEFAULT_CONFIG: dict[str, Any] = {
    "update_interval_weeks": 0,   # 0 = auto-updates disabled
    "last_update_check":    None, # unix ts
    "downloaded_resources": {},   # id -> {filename, downloaded_at, updated}
    "admin_password":       "ark", # change via /admin config panel
}

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ark")

# ---------- App ----------
app = Flask(__name__, static_folder="static", template_folder="templates")


def admin_required(f: Any) -> Any:
    """HTTP Basic Auth decorator for admin routes."""
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        auth = request.authorization
        cfg = load_config()
        password = cfg.get("admin_password") or "ark"
        if not auth or auth.password != password:
            return Response(
                "Admin login required.\n", 401,
                {"WWW-Authenticate": 'Basic realm="Ark Admin"'},
            )
        return f(*args, **kwargs)
    return decorated


# ======================================================================
#   Config persistence
# ======================================================================

_config_lock = threading.Lock()


def load_config() -> dict[str, Any]:
    with _config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    loaded = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update(loaded)
                # Ensure downloaded_resources is a dict even if corrupted.
                if not isinstance(merged.get("downloaded_resources"), dict):
                    merged["downloaded_resources"] = {}
                return merged
            except (OSError, json.JSONDecodeError) as e:
                log.warning("config.json unreadable (%s); using defaults.", e)
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy


def save_config(cfg: dict[str, Any]) -> None:
    with _config_lock:
        os.makedirs(ARK_DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


# ======================================================================
#   Storage / disk usage
# ======================================================================

def get_storage() -> dict[str, Any]:
    try:
        st = shutil.disk_usage(ARK_DATA_DIR)
    except FileNotFoundError:
        return {"available": False, "path": ARK_DATA_DIR,
                "total": 0, "used": 0, "free": 0}
    return {
        "available": True,
        "path":      ARK_DATA_DIR,
        "total":     st.total,
        "used":      st.used,
        "free":      st.free,
        "total_gb":  round(st.total / 1_000_000_000, 1),
        "used_gb":   round(st.used  / 1_000_000_000, 1),
        "free_gb":   round(st.free  / 1_000_000_000, 1),
        "used_pct":  round(st.used / st.total * 100, 1) if st.total else 0,
    }


# ======================================================================
#   Kiwix OPDS lookup + library management
# ======================================================================

def opds_find(kiwix_name: str, kiwix_flavour: str = "") -> dict[str, Any] | None:
    """Resolve a resource's current download URL + size via Kiwix OPDS.

    Kiwix OPDS entries use ``name`` (e.g. ``wikipedia_en_all``) and an
    optional ``flavour`` (e.g. ``maxi``, ``nopic``, ``mini``).  The
    acquisition link points to a ``.meta4`` metalink file; we strip that
    suffix to get the direct ``.zim`` download URL on the Kiwix CDN.
    """
    try:
        r = requests.get(
            OPDS_CATALOG,
            params={"name": kiwix_name, "count": "10"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("OPDS lookup failed for %s: %s", kiwix_name, e)
        return None

    soup = BeautifulSoup(r.text, "xml")

    for entry in soup.find_all("entry"):
        # Match the requested flavour (empty string matches entries with no flavour).
        entry_flavour_tag = entry.find("flavour")
        entry_flavour = entry_flavour_tag.get_text(strip=True) if entry_flavour_tag else ""
        if entry_flavour != kiwix_flavour:
            continue

        # Find the acquisition link (type includes "zim").
        link = None
        for candidate in entry.find_all("link"):
            rel = candidate.get("rel", "")
            typ = candidate.get("type", "")
            if "acquisition" in rel and "zim" in typ:
                link = candidate
                break
        if link is None:
            continue

        href = link.get("href", "")
        if not href:
            continue

        # Strip .meta4 suffix — the bare URL is the direct .zim download.
        if href.endswith(".meta4"):
            href = href[: -len(".meta4")]

        return {
            "url":      href,
            "size":     int(link.get("length") or 0),
            "updated":  (entry.find("updated").text if entry.find("updated") else None),
            "filename": os.path.basename(href.split("?", 1)[0]),
        }

    log.warning("OPDS: no entry matched name=%s flavour=%r", kiwix_name, kiwix_flavour)
    return None


def rebuild_library() -> None:
    """Rewrite library.xml to contain every .zim currently on the SSD.

    kiwix-serve is started with --monitorLibrary, so it reloads automatically
    as soon as we replace the file.
    """
    global _books_cache
    _books_cache = None  # Invalidate so next search re-discovers.
    if not os.path.isdir(ZIM_DIR):
        return
    try:
        # Start from an empty library; kiwix-manage add will populate it.
        with open(LIBRARY_XML, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<library version="20110515"></library>\n')
        for fn in sorted(os.listdir(ZIM_DIR)):
            if not fn.endswith(".zim"):
                continue
            full = os.path.join(ZIM_DIR, fn)
            try:
                subprocess.run(
                    [KIWIX_MANAGE, LIBRARY_XML, "add", full],
                    check=False, capture_output=True, timeout=30,
                )
            except (FileNotFoundError, subprocess.SubprocessError) as e:
                log.warning("kiwix-manage add failed for %s: %s", fn, e)
    except OSError as e:
        log.warning("Could not rebuild library.xml: %s", e)


# ======================================================================
#   Background download manager (chunked + resumable)
# ======================================================================

_dl_lock = threading.Lock()
_dl_state: dict[str, dict[str, Any]] = {}  # resource_id -> state dict
_stop_event = threading.Event()


def _set_dl(resource_id: str, **updates: Any) -> None:
    with _dl_lock:
        state = _dl_state.setdefault(resource_id, {})
        state.update(updates)


def _get_resource(resource_id: str) -> dict[str, Any] | None:
    return next((r for r in RESOURCE_CATALOG if r["id"] == resource_id), None)


def download_worker(resource_id: str, url: str, dest_path: str,
                    expected_size: int, updated: str | None = None) -> None:
    """Stream-download `url` to `dest_path` in 8 KiB chunks with resume."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    part_path = dest_path + ".part"

    resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    headers = {"User-Agent": "ProjectArk/1.0"}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    _set_dl(resource_id,
            status="downloading",
            downloaded=resume_from,
            total=expected_size or 0,
            filename=os.path.basename(dest_path),
            error=None,
            started_at=time.time())

    try:
        with requests.get(url, stream=True, headers=headers,
                          timeout=(10, 120)) as r:
            # 416 = Range Not Satisfiable → the .part file is already >= total.
            if r.status_code == 416:
                log.info("%s: server says range not satisfiable — assuming complete.",
                         resource_id)
                if os.path.exists(part_path):
                    os.replace(part_path, dest_path)
                _finalize_download(resource_id, dest_path, updated)
                return

            r.raise_for_status()

            # Resume granted? 206 = partial, 200 = full (server refused resume).
            resuming = (r.status_code == 206)
            if not resuming and resume_from > 0:
                log.info("%s: server refused resume, restarting from zero.",
                         resource_id)
                resume_from = 0

            content_len = int(r.headers.get("Content-Length") or 0)
            total = (resume_from + content_len) if content_len else expected_size
            _set_dl(resource_id, total=total, downloaded=resume_from)

            mode = "ab" if resuming else "wb"
            bytes_written = resume_from
            last_push = 0.0

            with open(part_path, mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if _stop_event.is_set():
                        _set_dl(resource_id, status="paused")
                        log.info("%s: download paused (shutdown).", resource_id)
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    bytes_written += len(chunk)
                    # Throttle state updates to ~1/sec to avoid lock churn.
                    now = time.time()
                    if now - last_push >= 0.5:
                        _set_dl(resource_id, downloaded=bytes_written)
                        last_push = now

        os.replace(part_path, dest_path)
        _finalize_download(resource_id, dest_path, updated)

    except requests.RequestException as e:
        log.warning("%s: download failed: %s", resource_id, e)
        _set_dl(resource_id, status="error", error=str(e))
    except OSError as e:
        log.warning("%s: disk error: %s", resource_id, e)
        _set_dl(resource_id, status="error", error=f"Disk: {e}")


def _finalize_download(resource_id: str, dest_path: str,
                       updated: str | None) -> None:
    """Move a completed .zim into the library, clean up old versions."""
    cfg = load_config()
    downloaded = cfg.setdefault("downloaded_resources", {})
    old = downloaded.get(resource_id, {})
    old_filename = old.get("filename")
    new_filename = os.path.basename(dest_path)

    # Remove any stale previous version of this resource.
    if old_filename and old_filename != new_filename:
        old_path = os.path.join(ZIM_DIR, old_filename)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
                log.info("%s: removed old version %s", resource_id, old_filename)
            except OSError as e:
                log.warning("Could not remove old %s: %s", old_filename, e)

    downloaded[resource_id] = {
        "filename":      new_filename,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "updated":       updated,
    }
    save_config(cfg)

    rebuild_library()

    _set_dl(resource_id,
            status="completed",
            downloaded=os.path.getsize(dest_path),
            total=os.path.getsize(dest_path),
            finished_at=time.time())
    log.info("%s: finalized → %s", resource_id, new_filename)


def start_download(resource_id: str) -> tuple[bool, str]:
    """Kick off a background download for `resource_id`. Returns (ok, msg)."""
    resource = _get_resource(resource_id)
    if not resource:
        return False, "Unknown resource."

    with _dl_lock:
        existing = _dl_state.get(resource_id, {})
        if existing.get("status") in ("downloading", "starting"):
            return True, "Already downloading."

    _set_dl(resource_id, status="starting", downloaded=0,
            total=0, error=None)

    def _runner() -> None:
        info = opds_find(resource["kiwix_name"], resource.get("kiwix_flavour", ""))
        if not info:
            _set_dl(resource_id, status="error",
                    error="Not found in Kiwix catalog (no internet?).")
            return
        dest = os.path.join(ZIM_DIR, info["filename"])
        download_worker(resource_id, info["url"], dest,
                        info["size"], info.get("updated"))

    threading.Thread(target=_runner, daemon=True).start()
    return True, "Download started."


# ======================================================================
#   Background auto-update scheduler
# ======================================================================

def check_for_updates() -> int:
    """Check every downloaded resource for a newer version via OPDS."""
    cfg = load_config()
    downloaded: dict[str, dict[str, Any]] = cfg.get("downloaded_resources", {})
    started = 0
    for rid, meta in list(downloaded.items()):
        resource = _get_resource(rid)
        if not resource:
            continue
        info = opds_find(resource["kiwix_name"], resource.get("kiwix_flavour", ""))
        if not info:
            continue
        if info["filename"] and info["filename"] != meta.get("filename"):
            log.info("%s: new version available (%s -> %s)",
                     rid, meta.get("filename"), info["filename"])
            ok, _ = start_download(rid)
            if ok:
                started += 1
    return started


def _scheduler_loop() -> None:
    log.info("Scheduler thread started.")
    # Small stagger so we don't hammer the catalog right at boot.
    for _ in range(30):
        if _stop_event.is_set():
            return
        time.sleep(1)

    while not _stop_event.is_set():
        try:
            cfg = load_config()
            weeks = int(cfg.get("update_interval_weeks") or 0)
            if weeks > 0:
                interval_s = weeks * 7 * 24 * 3600
                last = cfg.get("last_update_check") or 0
                if time.time() - float(last) >= interval_s:
                    log.info("Scheduler: running auto-update check.")
                    check_for_updates()
                    cfg["last_update_check"] = time.time()
                    save_config(cfg)
        except Exception:  # pragma: no cover — never let the thread die
            log.exception("Scheduler iteration failed.")

        # Wake up every minute to re-check settings (so a user change to
        # the interval takes effect without a restart).
        for _ in range(60):
            if _stop_event.is_set():
                return
            time.sleep(1)


def _shutdown_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
    log.info("Signal %s received — shutting down.", signum)
    _stop_event.set()
    # Re-raise with default handler so the process actually exits.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# ======================================================================
#   Session storage for multi-turn conversations
# ======================================================================

_session_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}  # session_id -> {history, ...}


def _get_session_id(req: Any) -> str:
    """Get or create a session ID from request IP (simplified)."""
    # In a real app, use proper session tokens. Here, use client IP.
    return req.remote_addr or "unknown"


def _get_session(session_id: str) -> dict[str, Any]:
    """Get session data, creating if needed."""
    with _session_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {"history": []}
        return _sessions[session_id]


def _clear_session(session_id: str) -> None:
    """Clear session history."""
    with _session_lock:
        if session_id in _sessions:
            _sessions[session_id] = {"history": []}


# ======================================================================
#   RAG pipeline
# ======================================================================

_books_cache: list[str] | None = None


def _discover_books() -> list[str]:
    """Discover all available Kiwix book names by querying the server."""
    global _books_cache
    if _books_cache is not None:
        return _books_cache

    books: list[str] = []
    try:
        # The Kiwix catalog endpoint lists all loaded books.
        r = requests.get(f"{KIWIX_BASE}/catalog/v2/entries?count=100",
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        for entry in soup.find_all("entry"):
            name_tag = entry.find("name")
            if name_tag:
                books.append(name_tag.get_text(strip=True))
    except Exception as e:
        log.warning("Catalog discovery failed: %s", e)

    if not books:
        # Fallback: parse the homepage for book links.
        try:
            r = requests.get(KIWIX_BASE, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            # Look for any /viewer#BOOK/ or /content/BOOK/ patterns.
            found = set(re.findall(r'(?:/viewer#|/content/)([^/"\'?]+)', r.text))
            books = list(found)
        except Exception as e:
            log.warning("Homepage book discovery failed: %s", e)

    if not books:
        # Last resort: derive from ZIM filenames on disk.
        if os.path.isdir(ZIM_DIR):
            for fn in os.listdir(ZIM_DIR):
                if fn.endswith(".zim"):
                    # e.g. "wikipedia_en_all_maxi_2024-01.zim" → "wikipedia_en_all_maxi_2024-01"
                    books.append(fn.rsplit(".", 1)[0])

    _books_cache = books
    log.info("Discovered Kiwix books: %s", books if books else "(none)")
    return books


def _expand_query(query: str) -> list[str]:
    """Generate multiple search queries from natural language.

    Returns a list of search strings to try against Kiwix, ordered from
    most specific to most general.  No LLM call — all instant.
    """
    lower = query.lower().strip()
    queries: list[str] = []

    # 1. Original query as-is (sometimes it just works).
    queries.append(query)

    # 2. Abbreviation expansion.
    for abbr, expansion in ABBREVIATIONS.items():
        if re.search(rf'\b{re.escape(abbr)}\b', lower):
            queries.append(re.sub(
                rf'\b{re.escape(abbr)}\b', expansion, lower, flags=re.IGNORECASE,
            ))

    # 3. Concept synonym injection — if the query contains a known phrase,
    #    add the Wikipedia-friendly search terms.
    for phrase, extra_terms in CONCEPT_SYNONYMS.items():
        if phrase in lower:
            for term in extra_terms:
                queries.append(term)

    # 4. Stop-word-stripped keywords (catches the long-tail).
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", lower)
    keywords = [w for w in words if w not in _STOP_WORDS and len(w) > 1]
    if keywords and len(keywords) < len(words):
        queries.append(" ".join(keywords))

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        q_key = q.strip().lower()
        if q_key and q_key not in seen:
            seen.add(q_key)
            unique.append(q.strip())
    return unique


def kiwix_search_articles(query: str, count: int = 5) -> list[dict[str, str]]:
    """Search across ALL Kiwix books using the /suggest JSON API (primary)
    and /search HTML fallback.  Expands abbreviations and deduplicates.
    """
    seen_urls: set[str] = set()
    candidates: list[dict[str, str]] = []
    books = _discover_books()

    for search_term in _expand_query(query):
        if len(candidates) >= count:
            break

        # --- Strategy 1: /suggest JSON API (per-book, reliable) ----------
        for book in books:
            if len(candidates) >= count:
                break
            try:
                r = requests.get(
                    f"{KIWIX_BASE}/suggest",
                    params={"term": search_term, "count": count, "content": book},
                    timeout=HTTP_TIMEOUT,
                )
                r.raise_for_status()
                data = r.json()
                log.info("[suggest] book=%s term='%s' → %d hits",
                         book, search_term, len(data))
                for hit in data:
                    path = hit.get("path") or hit.get("url") or ""
                    label = hit.get("label") or hit.get("title") or path
                    if not path:
                        continue
                    url = urljoin(KIWIX_BASE, f"/{book}/{path.lstrip('/')}")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    candidates.append({
                        "title": label, "url": url,
                        "book": book, "snippet": hit.get("snippet", ""),
                    })
            except Exception as e:
                log.debug("[suggest] book=%s term='%s' failed: %s",
                          book, search_term, e)

        # --- Strategy 2: /search HTML fallback (cross-book) ------
        if len(candidates) >= count:
            break
        try:
            r = requests.get(
                f"{KIWIX_BASE}/search",
                params={"pattern": search_term, "pageLength": count},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Try every <a> that links to an article (flexible selectors).
            links_found = 0
            for link in soup.find_all("a", href=True):
                href = link["href"]
                # Accept /content/BOOK/..., /BOOK/..., /viewer#BOOK/...
                if "/content/" not in href and not any(
                    f"/{b}/" in href for b in books
                ):
                    continue
                url = urljoin(KIWIX_BASE, href)
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title = link.get_text(strip=True) or href.split("/")[-1]
                # Try to find a sibling <cite> for snippet text.
                parent = link.parent
                cite = parent.find("cite") if parent else None
                snippet = cite.get_text(strip=True) if cite else ""

                book = "unknown"
                parts = href.split("/")
                if len(parts) >= 3:
                    book = parts[2] if "/content/" in href else parts[1]

                candidates.append({
                    "title": title, "url": url,
                    "book": book, "snippet": snippet,
                })
                links_found += 1
                if len(candidates) >= count:
                    break

            log.info("[search] term='%s' → %d links found in HTML",
                     search_term, links_found)

        except requests.RequestException as e:
            log.warning("[search] term='%s' failed: %s", search_term, e)

    log.info("Total search candidates for '%s': %d", query, len(candidates))
    return candidates[:count]


def fetch_and_clean_article(url: str) -> str:
    """Fetch a Kiwix article and extract readable text.

    Tries the original URL first.  Falls back less aggressively than before
    — keeps <li>, <dd>, <section>, <h2>/<h3> headings, etc.
    """
    log.info("[fetch] %s", url)
    r = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    raw_len = len(r.text)
    soup = BeautifulSoup(r.text, "html.parser")

    # Remove only truly useless elements.
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Try to find the main content area.
    main = (
        soup.find(id="mw-content-text")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("article")
        or soup.body
        or soup
    )

    # Extract text from paragraphs, list items, headings, and <dd> definitions.
    text_parts: list[str] = []
    for el in main.find_all(["p", "li", "dd", "h2", "h3", "h4", "blockquote",
                             "figcaption", "section", "div"]):
        t = el.get_text(" ", strip=True)
        if t and len(t) > 15:  # skip tiny fragments
            text_parts.append(t)

    # If paragraph extraction failed, just get ALL text from main.
    if not text_parts:
        text_parts = [main.get_text(" ", strip=True)]

    text = " ".join(text_parts)
    text = re.sub(r"\[\d+\]", "", text)       # strip footnote markers
    text = re.sub(r"\s+", " ", text).strip()

    words = text.split()
    if len(words) > MAX_CONTEXT_WORDS:
        text = " ".join(words[:MAX_CONTEXT_WORDS]) + "…"

    log.info("[fetch] raw=%d bytes → cleaned=%d chars (%d words)",
             raw_len, len(text), len(words))
    return text


def _llm_suggest_search(query: str) -> str | None:
    """Ask the LLM for a Wikipedia article title to search.

    ONLY used as a last resort when all instant search strategies fail.
    Constrained to very short output to minimize latency.
    """
    payload = {
        "model":       LLM_MODEL,
        "messages":    [
            {"role": "system", "content": (
                "You help find Wikipedia articles. Given the user's question, "
                "respond with ONLY the most likely Wikipedia article title that "
                "would answer it. One title, nothing else. Examples:\n"
                "Q: 'my kid swallowed bleach' → 'Poisoning'\n"
                "Q: 'how to CPR' → 'Cardiopulmonary resuscitation'\n"
                "Q: 'fix iphone screen' → 'IPhone screen replacement'"
            )},
            {"role": "user", "content": query},
        ],
        "temperature": 0.0,
        "max_tokens":  30,
    }
    r = requests.post(f"{LLM_BASE}/v1/chat/completions",
                      json=payload, timeout=(5, 120))
    r.raise_for_status()
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    title = choice.get("message", {}).get("content", "").strip().strip("'\"")
    return title if title else None


def ask_llm(context: str, history: list[dict[str, str]]) -> str:
    """Send a multi-turn conversation to llama.cpp's OpenAI-compatible endpoint.

    ``history`` is a list of ``{"role": "user"|"assistant", "content": "..."}``
    messages.  The system prompt and RAG context are prepended automatically.
    """
    system_msg = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Wikipedia context:\n\"\"\"\n{context}\n\"\"\""
    )
    messages = [{"role": "system", "content": system_msg}] + history

    if VERBOSE:
        log.info("LLM request: model=%s, messages=%d", LLM_MODEL, len(messages))

    payload = {
        "model":       LLM_MODEL,
        "messages":    messages,
        "temperature": 0.2,
        "top_p":       0.9,
    }
    r = requests.post(f"{LLM_BASE}/v1/chat/completions",
                      json=payload, timeout=(5, 300))
    r.raise_for_status()
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    content = choice.get("message", {}).get("content", "").strip()

    if VERBOSE:
        log.info("LLM response: %d chars", len(content))

    return content


# ======================================================================
#   Routes — portal
# ======================================================================

@app.route("/")
def index():
    return render_template("index.html", model=LLM_MODEL, verbose=VERBOSE)


@app.route("/generate_204")
@app.route("/gen_204")
@app.route("/hotspot-detect.html")
@app.route("/library/test/success.html")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
def captive_probe():
    return render_template("index.html", model=LLM_MODEL, verbose=VERBOSE), 200


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    history: list[dict[str, str]] = payload.get("history") or []
    # Frontend can send the last source URL so follow-ups reuse it.
    last_source: str | None = payload.get("last_source")
    # User can specify a Kiwix article URL directly (from "browse archives").
    user_source: str | None = payload.get("source_url")

    if not query:
        return jsonify({"ok": False, "error": "Empty query."}), 400
    if len(query) > 500:
        return jsonify({"ok": False, "error": "Query too long (max 500 chars)."}), 400

    def generate():
        trimmed_history = history[-10:]
        log.info("=== Query: '%s' (history: %d turns, last_source: %s, user_source: %s) ===",
                 query, len(trimmed_history), last_source, user_source)

        context = None
        source_url = None

        # ------------------------------------------------------------------
        # Priority 1: User-specified source URL (from browse archives link).
        # ------------------------------------------------------------------
        if user_source:
            yield json.dumps({"type": "status", "message": "Loading specified article..."}) + "\n"
            try:
                context = fetch_and_clean_article(user_source)
                source_url = user_source
                log.info("Using user-specified source: %s", user_source)
            except Exception as e:
                log.warning("Could not fetch user-specified source: %s", e)

        # ------------------------------------------------------------------
        # Priority 2: Follow-up reuse — same article, new question.
        # ------------------------------------------------------------------
        if context is None and trimmed_history and last_source:
            yield json.dumps({"type": "status", "message": "Using previous article for follow-up..."}) + "\n"
            try:
                context = fetch_and_clean_article(last_source)
                source_url = last_source
                log.info("Follow-up: reusing source %s", last_source)
            except Exception as e:
                log.warning("Could not re-fetch last source: %s", e)

        # ------------------------------------------------------------------
        # Fresh search: search ALL Kiwix books (Wikipedia, iFixit, WikiMed…)
        # _expand_query handles abbreviations, concept synonyms, stop-word
        # stripping — all instant, no LLM needed.
        # ------------------------------------------------------------------
        if context is None:
            yield json.dumps({"type": "status", "message": "Searching offline archives..."}) + "\n"
            candidates = kiwix_search_articles(query, count=5)

            # Last resort: if the smart search found nothing, ask the LLM
            # to suggest a Wikipedia article title.  This is slow but beats
            # returning nothing in a survival situation.
            if not candidates:
                yield json.dumps({"type": "status", "message": "Trying alternate search..."}) + "\n"
                try:
                    llm_terms = _llm_suggest_search(query)
                    if llm_terms:
                        log.info("LLM suggested search: %s", llm_terms)
                        candidates = kiwix_search_articles(llm_terms, count=5)
                except Exception as e:
                    log.warning("LLM search suggestion failed: %s", e)

            if not candidates:
                yield json.dumps({
                    "ok": False, "type": "result",
                    "error": (
                        "No articles found in any archive. "
                        "You can browse the archives directly using the link above."
                    ),
                }) + "\n"
                return

            # Try candidates in order.  Pick the first with real content.
            for cand in candidates:
                yield json.dumps({
                    "type": "status",
                    "message": f"Reading: {cand['title']}…",
                }) + "\n"
                try:
                    text = fetch_and_clean_article(cand["url"])
                except Exception as e:
                    log.warning("Article fetch failed (%s): %s", cand["title"], e)
                    continue
                if not text or len(text) < 80:
                    log.info("Article too short, skipping: %s", cand["title"])
                    continue
                context = text
                source_url = cand["url"]
                log.info("Using article: %s (from %s)", cand["title"], cand["book"])
                break

        if context is None:
            yield json.dumps({
                "ok": False, "type": "result",
                "error": "Found articles but none had usable content.",
            }) + "\n"
            return

        # ------------------------------------------------------------------
        # LLM inference — single call with the chosen article context.
        # ------------------------------------------------------------------
        yield json.dumps({"type": "status", "message": "Generating answer..."}) + "\n"

        messages = list(trimmed_history) + [{"role": "user", "content": query}]
        try:
            answer = ask_llm(context, messages)
        except requests.RequestException as e:
            log.warning("LLM call failed: %s", e)
            yield json.dumps({
                "ok": False, "type": "result",
                "error": "Local LLM is not responding. Is llama.cpp running?",
            }) + "\n"
            return

        # If the LLM rejected this article, try the NEXT candidate (one retry only).
        if "IRRELEVANT_ARTICLE" in answer and not last_source:
            log.info("LLM rejected article, trying next candidate...")
            # We already consumed some candidates above; search again for fallback.
            fallback_candidates = kiwix_search_articles(query, count=5)
            for cand in fallback_candidates:
                if cand["url"] == source_url:
                    continue  # skip the one we already tried
                yield json.dumps({
                    "type": "status",
                    "message": f"Trying: {cand['title']}…",
                }) + "\n"
                try:
                    alt_context = fetch_and_clean_article(cand["url"])
                    if not alt_context or len(alt_context) < 80:
                        continue
                    alt_answer = ask_llm(alt_context, messages)
                    if "IRRELEVANT_ARTICLE" not in alt_answer:
                        answer = alt_answer
                        source_url = cand["url"]
                        context = alt_context
                        break
                except Exception:
                    continue

        # Still irrelevant after retry?
        if "IRRELEVANT_ARTICLE" in answer:
            answer = (
                "I couldn't find an article that directly answers your question "
                "in the offline archives. You can try:\n"
                "- Asking in a slightly different way\n"
                "- Browsing the archives directly using the link at the top\n"
                "- Checking if the right content pack is installed (admin panel)"
            )
            source_url = None

        if not answer:
            yield json.dumps({
                "ok": False, "type": "result",
                "error": "LLM returned an empty answer.",
            }) + "\n"
            return

        yield json.dumps({
            "ok": True,
            "type": "result",
            "answer": answer,
            "source": source_url,
            "model": LLM_MODEL,
        }) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/api/search", methods=["POST"])
def api_search():
    """Search Kiwix and return top 5 results for manual article selection."""
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query or len(query) > 500:
        return jsonify({"ok": False, "error": "Invalid query."}), 400

    book = get_book_name()
    if not book:
        return jsonify({"ok": False, "error": "No Kiwix content available."}), 502

    results = []
    try:
        r = requests.get(
            f"{KIWIX_BASE}/suggest",
            params={"content": book, "term": query, "count": 5},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        for i, hit in enumerate(data, 1):
            path = hit.get("path") or hit.get("url")
            if path:
                url = urljoin(KIWIX_BASE, f"/content/{book}/{path.lstrip('/')}")
                # Try to fetch snippet
                snippet = ""
                try:
                    art = requests.get(url, timeout=(5, 10))
                    art.raise_for_status()
                    soup = BeautifulSoup(art.text, "html.parser")
                    main = soup.find("main") or soup.find(id="mw-content-text") or soup.body
                    if main:
                        paragraphs = [p.get_text(" ", strip=True) for p in main.find_all("p")]
                        snippet = " ".join(paragraphs[:2])[:200]
                except Exception:
                    pass
                results.append({
                    "id": i - 1,
                    "title": hit.get("label", "Unknown"),
                    "url": url,
                    "snippet": snippet,
                })
    except requests.RequestException as e:
        log.warning("Article search failed: %s", e)
        return jsonify({"ok": False, "error": "Search failed."}), 502

    if VERBOSE:
        log.info("Search returned %d results for: %s", len(results), query)

    return jsonify({"ok": True, "results": results[:5]})


@app.route("/api/clear-history", methods=["POST"])
def api_clear_history():
    """Clear the backend conversation history for this session."""
    session_id = _get_session_id(request)
    _clear_session(session_id)
    if VERBOSE:
        log.info("Cleared history for session: %s", session_id)
    return jsonify({"ok": True, "message": "History cleared."})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "model": LLM_MODEL,
                    "kiwix": KIWIX_BASE, "data_dir": ARK_DATA_DIR})


@app.route("/api/debug-search", methods=["POST"])
def api_debug_search():
    """Debug endpoint: shows exactly what the search pipeline returns.

    POST {"query": "how to start a fire"}
    Returns: expanded queries, raw candidates, article previews.
    """
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Empty query."}), 400

    books = _discover_books()
    expanded = _expand_query(query)
    candidates = kiwix_search_articles(query, count=5)

    # Try fetching the first candidate's content to show what cleaning does.
    article_preview = None
    if candidates:
        try:
            text = fetch_and_clean_article(candidates[0]["url"])
            article_preview = {
                "url": candidates[0]["url"],
                "title": candidates[0]["title"],
                "cleaned_length": len(text),
                "first_500_chars": text[:500],
            }
        except Exception as e:
            article_preview = {"error": str(e)}

    return jsonify({
        "ok": True,
        "query": query,
        "books_discovered": books,
        "expanded_queries": expanded,
        "candidates": candidates,
        "article_preview": article_preview,
    })


# ======================================================================
#   Routes — admin
# ======================================================================

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html", model=LLM_MODEL,
                           data_dir=ARK_DATA_DIR)


@app.route("/api/storage")
@admin_required
def api_storage():
    return jsonify(get_storage())


@app.route("/api/resources")
@admin_required
def api_resources():
    storage = get_storage()
    cfg = load_config()
    downloaded = cfg.get("downloaded_resources", {})
    with _dl_lock:
        dl_snapshot = {k: dict(v) for k, v in _dl_state.items()}

    out = []
    for r in RESOURCE_CATALOG:
        approx_bytes = int(r["approx_size_gb"] * 1_000_000_000)
        is_downloaded = r["id"] in downloaded
        fits = (storage["free"] > approx_bytes) if storage["available"] else False
        out.append({
            "id":                 r["id"],
            "name":                r["name"],
            "description":         r["description"],
            "category":            r["category"],
            "approx_size_gb":      r["approx_size_gb"],
            "approx_size_bytes":   approx_bytes,
            "kiwix_name":          r["kiwix_name"],
            "fits_on_disk":        fits or is_downloaded,
            "downloaded":          is_downloaded,
            "downloaded_meta":     downloaded.get(r["id"]),
            "download_state":      dl_snapshot.get(r["id"]),
        })
    return jsonify({
        "storage":   storage,
        "resources": out,
    })


@app.route("/api/download", methods=["POST"])
@admin_required
def api_download():
    body = request.get_json(silent=True) or {}
    rid = (body.get("id") or "").strip()
    if not rid:
        return jsonify({"ok": False, "error": "Missing resource id."}), 400

    resource = _get_resource(rid)
    if not resource:
        return jsonify({"ok": False, "error": "Unknown resource."}), 404

    # Refuse if obviously too big for the SSD.
    storage = get_storage()
    approx = int(resource["approx_size_gb"] * 1_000_000_000)
    if storage["available"] and storage["free"] < approx:
        return jsonify({"ok": False,
                        "error": "Not enough free space on the SSD."}), 400

    ok, msg = start_download(rid)
    status = 200 if ok else 409
    return jsonify({"ok": ok, "message": msg}), status


@app.route("/api/downloads")
@admin_required
def api_downloads():
    with _dl_lock:
        return jsonify({k: dict(v) for k, v in _dl_state.items()})


@app.route("/api/config", methods=["GET", "POST"])
@admin_required
def api_config():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        cfg = load_config()
        if "update_interval_weeks" in body:
            try:
                weeks = int(body["update_interval_weeks"])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Invalid interval."}), 400
            if weeks < 0 or weeks > 104:
                return jsonify({"ok": False,
                                "error": "Interval must be between 0 and 104 weeks."}), 400
            cfg["update_interval_weeks"] = weeks
        save_config(cfg)
        return jsonify({"ok": True, "config": cfg})

    return jsonify(load_config())


@app.route("/api/check-updates", methods=["POST"])
@admin_required
def api_check_updates():
    """Manual trigger for the auto-update check."""
    def _runner() -> None:
        try:
            started = check_for_updates()
            log.info("Manual update check started %d downloads.", started)
            cfg = load_config()
            cfg["last_update_check"] = time.time()
            save_config(cfg)
        except Exception:
            log.exception("Manual update check failed.")
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True, "message": "Update check started."})


@app.route("/api/password", methods=["POST"])
@admin_required
def api_password():
    """Change the admin password."""
    body = request.get_json(silent=True) or {}
    new_pw = (body.get("password") or "").strip()
    if not new_pw or len(new_pw) < 3:
        return jsonify({"ok": False, "error": "Password must be at least 3 characters."}), 400
    if len(new_pw) > 128:
        return jsonify({"ok": False, "error": "Password too long."}), 400
    cfg = load_config()
    cfg["admin_password"] = new_pw
    save_config(cfg)
    return jsonify({"ok": True, "message": "Password updated."})


# ======================================================================
#   Boot
# ======================================================================

def _boot() -> None:
    os.makedirs(ZIM_DIR, exist_ok=True)
    # Make sure library.xml is in sync with what's actually on disk at startup.
    try:
        rebuild_library()
    except Exception:
        log.exception("Initial library rebuild failed.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown_handler)
        except (ValueError, OSError):
            pass  # not main thread (rare in dev reload)

    threading.Thread(target=_scheduler_loop, daemon=True).start()


_boot()


if __name__ == "__main__":
    port = int(os.environ.get("ARK_PORT", "80"))
    app.run(host="0.0.0.0", port=port, debug=False)
