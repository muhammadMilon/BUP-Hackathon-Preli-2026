#!/usr/bin/env bash
# Push a code change to an already-provisioned VPS.
#
#   cd /root/campus-energy && bash deploy/update.sh
#
# Keeps /opt/campus-energy/.env and the virtualenv; only refreshes the code,
# reinstalls dependencies if requirements.txt changed, and restarts the service.

set -euo pipefail

APP_DIR=/opt/campus-energy
APP_USER=campus
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (or with sudo)." >&2
    exit 1
fi
if [[ ! -d "$APP_DIR/venv" ]]; then
    echo "$APP_DIR is not provisioned yet -- run deploy/setup.sh first." >&2
    exit 1
fi

echo "==> Refreshing code"
for item in app samples; do
    rm -rf "${APP_DIR:?}/${item}"
    cp -r "$SRC_DIR/$item" "$APP_DIR/"
done
cp -r "$SRC_DIR/scripts/." "$APP_DIR/scripts/" 2>/dev/null || true
cp -r "$SRC_DIR/tests/." "$APP_DIR/tests/" 2>/dev/null || true

if ! cmp -s "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"; then
    echo "==> requirements.txt changed, reinstalling dependencies"
    cp "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
    "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> Restarting"
systemctl restart campus-energy

for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo "==> Healthy"
        curl -fsS http://127.0.0.1/health
        echo
        exit 0
    fi
    sleep 1
done

echo "Service did not become healthy in 20s. Recent logs:" >&2
journalctl -u campus-energy -n 40 --no-pager >&2
exit 1
