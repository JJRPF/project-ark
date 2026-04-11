# Project Ark

> *Because when the grid fails, deploying an automated Wikipedia + AI captive portal via a single curl command is the ultimate flex.*

Project Ark is a **completely offline, battery-powered community emergency network node** built on a Raspberry Pi 5. It pairs a curated library of offline knowledge (Wikipedia, WikiMed, iFixit, WikiHow, Project Gutenberg — served by Kiwix) with a local Large Language Model (served by Ollama) to answer survival, medical, and practical questions for anyone who connects to its open Wi-Fi network — no internet, no cloud, no accounts.

Connecting clients are automatically captured into a dark-mode, mobile-first web portal where they can "Consult the Ark". Operators manage content and auto-updates from a separate `/admin` dashboard with a chunked, resumable downloader.

---

## Architecture

```
 ┌──────────────┐        Open SSID        ┌────────────────────────┐
 │ Client phone │ ───────────────────────▶│ Asus router            │
 │ (no data)    │◀───── Captive redirect ─│ (DD-WRT / FreshTomato) │
 └──────────────┘                         │ NoDogSplash → Pi :80   │
                                          └───────────┬────────────┘
                                                      │ Ethernet / LAN
                                                      ▼
                                    ┌────────────────────────────────┐
                                    │ Raspberry Pi 5 (static IP)     │
                                    │  • Flask portal      :80       │
                                    │  • Flask /admin      :80       │
                                    │  • Kiwix-serve       :8080     │
                                    │  • Ollama (local)    :11434    │
                                    │  • External SSD                │
                                    │      └─ ark-data/              │
                                    │         ├─ zims/*.zim          │
                                    │         ├─ library.xml         │
                                    │         └─ config.json         │
                                    └────────────────────────────────┘
```

### RAG pipeline (client-facing)

1. User submits a query via the captive portal.
2. Flask hits the local **Kiwix API** to find the best matching article.
3. The article HTML is fetched, cleaned with BeautifulSoup, truncated to ~1,500 words.
4. The cleaned context + user query is passed to **Ollama** (`gemma4` family) with a strict survival-assistant system prompt.
5. The LLM answer is rendered in the browser as bulleted, actionable steps.

### Content pipeline (operator-facing, `/admin`)

1. Operator browses the admin dashboard and sees a live **Storage Matrix** (free / used / total).
2. Picks from a curated **Resource Library** (Wikipedia, WikiMed, iFixit, WikiHow, Gutenberg). Items that won't fit on the SSD are auto-disabled.
3. Flask resolves the current `.zim` via the Kiwix **OPDS catalog**, then streams the download in **8 KiB chunks** with `Range`-header **resume** support.
4. On completion, Flask rebuilds `library.xml` via `kiwix-manage`; `kiwix-serve --monitorLibrary` auto-reloads. No restarts.
5. A background scheduler (interval set in the admin UI, saved to `config.json`) quietly checks the OPDS catalog and replaces stale `.zim` files with newer versions.

Everything runs on the Pi. Nothing leaves the Pi during normal operation. The only time the Pi needs internet is when the operator is actively downloading or auto-updating content.

---

## Hardware Requirements

| Component | Notes |
|---|---|
| **Raspberry Pi 5 (8 GB)** | Required. 4 GB will struggle with `gemma4:9b`. |
| **Active cooling (fan + heatsink)** | LLM inference will thermal throttle otherwise. |
| **External USB 3.0 SSD (≥ 256 GB)** | Holds all `.zim` files. English Wikipedia Maxi alone is ~100 GB; budget more if adding Gutenberg (~72 GB) or WikiHow (~12 GB). |
| **Asus router** supporting **DD-WRT or FreshTomato** | E.g. RT-AC68U, RT-N66U. Required for captive portal. |
| **LiFePO₄ battery + buck converter** | 12 V → 5 V 5 A USB-C PD. Target 12+ hours runtime. |
| **Ethernet cable** | Pi to router LAN port. |

---

## Software Stack

- Raspberry Pi OS Lite (64-bit, Debian bookworm)
- Python 3.11+ / Flask
- Ollama + the **gemma4 family only** (`gemma4:2b`, `gemma4:4b` default, `gemma4:9b`)
- Kiwix-serve + kiwix-manage (ARM64) with `--monitorLibrary` auto-reload
- Custom chunked resumable downloader + background scheduler (threaded)
- systemd (two units: `ark-flask`, `ark-kiwix`)

---

## Installation

