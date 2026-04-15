#!/usr/bin/env bash
#
# Project Ark — Interactive Installer
# ------------------------------------
# Builds a fully offline, battery-powered community emergency network node
# on a Raspberry Pi 5 running Raspberry Pi OS Lite (64-bit / Debian bookworm).
#
# Components deployed:
#   - llama.cpp + local LLM (default: google/gemma-4-E2B-it)
#   - Kiwix-serve (ARM64) hosting an offline Wikipedia .zim
#   - Flask RAG app bound to port 80 (served via systemd)
#   - ark-kiwix.service and ark-flask.service
#
# Usage:
#   sudo ./install.sh
#

set -euo pipefail

# ---------- Terminal colors ----------
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

INSTALL_USER="${SUDO_USER:-$USER}"
INSTALL_HOME="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
ARK_DIR="${INSTALL_HOME}/project-ark"
VENV_DIR="${ARK_DIR}/venv"
KIWIX_DIR="/opt/kiwix"
KIWIX_BIN="${KIWIX_DIR}/kiwix-serve"
KIWIX_MANAGE_BIN="${KIWIX_DIR}/kiwix-manage"
KIWIX_VERSION="3.7.0-2"
KIWIX_TARBALL="kiwix-tools_linux-aarch64-${KIWIX_VERSION}.tar.gz"
KIWIX_URL="https://download.kiwix.org/release/kiwix-tools/${KIWIX_TARBALL}"

# ---------- Helpers ----------
log()  { echo -e "${CYAN}[ark]${NC} $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
die()  { echo -e "${RED}[fail]${NC} $*" >&2; exit 1; }

# ---------- Root check ----------
if [[ "${EUID}" -ne 0 ]]; then
    die "This installer must be run as root. Try: sudo ./install.sh"
fi

# ---------- Pre-flight banner ----------
# Use a raw ANSI clear instead of `clear`, which reads terminfo and fails
# under unknown TERM values (e.g. xterm-ghostty on a fresh Pi OS Lite image).
printf '\033[2J\033[H'
cat <<'BANNER'

  ____            _           _       _         _
 |  _ \ _ __ ___ (_) ___  ___| |_    / \   _ __| | __
 | |_) | '__/ _ \| |/ _ \/ __| __|  / _ \ | '__| |/ /
 |  __/| | | (_) | |  __/ (__| |_  / ___ \| |  |   <
 |_|   |_|  \___// |\___|\___|\__|/_/   \_\_|  |_|\_\
               |__/
        Offline Emergency Knowledge Node — Pi 5

BANNER

echo -e "${RED}${BOLD}"
cat <<'WARN'
================================================================
!!  STOP — READ THIS BEFORE CONTINUING                        !!
================================================================

  Project Ark requires a SPECIFICALLY CONFIGURED captive portal
  router to function. This Pi does NOT broadcast Wi-Fi. It is a
  backend node that a separate Asus router (DD-WRT / FreshTomato)
  MUST redirect clients to via NoDogSplash.

  If you have not yet flashed and configured your router, this
  installation will succeed but NO CLIENT will ever reach it.

  >>> Please read ROUTER_SETUP.md BEFORE continuing. <<<

================================================================
WARN
echo -e "${NC}"

read -rp "$(echo -e "${BOLD}Have you read ROUTER_SETUP.md and configured the router? [Y/N]: ${NC}")" ack
case "${ack,,}" in
    y|yes)
        ok "Acknowledged. Proceeding with installation."
        ;;
    *)
        warn "Installation aborted. Please read ROUTER_SETUP.md first."
        exit 0
        ;;
esac

echo

# ---------- Interactive: Storage location ----------
echo -e "${BOLD}[1/2] Storage location${NC}"
echo "Project Ark stores .zim files, library.xml, and config.json in a data"
echo "directory. You will download content from the /admin panel AFTER install."
echo
echo "Where should Ark store its data?"
echo "  1) External SSD  (recommended — room for full Wikipedia ~100 GB)"
echo "  2) Boot SD card   (simpler — only small content like WikiMed fits)"
read -rp "Choice [1]: " storage_choice

