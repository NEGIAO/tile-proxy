#!/bin/bash
# 一键安装：用法 sudo bash deploy/install.sh [/opt/tile-proxy]
set -euo pipefail

PREFIX="${1:-/opt/tile-proxy}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$PREFIX" /var/cache/tile-proxy
if [ ! -d "$PREFIX/domains" ]; then
  cp -a "$REPO_DIR/." "$PREFIX/"
fi

python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -U pip
"$PREFIX/.venv/bin/pip" install -r "$PREFIX/requirements.txt"

if [ -f "$PREFIX/deploy/tile-proxy.service" ]; then
  cp "$PREFIX/deploy/tile-proxy.service" /etc/systemd/system/tile-proxy.service
  systemctl daemon-reload
  systemctl enable --now tile-proxy
  sleep 1
  systemctl is-active tile-proxy
  curl -fsS http://127.0.0.1:9002/health || true
  echo
fi

echo "install OK: $PREFIX"
