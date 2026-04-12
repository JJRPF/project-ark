#!/usr/bin/env bash
#
# Project Ark — Interactive Installer
# ------------------------------------
# Builds a fully offline, battery-powered community emergency network node
# on a Raspberry Pi 5 running Raspberry Pi OS Lite (64-bit / Debian bookworm).
#
# Components deployed:
#   - Ollama + local LLM (default: gemma4:4b)
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

# ---------- Interactive: SSD mount point ----------
echo -e "${BOLD}[1/2] External SSD mount point${NC}"
echo "Project Ark stores every .zim file, library.xml, and config.json on an"
echo "external SSD. You will download and manage .zim files from the web-based"
echo "admin panel AFTER installation — this script does NOT download content."
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

# Directories / files we will manage on the SSD.
ARK_DATA_DIR="${ARK_MOUNT}/ark-data"
ZIM_DIR="${ARK_DATA_DIR}/zims"
LIBRARY_XML="${ARK_DATA_DIR}/library.xml"
CONFIG_JSON="${ARK_DATA_DIR}/config.json"

mkdir -p "${ZIM_DIR}"
ok "Ark data directory: ${ARK_DATA_DIR}"
echo

# ---------- Interactive: Model choice ----------
echo -e "${BOLD}[2/2] Select Ollama model${NC}"
echo "Project Ark restricts model selection to the gemma4 family."
echo "  1) gemma4:2b   (tiny — fastest, lowest RAM)"
echo "  2) gemma4:4b   (default — balanced, recommended for Pi 5 8GB)"
read -rp "Choice [2]: " model_choice
case "${model_choice}" in
    1) OLLAMA_MODEL="gemma4:2b" ;;
    *) OLLAMA_MODEL="gemma4:4b" ;;
esac
ok "Using model: ${OLLAMA_MODEL}"
echo

# ---------- Final confirmation ----------
echo -e "${BOLD}Summary:${NC}"
echo "  Install user : ${INSTALL_USER}"
echo "  Ark dir      : ${ARK_DIR}"
echo "  SSD mount    : ${ARK_MOUNT}"
echo "  Data dir     : ${ARK_DATA_DIR}"
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
    build-essential libcap2-bin \
    git

# ---------- Ollama ----------
if ! command -v ollama >/dev/null 2>&1; then
    log "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    ok "Ollama already installed."
fi

log "Enabling ollama.service..."
systemctl enable --now ollama.service || warn "Could not enable ollama.service (may already be running)."

log "Pulling model ${OLLAMA_MODEL} (this can take a while)..."
sudo -u "${INSTALL_USER}" ollama pull "${OLLAMA_MODEL}" || \
    warn "Failed to pull ${OLLAMA_MODEL}. You can retry manually with: ollama pull ${OLLAMA_MODEL}"

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
log "Staging application files into ${ARK_DIR}..."
mkdir -p "${ARK_DIR}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "${SCRIPT_DIR}/app.py"        "${ARK_DIR}/"
cp -r "${SCRIPT_DIR}/requirements.txt" "${ARK_DIR}/"
cp -r "${SCRIPT_DIR}/templates"     "${ARK_DIR}/"
cp -r "${SCRIPT_DIR}/static"        "${ARK_DIR}/"
chown -R "${INSTALL_USER}:${INSTALL_USER}" "${ARK_DIR}"

log "Creating Python virtualenv..."
sudo -u "${INSTALL_USER}" python3 -m venv "${VENV_DIR}"
sudo -u "${INSTALL_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip
sudo -u "${INSTALL_USER}" "${VENV_DIR}/bin/pip" install -r "${ARK_DIR}/requirements.txt"

# Allow the venv python to bind to privileged port 80 without running as root.
log "Granting CAP_NET_BIND_SERVICE to venv python..."
VENV_PY="$(readlink -f "${VENV_DIR}/bin/python")"
setcap 'cap_net_bind_service=+ep' "${VENV_PY}" || \
    warn "Could not setcap on ${VENV_PY}. Flask may fail to bind port 80."

# ---------- systemd units ----------
log "Installing systemd unit files..."

# ark-kiwix.service — templated with library.xml + binary path
sed \
    -e "s|@LIBRARY_XML@|${LIBRARY_XML}|g" \
    -e "s|@KIWIX_BIN@|${KIWIX_BIN}|g" \
    "${SCRIPT_DIR}/ark-kiwix.service" > /etc/systemd/system/ark-kiwix.service

# ark-flask.service — templated with user & paths (includes ARK_DATA_DIR)
sed \
    -e "s|@INSTALL_USER@|${INSTALL_USER}|g" \
    -e "s|@ARK_DIR@|${ARK_DIR}|g" \
    -e "s|@VENV_DIR@|${VENV_DIR}|g" \
    -e "s|@ARK_DATA_DIR@|${ARK_DATA_DIR}|g" \
    -e "s|@OLLAMA_MODEL@|${OLLAMA_MODEL}|g" \
    "${SCRIPT_DIR}/ark-flask.service" > /etc/systemd/system/ark-flask.service

systemctl daemon-reload
systemctl enable ark-kiwix.service ark-flask.service
systemctl restart ark-kiwix.service
sleep 2
systemctl restart ark-flask.service
sleep 2

# ---------- Health check ----------
echo
log "Service status:"
systemctl --no-pager --lines=3 status ark-kiwix.service || true
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
       iFixit, WikiHow, Gutenberg, ...) onto the SSD.
    2. Configure your Asus captive portal router to redirect all
       HTTP traffic to http://${IP_ADDR}/  (see ROUTER_SETUP.md).
    3. Set this Pi to a STATIC IP matching the router's redirect.
    4. Disconnect from the internet — Ark is now fully offline.

  Useful commands:
    sudo systemctl status ark-flask
    sudo systemctl status ark-kiwix
    journalctl -u ark-flask -f
    journalctl -u ark-kiwix -f

  Stay safe out there.
================================================================
SUCCESS
echo -e "${NC}"