case "${storage_choice}" in
    2)
        # ---------- SD card path ----------
        ARK_DATA_DIR="${INSTALL_HOME}/ark-data"
        warn "Using the boot SD card at ${ARK_DATA_DIR}."
        warn "Space is limited — large resources like Wikipedia Maxi won't fit."
        ;;
    *)
        # ---------- External SSD ----------
        echo
        echo "Enter the ABSOLUTE path where your SSD is (or will be) mounted."
        echo "Example: /mnt/ssd-ark"
        read -rp "SSD mount point: " ARK_MOUNT

        if [[ -z "${ARK_MOUNT}" ]]; then
            die "SSD mount point cannot be empty."
        fi

        # Verify it's a real mountpoint — if not, offer to mount it interactively.
        if ! mountpoint -q "${ARK_MOUNT}" 2>/dev/null; then
            warn "'${ARK_MOUNT}' is not currently a mountpoint."
            echo
            echo "Detected block devices:"
            lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,UUID | sed 's/^/  /'
            echo
            read -rp "Attempt to mount a device at ${ARK_MOUNT} now? [y/N]: " do_mount
            if [[ "${do_mount,,}" == "y" ]]; then
                read -rp "Device to mount (e.g. /dev/sda1): " ARK_DEV
                [[ -b "${ARK_DEV}" ]] || die "'${ARK_DEV}' is not a block device."
                mkdir -p "${ARK_MOUNT}"
                mount "${ARK_DEV}" "${ARK_MOUNT}" || die "Failed to mount ${ARK_DEV}."
                ok "Mounted ${ARK_DEV} at ${ARK_MOUNT}"

                read -rp "Add an /etc/fstab entry so it auto-mounts on boot? [y/N]: " do_fstab
                if [[ "${do_fstab,,}" == "y" ]]; then
                    ARK_UUID="$(blkid -s UUID -o value "${ARK_DEV}" || true)"
                    ARK_FSTYPE="$(blkid -s TYPE -o value "${ARK_DEV}" || true)"
                    if [[ -n "${ARK_UUID}" && -n "${ARK_FSTYPE}" ]]; then
                        if ! grep -q "${ARK_UUID}" /etc/fstab; then
                            echo "UUID=${ARK_UUID} ${ARK_MOUNT} ${ARK_FSTYPE} defaults,nofail 0 2" >> /etc/fstab
                            ok "Added fstab entry for UUID=${ARK_UUID}"
                        else
                            warn "fstab already contains an entry for this UUID — skipping."
                        fi
                    else
                        warn "Could not read UUID/FSTYPE for ${ARK_DEV} — skipping fstab."
                    fi
                fi
            else
                die "Mount the SSD at ${ARK_MOUNT} and re-run this installer."
            fi
        fi
        ARK_DATA_DIR="${ARK_MOUNT}/ark-data"
        ;;
esac

# Directories / files we will manage in the data directory.
ZIM_DIR="${ARK_DATA_DIR}/zims"
LIBRARY_XML="${ARK_DATA_DIR}/library.xml"
CONFIG_JSON="${ARK_DATA_DIR}/config.json"

mkdir -p "${ZIM_DIR}"
ok "Ark data directory: ${ARK_DATA_DIR}"
echo

# ---------- Interactive: Model choice ----------
echo -e "${BOLD}[2/2] Select LLM model (GGUF format)${NC}"
echo
echo "  RAM estimates include model weights + KV cache + inference overhead."
echo "  Pi 5 (8 GB) typically has ~5–6 GB free after OS + Kiwix + Flask."
echo
echo -e "  ${BOLD}Recommended:${NC}"
echo "    1) google/gemma-4-E2B-it (~2 GB RAM — fast, fits comfortably on 8 GB Pi)"
echo
echo -e "  ${BOLD}Advanced:${NC}"
echo "    2) google/gemma-4-E4B-it (~7 GB RAM — better quality, TIGHT on 8 GB — may OOM)"
echo "    3) phi-2          (~4 GB RAM — Microsoft, strong reasoning)"
echo "    4) mistral-7b     (~5 GB RAM — Mistral, fast + capable)"
echo "    5) neural-chat-7b (~5 GB RAM — Intel, optimized)"
echo "    6) Custom         (enter GGUF model name)"
read -rp "Choice [1]: " model_choice
case "${model_choice}" in
    2) OLLAMA_MODEL="google/gemma-4-E4B-it" ;;
    3) OLLAMA_MODEL="phi-2"  ;;
    4) OLLAMA_MODEL="mistral-7b" ;;
    5) OLLAMA_MODEL="neural-chat-7b" ;;
    6)
        read -rp "Enter GGUF model name: " custom_model
        [[ -z "${custom_model}" ]] && die "Model tag cannot be empty."
        OLLAMA_MODEL="${custom_model}"
        ;;
    *) OLLAMA_MODEL="google/gemma-4-E2B-it" ;;
esac
ok "Using model: ${OLLAMA_MODEL}"
echo

# ---------- Final confirmation ----------
echo -e "${BOLD}Summary:${NC}"
echo "  Install user : ${INSTALL_USER}"
echo "  Ark dir      : ${ARK_DIR}"
echo "  Data dir     : ${ARK_DATA_DIR}"
echo "  Storage      : ${ARK_MOUNT:-boot SD card}"
echo "  Ollama model : ${OLLAMA_MODEL}"
echo
read -rp "Proceed with installation? [Y/n]: " go
case "${go,,}" in
    n|no) die "User aborted." ;;
esac

