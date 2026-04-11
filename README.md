# Project Ark

> *Because when the grid fails, deploying an automated Wikipedia + AI captive portal via a single curl command is the ultimate flex.*

Project Ark is a **completely offline, battery-powered community emergency network node** built on a Raspberry Pi 5. It pairs a full offline mirror of Wikipedia (served by Kiwix) with a local Large Language Model (served by Ollama) to answer survival, medical, and practical questions for anyone who connects to its open Wi-Fi network — no internet, no cloud, no accounts.

Connecting clients are automatically captured into a dark-mode, mobile-first web portal where they can "Consult the Ark".

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
                                    ┌───────────────────────────────┐
                                    │ Raspberry Pi 5 (static IP)    │
                                    │  • Flask portal   :80         │
                                    │  • Kiwix-serve    :8080       │
                                    │  • Ollama (local LLM) :11434  │
                                    │  • External SSD (.zim file)   │
                                    └───────────────────────────────┘
```

The RAG pipeline:

1. User submits a query via the captive portal.
2. Flask hits the local **Kiwix API** to find the best Wikipedia article.
3. The article HTML is fetched, cleaned with BeautifulSoup, truncated to ~1,500 words.
4. The cleaned context + user query is passed to **Ollama** with a strict survival-assistant system prompt.
5. The LLM answer is streamed back to the browser in a bulleted, actionable format.

Everything runs on the Pi. Nothing leaves the Pi. There is no uplink.

---

## Hardware Requirements

| Component | Notes |
|---|---|
| **Raspberry Pi 5 (8 GB)** | Required. 4 GB will OOM on `llama3:8b`. |
| **Active cooling (fan + heatsink)** | LLM inference will thermal throttle otherwise. |
| **External USB 3.0 SSD (≥ 256 GB)** | Holds the Wikipedia `.zim` (English all-maxi is ~100 GB). |
| **Asus router** supporting **DD-WRT or FreshTomato** | E.g. RT-AC68U, RT-N66U. Required for captive portal. |
| **LiFePO₄ battery + buck converter** | 12 V → 5 V 5 A USB-C PD. Target 12+ hours runtime. |
| **Ethernet cable** | Pi to router LAN port. |

---

## Software Stack

- Raspberry Pi OS Lite (64-bit, Debian bookworm)
- Python 3.11+ / Flask
- Ollama + `gemma4:4b` (default) or `llama3:8b`
- Kiwix-serve (ARM64) + Wikipedia ZIM
- systemd (two units: `ark-flask`, `ark-kiwix`)

---

## Installation

> **⚠️ READ [`ROUTER_SETUP.md`](./ROUTER_SETUP.md) FIRST.** The Pi on its own does not broadcast Wi-Fi. A correctly configured captive portal router is mandatory or no client will ever reach Ark.

1. **Flash** Raspberry Pi OS Lite (64-bit) to the Pi's SD card.
2. **Mount** your external SSD containing a downloaded Wikipedia `.zim` file. Download zims from <https://library.kiwix.org/>.
3. **Clone** this repo onto the Pi:
   ```bash
   git clone https://github.com/<you>/project-ark.git
   cd project-ark
   chmod +x install.sh
   sudo ./install.sh
   ```
4. The installer will:
   - Warn you (loudly) about the router requirement.
   - Ask for the absolute path to your `.zim` file.
   - Ask which Ollama model to pull.
   - Update the OS and install dependencies.
   - Install Ollama, Kiwix-serve, and the Python venv.
   - Deploy and start the two systemd services.

5. Give the Pi a **static IP** on the router's LAN, then configure the router's captive portal to redirect to that IP per [`ROUTER_SETUP.md`](./ROUTER_SETUP.md).

---

## Operation

```bash
# Tail live logs
journalctl -u ark-flask -f
journalctl -u ark-kiwix -f

# Restart services
sudo systemctl restart ark-flask ark-kiwix

# Swap models
ollama pull llama3:8b
sudo systemctl edit ark-flask      # change OLLAMA_MODEL env
sudo systemctl restart ark-flask
```

---

## Repository Layout

```
project-ark/
├── install.sh              # Interactive bash installer
├── README.md               # You are here
├── ROUTER_SETUP.md         # Manual router flashing + captive portal guide
├── app.py                  # Flask + RAG pipeline
├── requirements.txt        # Python deps
├── templates/
│   └── index.html          # Mobile-first dark portal
├── static/
│   └── style.css           # Zero-dependency offline CSS
├── ark-flask.service       # systemd unit for Flask
└── ark-kiwix.service       # systemd unit for Kiwix-serve
```

---

## License

MIT. Use it, fork it, deploy it, survive with it.
