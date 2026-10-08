#!/usr/bin/env bash
# One-shot setup for Debian/Ubuntu (sandbox, VPS, or WSL on the laptop). macOS/Windows: see README "Laptop".
set -e
sudo apt-get update
sudo apt-get install -y chromium xvfb x11vnc novnc xdotool imagemagick x11-utils curl python3-pip || \
sudo apt-get install -y chromium-browser xvfb x11vnc novnc xdotool imagemagick x11-utils curl python3-pip
pip install -r "$(dirname "$0")/requirements.txt"
mkdir -p ~/.agentapp
ARCH=$(uname -m); case "$ARCH" in aarch64|arm64) CF=arm64;; *) CF=amd64;; esac
[ -x ~/.agentapp/cloudflared ] || curl -sL -o ~/.agentapp/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF && chmod +x ~/.agentapp/cloudflared
echo "Done. Start with:  OPENROUTER_API_KEY=... python3 $(dirname "$0")/main.py --tunnel"
