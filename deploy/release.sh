#!/usr/bin/env bash
# Install a source tree as a new release, switch to it, and roll back if the
# new release does not pass its health check.
#
#   bash deploy/release.sh <source_dir> [label]
#
# Layout under /opt/campus-energy:
#   releases/<utc-time>-<label>/   immutable, root-owned code
#   current -> releases/...        what systemd runs
#   venv/                          shared virtualenv, rebuilt only when
#                                  requirements.txt changes
#   .env                           secrets, never touched by a release
#
# setup.sh installs this script as /opt/campus-energy/bin/release.sh, which is
# the copy CI deploys run.

set -euo pipefail

APP_DIR=/opt/campus-energy
APP_USER=campus
SERVICE=campus-energy
HEALTH_URL=http://127.0.0.1:8000/health
KEEP=5

SRC="${1:?usage: release.sh <source_dir> [label]}"
LABEL="$(printf '%s' "${2:-manual}" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)"

if [[ $EUID -ne 0 ]]; then
    echo "Run this as root." >&2
    exit 1
fi
for need in app requirements.txt; do
    if [[ ! -e "$SRC/$need" ]]; then
        echo "Not a release tree: $SRC/$need is missing." >&2
        exit 1
    fi
done

ID="$(date -u +%Y%m%d%H%M%S)-${LABEL:-manual}"
REL="$APP_DIR/releases/$ID"

echo "==> Staging release $ID"
mkdir -p "$REL"
cp -r "$SRC/app" "$SRC/requirements.txt" "$REL/"
for extra in samples scripts tests; do
    [[ -d "$SRC/$extra" ]] && cp -r "$SRC/$extra" "$REL/"
done
find "$REL" -name '__pycache__' -prune -exec rm -rf {} +
# Code is root-owned and read-only to the service user.
chown -R root:root "$REL"
chmod -R u=rwX,go=rX "$REL"

REQ_HASH="$(sha256sum "$REL/requirements.txt" | cut -d' ' -f1)"
if [[ "$(cat "$APP_DIR/venv/.requirements.sha256" 2>/dev/null)" != "$REQ_HASH" ]]; then
    echo "==> Installing dependencies"
    runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
    runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$REL/requirements.txt"
    echo "$REQ_HASH" > "$APP_DIR/venv/.requirements.sha256"
    chown "$APP_USER":"$APP_USER" "$APP_DIR/venv/.requirements.sha256"
fi

echo "==> Import check"
(cd "$REL" && LLM_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 \
    runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/python" -c "import app.main")

PREV="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"

switch_to() {
    ln -sfn "$1" "$APP_DIR/current.next"
    mv -Tf "$APP_DIR/current.next" "$APP_DIR/current"
    systemctl restart "$SERVICE"
}

healthy() {
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

echo "==> Switching to $ID"
switch_to "$REL"

if ! healthy; then
    echo "!! $ID failed its health check. Recent logs:" >&2
    journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true
    if [[ -n "$PREV" && -d "$PREV" && "$PREV" != "$REL" ]]; then
        echo "!! Rolling back to $(basename "$PREV")" >&2
        switch_to "$PREV"
        healthy && echo "!! Rollback healthy." >&2
    fi
    rm -rf "$REL"
    exit 1
fi

echo "==> Healthy: $(curl -fsS "$HEALTH_URL")"

# Keep the newest $KEEP releases for rollback; never prune the live one.
LIVE="$(readlink -f "$APP_DIR/current")"
ls -1dt "$APP_DIR"/releases/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    [[ "$(readlink -f "$old")" == "$LIVE" ]] || rm -rf "$old"
done

echo "==> Live release: $ID"
