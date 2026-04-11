"""
Project Ark — Flask backend
---------------------------
Offline RAG pipeline:
    client  ->  Flask (this file)  ->  Kiwix-serve (Wikipedia)
                                   ->  Ollama (local LLM)
                                   ->  client

Runs on port 80 so the router's captive portal can redirect directly to it.
The venv python is granted CAP_NET_BIND_SERVICE by install.sh.
"""

import html
import logging
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

# ---------- Config ----------
KIWIX_BASE   = os.environ.get("ARK_KIWIX_URL", "http://127.0.0.1:8080")
OLLAMA_BASE  = os.environ.get("ARK_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("ARK_OLLAMA_MODEL", "gemma4:4b")
MAX_CONTEXT_WORDS = 1500
HTTP_TIMEOUT = (5, 60)  # (connect, read)

SYSTEM_PROMPT = (
    "You are an emergency offline survival assistant. "
    "Answer the user's query using ONLY the provided Wikipedia context. "
    "Be highly concise, format with clear bullet points, and prioritize "
    "actionable steps. If the context does not contain the answer, say so "
    "plainly — do not invent facts."
)

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ark")

# ---------- App ----------
app = Flask(__name__, static_folder="static", template_folder="templates")


# ---------- Helpers ----------
def _find_book_name() -> str | None:
    """Discover the first available Kiwix book (ZIM) so we can query it."""
    try:
        r = requests.get(f"{KIWIX_BASE}/search?pattern=index&pageLength=1",
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        # Kiwix landing page links look like /viewer#<book_name>/A/...
        m = re.search(r'/viewer#([^/"\']+)/', r.text)
        if m:
            return m.group(1)
        m = re.search(r'/content/([^/"\']+)/', r.text)
        if m:
            return m.group(1)
    except requests.RequestException as e:
        log.warning("Could not auto-detect Kiwix book: %s", e)
    return None


_BOOK_NAME_CACHE: str | None = None


def get_book_name() -> str | None:
    global _BOOK_NAME_CACHE
    if _BOOK_NAME_CACHE is None:
        _BOOK_NAME_CACHE = _find_book_name()
        if _BOOK_NAME_CACHE:
            log.info("Using Kiwix book: %s", _BOOK_NAME_CACHE)
    return _BOOK_NAME_CACHE


def kiwix_top_article_url(query: str) -> str | None:
    """Hit the Kiwix suggest/search API and return the best article URL."""
    book = get_book_name()
    if not book:
        return None

    # /suggest is fast and returns the top hits.
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

    # Fallback to full search.
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
    """Fetch a Kiwix article and return up to MAX_CONTEXT_WORDS of clean text."""
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Strip elements that are never useful for RAG.
    for tag in soup(["script", "style", "table", "figure", "sup",
                     "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # Prefer the main content region Kiwix ships.
    main = soup.find("main") or soup.find(id="mw-content-text") or soup.body or soup

    paragraphs = [p.get_text(" ", strip=True) for p in main.find_all("p")]
    text = " ".join(p for p in paragraphs if p)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\[\d+\]", "", text)  # drop [1] [23] citation markers

    words = text.split()
    if len(words) > MAX_CONTEXT_WORDS:
        text = " ".join(words[:MAX_CONTEXT_WORDS]) + "…"
    return text


def ask_ollama(question: str, context: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "system": SYSTEM_PROMPT,
        "prompt": (
            f"Wikipedia context:\n\"\"\"\n{context}\n\"\"\"\n\n"
            f"User question: {question}\n\n"
            f"Answer:"
        ),
        "options": {
            "temperature": 0.2,
            "num_ctx": 4096,
        },
    }
    r = requests.post(f"{OLLAMA_BASE}/api/generate",
                      json=payload, timeout=(5, 300))
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()


# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html", model=OLLAMA_MODEL)


# Captive portal probes — always return 200 + our page so the phone
# shows the "Sign in to Wi-Fi" notification.
@app.route("/generate_204")              # Android
@app.route("/gen_204")                   # Android
@app.route("/hotspot-detect.html")       # iOS / macOS
@app.route("/library/test/success.html") # iOS fallback
@app.route("/ncsi.txt")                  # Windows
@app.route("/connecttest.txt")           # Windows
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

    # 1) Kiwix — find the best Wikipedia article.
    try:
        article_url = kiwix_top_article_url(query)
    except Exception as e:  # pragma: no cover — defensive
        log.exception("Kiwix lookup crashed: %s", e)
        return jsonify({"ok": False,
                        "error": "Kiwix lookup failed. Is ark-kiwix running?"}), 502

    if not article_url:
        return jsonify({"ok": False,
                        "error": "No matching Wikipedia article found."}), 404

    # 2) Fetch + clean.
    try:
        context = fetch_and_clean_article(article_url)
    except requests.RequestException as e:
        log.warning("Article fetch failed: %s", e)
        return jsonify({"ok": False,
                        "error": "Could not fetch the article from Kiwix."}), 502

    if not context:
        return jsonify({"ok": False,
                        "error": "Found an article but it had no readable text."}), 502

    # 3) LLM.
    try:
        answer = ask_ollama(query, context)
    except requests.RequestException as e:
        log.warning("Ollama call failed: %s", e)
        return jsonify({"ok": False,
                        "error": "Local LLM is not responding. Is ollama running?"}), 502

    if not answer:
        return jsonify({"ok": False,
                        "error": "LLM returned an empty answer."}), 502

    return jsonify({
        "ok": True,
        "answer": answer,
        "source": article_url,
        "model": OLLAMA_MODEL,
        "context_preview": html.escape(context[:280]) + ("…" if len(context) > 280 else ""),
    })


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "model": OLLAMA_MODEL, "kiwix": KIWIX_BASE})


if __name__ == "__main__":
    # Direct invocation is mainly for development. In production, systemd
    # (ark-flask.service) runs this under the venv python with CAP_NET_BIND_SERVICE.
    port = int(os.environ.get("ARK_PORT", "80"))
    app.run(host="0.0.0.0", port=port, debug=False)
