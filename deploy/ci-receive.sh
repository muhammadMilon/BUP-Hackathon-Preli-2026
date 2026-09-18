#!/usr/bin/env bash
# SSH forced command for the CI deploy key.
#
# setup.sh installs this as /opt/campus-energy/bin/ci-receive.sh and pins the
# CI public key to it in root's authorized_keys with `restrict,command=...`.
# Whatever the client asks to run, sshd runs this instead, so the key can do
# exactly one thing: hand over a release tarball on stdin.
#
#   git archive --format=tar.gz HEAD app samples scripts tests requirements.txt \
#     | ssh -i ci_key root@HOST "<commit-sha>"
#
# The requested "command" is used only as the release label.

set -euo pipefail

MAX_BYTES=$((50 * 1024 * 1024))
LABEL="$(printf '%s' "${SSH_ORIGINAL_COMMAND:-ci}" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)"

# One deploy at a time.
exec 9>/run/lock/campus-energy-deploy.lock
flock -w 300 9

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

head -c "$MAX_BYTES" | tar -xzf - -C "$TMP" --no-same-owner --no-same-permissions

/opt/campus-energy/bin/release.sh "$TMP" "${LABEL:-ci}"
