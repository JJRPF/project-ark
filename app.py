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
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

# ---------- Config ----------
KIWIX_BASE   = os.environ.get("ARK_KIWIX_URL", "http://127.0.0.1:8080")
OLLAMA_BASE  = os.environ.get("ARK_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("ARK_OLLAMA_MODEL", "gemma4:4b")
ARK_DATA_DIR = os.environ.get("ARK_DATA_DIR", "/mnt/ssd-ark/ark-data")

ZIM_DIR      = os.path.join(ARK_DATA_DIR, "zims")
LIBRARY_XML  = os.path.join(ARK_DATA_DIR, "library.xml")
CONFIG_PATH  = os.path.join(ARK_DATA_DIR, "config.json")
KIWIX_MANAGE = "/opt/kiwix/kiwix-manage"

OPDS_CATALOG = "https://library.kiwix.org/catalog/v2/entries"
CHUNK_SIZE   = 8192
HTTP_TIMEOUT = (5, 60)

MAX_CONTEXT_WORDS = 1500

SYSTEM_PROMPT = (
    "You are an emergency offline survival assistant. "
    "Answer the user's query using ONLY the provided Wikipedia context. "
    "Be highly concise, format with clear bullet points, and prioritize "
    "actionable steps. If the context does not contain the answer, say so "
    "plainly — do not invent facts."
)

# Curated offline content catalog. `kiwix_name` is the OPDS `name` field
# used to resolve the current latest filename + download URL at runtime.
RESOURCE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "wikipedia_maxi",
        "name": "Wikipedia (English, Full)",
        "description": "Complete English Wikipedia with images. The big one.",
        "category": "Reference",
        "approx_size_gb": 102.0,
        "kiwix_name": "wikipedia_en_all_maxi",
    },
    {
        "id": "wikimed",
        "name": "WikiMed Medicine",
        "description": "All medical articles from Wikipedia. Critical for triage.",
        "category": "Medical",
        "approx_size_gb": 4.2,
        "kiwix_name": "wikipedia_en_medicine_maxi",
    },
    {
        "id": "ifixit",
        "name": "iFixit Repair Guides",
        "description": "Full iFixit repair library — electronics, appliances, tools.",
        "category": "Skills",
        "approx_size_gb": 3.6,
        "kiwix_name": "ifixit_en_all",
    },
    {
        "id": "wikihow",
        "name": "WikiHow",
        "description": "Practical, step-by-step how-to guides on everyday tasks.",
        "category": "Skills",
        "approx_size_gb": 12.3,
        "kiwix_name": "wikihow_en_maxi",
    },
    {
        "id": "gutenberg",
        "name": "Project Gutenberg",
        "description": "~70,000 public-domain books. Literature, manuals, reference.",
        "category": "Library",
        "approx_size_gb": 72.0,
        "kiwix_name": "gutenberg_en_all",
    },
]

DEFAULT_CONFIG: dict[str, Any] = {
    "update_interval_weeks": 0,   # 0 = auto-updates disabled
    "last_update_check":    None, # unix ts
    "downloaded_resources": {},   # id -> {filename, downloaded_at, updated}
}

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ark")

# ---------- App ----------
app = Flask(__name__, static_folder="static", template_folder="templates")


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