# ---------- OS update ----------
log "Updating apt package index..."
apt-get update -y
log "Upgrading existing packages..."
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

# ---------- Dependencies ----------
log "Installing system dependencies..."
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip \
    curl wget tar ca-certificates \
    build-essential libxml2-dev libxslt-dev \
    git

# ---------- llama.cpp ----------
LLAMA_CPP_DIR="/opt/llama-cpp"
LLAMA_SERVER="${LLAMA_CPP_DIR}/server"

if [[ ! -x "${LLAMA_SERVER}" ]]; then
    log "Installing llama.cpp..."
    mkdir -p "${LLAMA_CPP_DIR}"

    # Detect architecture
    ARCH=$(uname -m)
    if [[ "${ARCH}" == "aarch64" ]]; then
        LLAMA_RELEASE="llama-cpp-arm64"
    elif [[ "${ARCH}" == "armv7l" ]]; then
        LLAMA_RELEASE="llama-cpp-armv7"
    else
        LLAMA_RELEASE="llama-cpp-x86_64"
    fi

    # Download latest llama.cpp release
    LATEST_URL=$(curl -s https://api.github.com/repos/ggerganov/llama.cpp/releases/latest | \
        grep "browser_download_url" | grep "${LLAMA_RELEASE}" | head -1 | cut -d'"' -f4)

    if [[ -z "${LATEST_URL}" ]]; then
        warn "Could not auto-detect llama.cpp release. Please download manually from:"
        warn "https://github.com/ggerganov/llama.cpp/releases"
        warn "Extract to ${LLAMA_CPP_DIR} and ensure 'server' binary exists."
    else
        tmp="$(mktemp -d)"
        pushd "${tmp}" >/dev/null
        wget -q --show-progress "${LATEST_URL}" -O llama.tar.gz || \
            die "Failed to download llama.cpp from ${LATEST_URL}"
        tar -xzf llama.tar.gz
        cp -r llama-cpp-*/* "${LLAMA_CPP_DIR}/" 2>/dev/null || true
        popd >/dev/null
        rm -rf "${tmp}"

        if [[ ! -x "${LLAMA_SERVER}" ]]; then
            die "llama.cpp server binary not found after extraction. Install manually."
        fi
        ok "llama.cpp installed at ${LLAMA_CPP_DIR}"
    fi
else
    ok "llama.cpp already installed."
fi

# ---------- Kiwix-serve ARM64 ----------
if [[ ! -x "${KIWIX_BIN}" ]]; then
    log "Installing Kiwix-serve (aarch64)..."
    mkdir -p "${KIWIX_DIR}"
    tmp="$(mktemp -d)"
    pushd "${tmp}" >/dev/null
    wget -q --show-progress "${KIWIX_URL}" -O "${KIWIX_TARBALL}" || \
        die "Failed to download Kiwix tools from ${KIWIX_URL}"
    tar -xzf "${KIWIX_TARBALL}"
    cp kiwix-tools_linux-aarch64-*/kiwix-serve  "${KIWIX_BIN}"
    cp kiwix-tools_linux-aarch64-*/kiwix-manage "${KIWIX_MANAGE_BIN}"
    chmod +x "${KIWIX_BIN}" "${KIWIX_MANAGE_BIN}"
    popd >/dev/null
    rm -rf "${tmp}"
    ok "Kiwix-serve + kiwix-manage installed at ${KIWIX_DIR}"
else
    ok "Kiwix-serve already installed."
    # Belt-and-braces: make sure kiwix-manage is there too.
    if [[ ! -x "${KIWIX_MANAGE_BIN}" ]]; then
        warn "kiwix-manage missing — re-extracting."
        tmp="$(mktemp -d)"
        pushd "${tmp}" >/dev/null
        wget -q "${KIWIX_URL}" -O "${KIWIX_TARBALL}"
        tar -xzf "${KIWIX_TARBALL}"
        cp kiwix-tools_linux-aarch64-*/kiwix-manage "${KIWIX_MANAGE_BIN}"
        chmod +x "${KIWIX_MANAGE_BIN}"
        popd >/dev/null
        rm -rf "${tmp}"
    fi
fi

# Initialize an empty library.xml on the SSD if one isn't there yet.
# kiwix-serve will monitor this file and auto-reload when Flask adds books.
if [[ ! -f "${LIBRARY_XML}" ]]; then
    cat > "${LIBRARY_XML}" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<library version="20110515"></library>
XML
    ok "Initialized empty library.xml at ${LIBRARY_XML}"
fi
chown -R "${INSTALL_USER}:${INSTALL_USER}" "${ARK_DATA_DIR}"

