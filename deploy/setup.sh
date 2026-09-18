#!/usr/bin/env bash
# One-time provisioning on an Ubuntu 22.04 / 24.04 VPS. Idempotent.
#
#   bash deploy/setup.sh [server_name]
#
# server_name defaults to the VPS's public IPv4. Pass a domain instead if one
# points at the box.
#
# Safe on a host that already runs other sites: it adds one nginx site that
# only matches its own server_name, never removes or edits another site, only
# *reloads* nginx after `nginx -t` passes, and never enables or disables ufw.
#
# Optional: CI_DEPLOY_PUBKEY_FILE=/path/key.pub authorizes a GitHub Actions key
# that can do nothing except deliver a release (see deploy/ci-receive.sh).

set -euo pipefail

APP_DIR=/opt/campus-energy
APP_USER=campus
SERVICE=campus-energy
SITE=/etc/nginx/sites-available/campus-energy
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root (or with sudo)." >&2
    exit 1
fi

SERVER_NAME="${1:-$(curl -4fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)}"
if [[ -z "$SERVER_NAME" ]]; then
    echo "Could not detect the public IP; pass it explicitly: bash deploy/setup.sh 1.2.3.4" >&2
    exit 1
fi

echo "==> Installing missing system packages"
missing=()
for pkg in python3 python3-venv curl nginx; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-upgrade "${missing[@]}"
fi

echo "==> Service user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/releases" "$APP_DIR/bin"

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR/venv"
    runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/venv"
fi

echo "==> Environment file"
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
    echo "    !! $APP_DIR/.env created from the template -- add GROQ_API_KEY and GEMINI_API_KEY,"
    echo "    !! then: systemctl restart $SERVICE"
fi
# systemd EnvironmentFile does not understand comments after values.
sed -i -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' "$APP_DIR/.env"
chown root:"$APP_USER" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

echo "==> Deploy tooling"
install -o root -g root -m 755 "$SRC_DIR/deploy/release.sh"    "$APP_DIR/bin/release.sh"
install -o root -g root -m 755 "$SRC_DIR/deploy/ci-receive.sh" "$APP_DIR/bin/ci-receive.sh"

echo "==> systemd unit"
install -o root -g root -m 644 "$SRC_DIR/deploy/campus-energy.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"

echo "==> First release"
"$APP_DIR/bin/release.sh" "$SRC_DIR" setup

echo "==> nginx site for $SERVER_NAME"
if grep -RqsE "server_name[^;]*[[:space:]]${SERVER_NAME//./\\.}[[:space:];]" \
        /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ --exclude=campus-energy; then
    echo "!! Another nginx site already claims server_name $SERVER_NAME; not touching nginx." >&2
    exit 1
fi
NEW_SITE="$(mktemp)"
sed "s/__SERVER_NAME__/$SERVER_NAME/" "$SRC_DIR/deploy/nginx.conf" > "$NEW_SITE"
[[ -f "$SITE" ]] && cp "$SITE" "$SITE.prev"
install -o root -g root -m 644 "$NEW_SITE" "$SITE"
rm -f "$NEW_SITE"
ln -sfn "$SITE" /etc/nginx/sites-enabled/campus-energy
if ! nginx -t 2>/dev/null; then
    echo "!! nginx -t failed with the new site; restoring the previous state." >&2
    if [[ -f "$SITE.prev" ]]; then mv -f "$SITE.prev" "$SITE"; else rm -f "$SITE" /etc/nginx/sites-enabled/campus-energy; fi
    nginx -t
    exit 1
fi
rm -f "$SITE.prev"
systemctl reload nginx

if ufw status 2>/dev/null | grep -q "Status: active"; then
    echo "==> ufw is active; making sure HTTP is allowed"
    ufw allow 'Nginx Full' >/dev/null
fi

if [[ -n "${CI_DEPLOY_PUBKEY_FILE:-}" ]]; then
    echo "==> Authorizing the CI deploy key (forced command only)"
    KEY="$(awk 'NF>=2 {print $1, $2; exit}' "$CI_DEPLOY_PUBKEY_FILE")"
    AUTH=/root/.ssh/authorized_keys
    install -d -m 700 /root/.ssh
    touch "$AUTH" && chmod 600 "$AUTH"
    sed -i '/campus-energy-ci$/d' "$AUTH"
    echo "restrict,command=\"$APP_DIR/bin/ci-receive.sh\" $KEY campus-energy-ci" >> "$AUTH"
fi

echo
echo "---- through nginx ----"
curl -fsS -H "Host: $SERVER_NAME" http://127.0.0.1/health || echo "(nginx check failed)"
echo
echo
echo "Done."
echo "    health:  http://$SERVER_NAME/health"
echo "    main:    http://$SERVER_NAME/optimize-energy"
echo "    logs:    journalctl -u $SERVICE -f"