def opds_find(kiwix_name: str) -> dict[str, Any] | None:
    """Resolve a resource's current download URL + size via Kiwix OPDS."""
    try:
        r = requests.get(
            OPDS_CATALOG,
            params={"name": kiwix_name, "count": "1"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("OPDS lookup failed for %s: %s", kiwix_name, e)
        return None

    soup = BeautifulSoup(r.text, "xml")
    entry = soup.find("entry")
    if not entry:
        return None

    # Kiwix exposes a direct .zim download as rel="...acquisition/open-access"
    # with type="application/x-zim".
    link = None
    for candidate in entry.find_all("link"):
        rel = candidate.get("rel", "")
        typ = candidate.get("type", "")
        if "acquisition" in rel and "zim" in typ:
            link = candidate
            break
    if link is None:
        return None

    href = link.get("href")
    if not href:
        return None

    return {
        "url":      href,
        "size":     int(link.get("length") or 0),
        "updated":  (entry.find("updated").text if entry.find("updated") else None),
        "filename": os.path.basename(href.split("?", 1)[0]),
    }


def rebuild_library() -> None:
    """Rewrite library.xml to contain every .zim currently on the SSD.

    kiwix-serve is started with --monitorLibrary, so it reloads automatically
    as soon as we replace the file.
    """
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
        info = opds_find(resource["kiwix_name"])
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
        info = opds_find(resource["kiwix_name"])
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
    log.info("Signal %s received — asking workers to stop.", signum)
    _stop_event.set()


# ======================================================================
#   RAG pipeline (unchanged behavior, still queries whatever Kiwix serves)
# ======================================================================

_BOOK_NAME_CACHE: str | None = None


def _find_book_name() -> str | None:
    try:
        r = requests.get(f"{KIWIX_BASE}/search?pattern=index&pageLength=1",
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        m = re.search(r'/viewer#([^/"\']+)/', r.text)
        if m:
            return m.group(1)
        m = re.search(r'/content/([^/"\']+)/', r.text)
        if m:
            return m.group(1)
    except requests.RequestException as e:
        log.warning("Could not auto-detect Kiwix book: %s", e)
    return None


def get_book_name() -> str | None:
    global _BOOK_NAME_CACHE
    if _BOOK_NAME_CACHE is None:
        _BOOK_NAME_CACHE = _find_book_name()
        if _BOOK_NAME_CACHE:
            log.info("Using Kiwix book: %s", _BOOK_NAME_CACHE)
    return _BOOK_NAME_CACHE


def kiwix_top_article_url(query: str) -> str | None:
    book = get_book_name()
    if not book:
        return None

    try:
        r = requests.get(
            f"{KIWIX_BASE}/suggest",
            params={"content": book, "term": query, "count": 5},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        for hit in data:
            path = hit.get("path") or hit.get("url")
            if path:
                return urljoin(KIWIX_BASE, f"/content/{book}/{path.lstrip('/')}")
    except (requests.RequestException, ValueError) as e:
        log.info("Kiwix /suggest failed, falling back to /search: %s", e)

    try:
        r = requests.get(
            f"{KIWIX_BASE}/search",
            params={"books.name": book, "pattern": query, "pageLength": 1},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.select_one("a[href*='/content/'], a[href*='/viewer']")
        if link and link.get("href"):
            return urljoin(KIWIX_BASE, link["href"])
    except requests.RequestException as e:
        log.warning("Kiwix /search failed: %s", e)

    return None


def fetch_and_clean_article(url: str) -> str:
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "table", "figure", "sup",
                     "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    main = soup.find("main") or soup.find(id="mw-content-text") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in main.find_all("p")]
    text = " ".join(p for p in paragraphs if p)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\[\d+\]", "", text)
    words = text.split()
    if len(words) > MAX_CONTEXT_WORDS:
        text = " ".join(words[:MAX_CONTEXT_WORDS]) + "…"
    return text


def ask_ollama(question: str, context: str) -> str:
    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "system": SYSTEM_PROMPT,
        "prompt": (
            f"Wikipedia context:\n\"\"\"\n{context}\n\"\"\"\n\n"
            f"User question: {question}\n\nAnswer:"
        ),
        "options": {"temperature": 0.2, "num_ctx": 4096},
    }
    r = requests.post(f"{OLLAMA_BASE}/api/generate",
                      json=payload, timeout=(5, 300))
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()


# ======================================================================
#   Routes — portal
# ======================================================================

@app.route("/")
def index():
    return render_template("index.html", model=OLLAMA_MODEL)


@app.route("/generate_204")
@app.route("/gen_204")
@app.route("/hotspot-detect.html")
@app.route("/library/test/success.html")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
def captive_probe():
    return render_template("index.html", model=OLLAMA_MODEL), 200


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Empty query."}), 400
    if len(query) > 500:
        return jsonify({"ok": False, "error": "Query too long (max 500 chars)."}), 400

    log.info("Query: %s", query)

    try:
        article_url = kiwix_top_article_url(query)
    except Exception as e:  # pragma: no cover — defensive
        log.exception("Kiwix lookup crashed: %s", e)
        return jsonify({"ok": False,
                        "error": "Kiwix lookup failed. Is ark-kiwix running?"}), 502

    if not article_url:
        return jsonify({"ok": False,
                        "error": "No matching Wikipedia article found."}), 404

    try:
        context = fetch_and_clean_article(article_url)
    except requests.RequestException as e:
        log.warning("Article fetch failed: %s", e)
        return jsonify({"ok": False,
                        "error": "Could not fetch the article from Kiwix."}), 502

    if not context:
        return jsonify({"ok": False,
                        "error": "Found an article but it had no readable text."}), 502

    try:
        answer = ask_ollama(query, context)
    except requests.RequestException as e:
        log.warning("Ollama call failed: %s", e)
        return jsonify({"ok": False,
                        "error": "Local LLM is not responding. Is ollama running?"}), 502

    if not answer:
        return jsonify({"ok": False, "error": "LLM returned an empty answer."}), 502

    return jsonify({
        "ok": True,
        "answer": answer,
        "source": article_url,
        "model": OLLAMA_MODEL,
        "context_preview": html.escape(context[:280]) + ("…" if len(context) > 280 else ""),
    })


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "model": OLLAMA_MODEL,
                    "kiwix": KIWIX_BASE, "data_dir": ARK_DATA_DIR})


# ======================================================================
#   Routes — admin
# ======================================================================

@app.route("/admin")
def admin():
    return render_template("admin.html", model=OLLAMA_MODEL,
                           data_dir=ARK_DATA_DIR)


@app.route("/api/storage")
def api_storage():
    return jsonify(get_storage())


@app.route("/api/resources")
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
def api_downloads():
    with _dl_lock:
        return jsonify({k: dict(v) for k, v in _dl_state.items()})


@app.route("/api/config", methods=["GET", "POST"])
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