# ---------- Python venv & app deployment ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(realpath "${SCRIPT_DIR}")" != "$(realpath "${ARK_DIR}")" ]]; then
    log "Staging application files into ${ARK_DIR}..."
    mkdir -p "${ARK_DIR}"
    cp -r "${SCRIPT_DIR}/app.py"           "${ARK_DIR}/"
    cp -r "${SCRIPT_DIR}/requirements.txt" "${ARK_DIR}/"
    cp -r "${SCRIPT_DIR}/templates"        "${ARK_DIR}/"
    cp -r "${SCRIPT_DIR}/static"           "${ARK_DIR}/"
    chown -R "${INSTALL_USER}:${INSTALL_USER}" "${ARK_DIR}"
else
    ok "Repo IS the deployment directory — skipping copy."
fi

log "Creating Python virtualenv..."
sudo -u "${INSTALL_USER}" python3 -m venv "${VENV_DIR}"
sudo -u "${INSTALL_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip
sudo -u "${INSTALL_USER}" "${VENV_DIR}/bin/pip" install -r "${ARK_DIR}/requirements.txt"

# NOTE: We do NOT setcap on the venv python. In a venv, python is a symlink
# to the system binary (e.g. /usr/bin/python3.13). Setting file capabilities
# on it triggers LD_LIBRARY_PATH restrictions that break pip and C extensions.
# Port 80 binding is handled safely via AmbientCapabilities in ark-flask.service.

# ---------- systemd units ----------
log "Installing systemd unit files..."

# ark-kiwix.service — templated with library.xml + binary path
sed \
    -e "s|@LIBRARY_XML@|${LIBRARY_XML}|g" \
    -e "s|@KIWIX_BIN@|${KIWIX_BIN}|g" \
    "${SCRIPT_DIR}/ark-kiwix.service" > /etc/systemd/system/ark-kiwix.service

# ark-llama-cpp.service — templated with user, paths, and model
# Note: GGUF models should be in a standard location or downloaded first
MODELS_DIR="${ARK_DATA_DIR}/models"
mkdir -p "${MODELS_DIR}"
sed \
    -e "s|@INSTALL_USER@|${INSTALL_USER}|g" \
    -e "s|@MODELS_DIR@|${MODELS_DIR}|g" \
    -e "s|@OLLAMA_MODEL@|${OLLAMA_MODEL}|g" \
    "${SCRIPT_DIR}/ark-llama-cpp.service" > /etc/systemd/system/ark-llama-cpp.service

# ark-flask.service — templated with user & paths (includes ARK_DATA_DIR)
sed \
    -e "s|@INSTALL_USER@|${INSTALL_USER}|g" \
    -e "s|@ARK_DIR@|${ARK_DIR}|g" \
    -e "s|@VENV_DIR@|${VENV_DIR}|g" \
    -e "s|@ARK_DATA_DIR@|${ARK_DATA_DIR}|g" \
    -e "s|@OLLAMA_MODEL@|${OLLAMA_MODEL}|g" \
    "${SCRIPT_DIR}/ark-flask.service" > /etc/systemd/system/ark-flask.service

systemctl daemon-reload
systemctl enable ark-kiwix.service ark-llama-cpp.service ark-flask.service
systemctl restart ark-kiwix.service
sleep 2
systemctl restart ark-llama-cpp.service
sleep 2
systemctl restart ark-flask.service
sleep 2

# ---------- Health check ----------
echo
log "Service status:"
systemctl --no-pager --lines=3 status ark-kiwix.service || true
echo
systemctl --no-pager --lines=3 status ark-llama-cpp.service || true
echo
systemctl --no-pager --lines=3 status ark-flask.service || true
echo

# ---------- Success banner ----------
IP_ADDR="$(hostname -I | awk '{print $1}')"
echo -e "${GREEN}${BOLD}"
cat <<SUCCESS
================================================================
  Project Ark installation complete.
================================================================

  Kiwix-serve  : http://${IP_ADDR}:8080
  Flask portal : http://${IP_ADDR}/           (port 80)
  Admin panel  : http://${IP_ADDR}/admin      (download content here)
  Model        : ${OLLAMA_MODEL}
  Data dir     : ${ARK_DATA_DIR}

  Next steps:
    1. Temporarily connect this Pi to the internet and open the
       admin panel to download ZIM resources (Wikipedia, WikiMed,
       iFixit, WikiHow, Gutenberg, ...) to the data directory.
    2. Configure your Asus captive portal router to redirect all
       HTTP traffic to http://${IP_ADDR}/  (see ROUTER_SETUP.md).
    3. Set this Pi to a STATIC IP matching the router's redirect.
    4. Disconnect from the internet — Ark is now fully offline.

  Useful commands:
    sudo systemctl status ark-flask
    sudo systemctl status ark-llama-cpp
    sudo systemctl status ark-kiwix
    journalctl -u ark-flask -f
    journalctl -u ark-llama-cpp -f
    journalctl -u ark-kiwix -f

  Stay safe out there.
================================================================
SUCCESS
echo -e "${NC}"