> **⚠️ READ [`ROUTER_SETUP.md`](./ROUTER_SETUP.md) FIRST.** The Pi on its own does not broadcast Wi-Fi. A correctly configured captive portal router is mandatory or no client will ever reach Ark.

The installer **does not download any `.zim` content** — it only prepares the OS, installs Ollama and Kiwix tools, mounts the SSD, and deploys systemd. All content is downloaded later via the web-based `/admin` dashboard.

### Steps

1. **Flash** Raspberry Pi OS Lite (64-bit) to the Pi's SD card.
2. **Attach your external SSD** to the Pi (formatted, empty is fine). The installer can mount it for you and optionally add an `fstab` entry.
3. **Clone** this repo onto the Pi:
   ```bash
   git clone https://github.com/<you>/project-ark.git
   cd project-ark
   chmod +x install.sh
   sudo ./install.sh
   ```
4. The installer will:
   - Print a large warning and require Y/N acknowledgement of `ROUTER_SETUP.md`.
   - Ask for the **SSD mount point** (e.g. `/mnt/ssd-ark`). If the path isn't yet a mountpoint it shows `lsblk`, lets you pick a device, mounts it, and optionally adds an `fstab` entry via `blkid` UUID.
   - Ask which **gemma4 model** to pull (`gemma4:2b`, `gemma4:4b` default, `gemma4:9b`). No other families are allowed.
   - Update the OS and install dependencies.
   - Install Ollama, `kiwix-serve`, and `kiwix-manage` (ARM64).
   - Create `${MOUNT}/ark-data/{zims/,library.xml,config.json}`.
   - Build the Python venv and grant it `CAP_NET_BIND_SERVICE` for port 80.
   - Template and start `ark-kiwix.service` + `ark-flask.service`.

5. Once services are up, **open** `http://<pi-ip>/admin` from any LAN client and download the content you want. The Pi needs temporary internet access during this step only.

6. Give the Pi a **static IP** on the router's LAN, then configure the router's captive portal to redirect to that IP per [`ROUTER_SETUP.md`](./ROUTER_SETUP.md). Disconnect the internet afterward — Ark is now fully offline.

---

## Admin Dashboard

Visit `http://<pi-ip>/admin` to:

- See live **Storage Matrix** — total, used, free, and a dynamic recommendation of which content combinations will fit.
- Browse the curated **Resource Library**:
  - Wikipedia (English, Full) — ~102 GB
  - WikiMed Medicine — ~4.2 GB
  - iFixit Repair Guides — ~3.6 GB
  - WikiHow — ~12.3 GB
  - Project Gutenberg — ~72 GB
- Start a download — Flask streams the `.zim` in 8 KiB chunks. If the network drops, the next click resumes from the exact byte offset using an HTTP `Range` header.
- Watch real-time progress bars on each resource card.
- Set an **auto-update interval** (0–104 weeks). A background thread checks the Kiwix OPDS catalog and silently replaces any `.zim` that has a newer version.
- Trigger a manual "Check Now" update sweep.

All admin settings persist to `${ARK_DATA_DIR}/config.json` on the SSD.

---

## Operation

```bash
# Tail live logs
journalctl -u ark-flask -f
journalctl -u ark-kiwix -f

# Restart services
sudo systemctl restart ark-flask ark-kiwix

# Swap models (gemma4 family only)
ollama pull gemma4:9b
sudo systemctl edit ark-flask      # change ARK_OLLAMA_MODEL env
sudo systemctl restart ark-flask

# Manually inspect the library Kiwix is serving
cat ${ARK_DATA_DIR}/library.xml
ls -lh ${ARK_DATA_DIR}/zims/
```

---

## Repository Layout

```
project-ark/
├── install.sh              # Interactive bash installer (OS/deps/mount/systemd)
├── README.md               # You are here
├── ROUTER_SETUP.md         # Manual router flashing + captive portal guide
├── app.py                  # Flask: RAG pipeline + admin API + download manager
├── requirements.txt        # Python deps
├── templates/
│   ├── index.html          # Mobile-first dark captive portal
│   └── admin.html          # Content management + auto-update dashboard
├── static/
│   └── style.css           # Zero-dependency offline CSS (portal + admin)
├── ark-flask.service       # systemd unit for Flask
└── ark-kiwix.service       # systemd unit for Kiwix-serve (--monitorLibrary)
```

On the SSD, at runtime:

```
${ARK_DATA_DIR}/
├── zims/               # Every downloaded .zim lives here
├── library.xml         # kiwix-manage-built library, monitored by kiwix-serve
└── config.json         # Admin settings (update interval, download history)
```

---

## License

MIT. Use it, fork it, deploy it, survive with it.
