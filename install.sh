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
clear
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

# ---------- Interactive: SSD path ----------
echo -e "${BOLD}[1/2] External SSD / Wikipedia ZIM path${NC}"
echo "Enter the ABSOLUTE path to your offline Wikipedia .zim file."
echo "Example: /mnt/ssd/wikipedia_en_all_maxi_2024-01.zim"
read -rp "ZIM path: " ZIM_PATH

if [[ -z "${ZIM_PATH}" ]]; then
    die "ZIM path cannot be empty."
fi
if [[ ! -f "${ZIM_PATH}" ]]; then
    warn "File '${ZIM_PATH}' does not exist yet."
    read -rp "Continue anyway? (service will fail until the file exists) [y/N]: " cont
    [[ "${cont,,}" == "y" ]] || die "Aborting — mount the SSD and re-run."
fi
ok "Using ZIM: ${ZIM_PATH}"
echo

# ---------- Interactive: Model choice ----------
echo -e "${BOLD}[2/2] Select Ollama model${NC}"
echo "  1) gemma4:4b   (default — small, fast, Pi 5 friendly)"
echo "  2) llama3:8b   (larger, slower, better reasoning)"
read -rp "Choice [1]: " model_choice
case "${model_choice}" in
    2) OLLAMA_MODEL="llama3:8b" ;;
    *) OLLAMA_MODEL="gemma4:4b" ;;
esac
ok "Using model: ${OLLAMA_MODEL}"
echo

# ---------- Final confirmation ----------
echo -e "${BOLD}Summary:${NC}"
echo "  Install user : ${INSTALL_USER}"
echo "  Ark dir      : ${ARK_DIR}"
echo "  ZIM file     : ${ZIM_PATH}"
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
    cp kiwix-tools_linux-aarch64-*/kiwix-serve "${KIWIX_BIN}"
    chmod +x "${KIWIX_BIN}"
    popd >/dev/null
    rm -rf "${tmp}"
    ok "Kiwix-serve installed at ${KIWIX_BIN}"
else
    ok "Kiwix-serve already installed."
fi

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

# ark-kiwix.service — templated with ZIM path
sed \
    -e "s|@ZIM_PATH@|${ZIM_PATH}|g" \
    -e "s|@KIWIX_BIN@|${KIWIX_BIN}|g" \
    "${SCRIPT_DIR}/ark-kiwix.service" > /etc/systemd/system/ark-kiwix.service

# ark-flask.service — templated with user & paths
sed \
    -e "s|@INSTALL_USER@|${INSTALL_USER}|g" \
    -e "s|@ARK_DIR@|${ARK_DIR}|g" \
    -e "s|@VENV_DIR@|${VENV_DIR}|g" \
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
  Flask portal : http://${IP_ADDR}/       (port 80)
  Model        : ${OLLAMA_MODEL}

  Next steps:
    1. Configure your Asus captive portal router to redirect all
       HTTP traffic to http://${IP_ADDR}/  (see ROUTER_SETUP.md).
    2. Set this Pi to a STATIC IP matching the router's redirect.
    3. Connect a client device — you should be captured into Ark.

  Useful commands:
    sudo systemctl status ark-flask
    sudo systemctl status ark-kiwix
    journalctl -u ark-flask -f
    journalctl -u ark-kiwix -f

  Stay safe out there.
================================================================
SUCCESS
echo -e "${NC}"
