#!/usr/bin/env bash
# One-shot provisioning for a fresh Hostinger VPS (Ubuntu 22.04 / 24.04).
#
#   scp -r . root@YOUR_VPS_IP:/root/campus-energy
#   ssh root@YOUR_VPS_IP
#   cd /root/campus-energy && bash deploy/setup.sh
#
# Idempotent: safe to re-run. Use deploy/update.sh for later code pushes.

set -euo pipefail

APP_DIR=/opt/campus-energy
APP_USER=campus
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (or with sudo)." >&2
    exit 1
fi

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx ufw curl ca-certificates

echo "==> Creating service user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"

echo "==> Copying application code"
for item in app samples requirements.txt; do
    rm -rf "${APP_DIR:?}/${item}"
    cp -r "$SRC_DIR/$item" "$APP_DIR/"
done
# Useful on the box for verifying a live deployment.
mkdir -p "$APP_DIR/scripts" "$APP_DIR/tests"
cp -r "$SRC_DIR/scripts/." "$APP_DIR/scripts/" 2>/dev/null || true
cp -r "$SRC_DIR/tests/." "$APP_DIR/tests/" 2>/dev/null || true

echo "==> Building the virtualenv"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Preparing the environment file"
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
    echo
    echo "    !! $APP_DIR/.env was created from the template."
    echo "    !! Put your XAI_API_KEY and GEMINI_API_KEY in it, then re-run:"
    echo "    !!     systemctl restart campus-energy"
    echo
fi
# systemd EnvironmentFile cannot parse blank values with inline comments; strip them.
sed -i 's/[[:space:]]*#.*$//' "$APP_DIR/.env"
sed -i '/^[[:space:]]*$/d' "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> Installing the systemd unit"
cp "$SRC_DIR/deploy/campus-energy.service" /etc/systemd/system/campus-energy.service
systemctl daemon-reload
systemctl enable --quiet campus-energy
systemctl restart campus-energy

echo "==> Configuring nginx"
cp "$SRC_DIR/deploy/nginx.conf" /etc/nginx/sites-available/campus-energy
ln -sf /etc/nginx/sites-available/campus-energy /etc/nginx/sites-enabled/campus-energy
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

echo "==> Opening the firewall"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

echo "==> Waiting for the service to come up"
for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
    sleep 1
done

echo
echo "---- local health ----"
curl -fsS http://127.0.0.1:8000/health || echo "(direct uvicorn check failed)"
echo
echo "---- through nginx ----"
curl -fsS http://127.0.0.1/health || echo "(nginx check failed)"
echo
PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo YOUR_VPS_IP)"
echo
echo "Done. Submit these URLs:"
echo "    health:  http://$PUBLIC_IP/health"
echo "    main:    http://$PUBLIC_IP/optimize-energy"
echo
echo "Logs:    journalctl -u campus-energy -f"
echo "Restart: systemctl restart campus-energy"
echo "Verify:  $APP_DIR/venv/bin/python $APP_DIR/scripts/check_deployment.py http://$PUBLIC_IP"
