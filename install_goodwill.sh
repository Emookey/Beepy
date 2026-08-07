#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo:"
  echo "  sudo ./install_goodwill.sh"
  exit 1
fi

APP_DIR="/opt/mbc-intelligence"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing system packages..."
apt-get update
apt-get install -y ca-certificates curl jq git rsync unzip

systemctl enable --now docker
usermod -aG docker "${SUDO_USER:-$USER}" || true

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled

if ! command -v ollama >/dev/null 2>&1; then
  echo "Installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
fi
systemctl enable --now ollama

echo "Checking NVIDIA GPU..."
nvidia-smi || echo "NVIDIA driver is not active yet. Install/reboot before expecting GPU acceleration."

echo "Pulling recommended models..."
sudo -u ollama ollama pull qwen3.5:9b || ollama pull qwen3.5:9b
sudo -u ollama ollama pull qwen3-embedding:0.6b || ollama pull qwen3-embedding:0.6b

echo "Installing application into $APP_DIR..."
mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.env' "$SOURCE_DIR/" "$APP_DIR/"
chown -R "${SUDO_USER:-root}:${SUDO_USER:-root}" "$APP_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

echo
echo "Installation files are ready."
echo "Next:"
echo "  1. Edit $APP_DIR/.env"
echo "  2. Run: cd $APP_DIR && ./deploy.sh"
echo
echo "You may need to run 'sudo tailscale up' first if Goodwill is not signed in."
