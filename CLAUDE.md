# Project Ark — Claude Code Context

## What This Is

Fully offline, battery-powered emergency knowledge node on a Raspberry Pi 5 (8 GB).
Pairs Kiwix (offline Wikipedia/iFixit/WikiMed) with a local LLM (llama.cpp) to answer
survival, medical, and practical questions via a captive Wi-Fi portal.

## Architecture

- **Flask** (port 80) — chat UI + admin dashboard + RAG pipeline
- **llama.cpp** (port 8001) — local LLM, OpenAI-compatible `/v1/chat/completions`
- **Kiwix-serve** (port 8080) — serves .zim files, search via `/suggest` (JSON) and `/search` (HTML)
- **Asus router** — captive portal redirects all clients to the Pi
- **Storage** — external SSD or boot SD card at `${ARK_DATA_DIR}/`

## Key Files

- `app.py` — Flask backend: RAG pipeline, admin API, download manager
- `install.sh` — Interactive installer (OS deps, llama.cpp, Kiwix, systemd)
- `templates/index.html` — Chat UI (NDJSON streaming, multi-turn, follow-up reuse)
- `templates/admin.html` — Content management dashboard
- `static/style.css` — Dark-mode, mobile-first, zero-dependency CSS
- `ark-flask.service` — systemd unit for Flask (port 80 via AmbientCapabilities)
- `ark-llama-cpp.service` — systemd unit for llama.cpp server
- `ark-kiwix.service` — systemd unit for Kiwix-serve (--monitorLibrary)

## RAG Pipeline Flow

1. User query → `_expand_query()` generates search variations (abbreviations, synonyms, stop-word stripping)
2. `kiwix_search_articles()` searches ALL Kiwix books via `/suggest` (JSON, per-book) + `/search` (HTML, cross-book)
3. `fetch_and_clean_article()` fetches and extracts readable text from the top result
4. `ask_llm()` sends context + conversation history to llama.cpp `/v1/chat/completions`
5. If LLM says `IRRELEVANT_ARTICLE`, tries the next search candidate
6. Follow-up questions reuse the last source (frontend sends `last_source`)

## Important Constraints

- **Do NOT run install.sh or test on the Pi** unless the user explicitly asks. User does Pi operations to save credits.
- **No extra LLM calls for search routing** — the "router" pattern was removed because it added minutes of latency on Pi hardware.
- **Users are regular people in survival situations** — they type "CPR" not "cardiopulmonary resuscitation". The search must handle abbreviations and natural language.
- **Search ALL databases** — Wikipedia, iFixit, WikiMed, Gutenberg. Not just one book.
- **Port 80** requires `CAP_NET_BIND_SERVICE` — handled by systemd `AmbientCapabilities`, NOT setcap.
- **Kiwix OPDS** — `.meta4` suffix must be stripped from download URLs to get direct `.zim` link.

## Debug Tools

- `GET /healthz` — basic health check
- `POST /api/debug-search` — shows expanded queries, discovered books, candidates, article preview
- `journalctl -u ark-flask -f` — live Flask logs (search hits, article fetch sizes, LLM calls)
- `ARK_VERBOSE=1` in service env — extra logging for LLM request/response details

## Common Gotchas

- Kiwix `/suggest` needs a `content` (book name) parameter; `/search` is cross-book
- ZIM filenames ≠ Kiwix book names (book names come from the ZIM metadata)
- `xterm-ghostty` terminal: use `printf '\033[2J\033[H'` instead of `clear`
- Venv python is a symlink to system python — `setcap` on it breaks `pip`
- SIGTERM handler must re-raise with `SIG_DFL` or Flask socket loop hangs 90s
