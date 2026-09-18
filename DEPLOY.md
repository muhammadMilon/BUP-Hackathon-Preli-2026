# Deployment

Production runs on a Hostinger VPS (Ubuntu 24.04) that also hosts other,
unrelated production sites. The deployment is built around that fact: it adds
exactly one systemd service and one nginx site, and never edits anything else.

```
                 ┌──────────────────────── VPS ────────────────────────┐
 judge ──HTTP──► │ nginx :80  server_name <VPS IP>                     │
                 │   └─► 127.0.0.1:8000  uvicorn × 3  (systemd,        │
                 │                        user "campus", read-only fs) │
                 │ other sites: matched by their own domain names,     │
                 │              untouched                              │
                 └─────────────────────────────────────────────────────┘
```

| Endpoint | URL |
| -------- | --- |
| Health   | `http://82.112.237.249/health` |
| Main     | `http://82.112.237.249/optimize-energy` |
| Docs     | `http://82.112.237.249/docs` |

## Continuous delivery

Every push to `main` runs [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml):

1. **Test** — all six suites (`tests/run_all.py`) on Python 3.12.
2. **Docker** — builds the image, starts it with *no* environment variables,
   checks `/health` and a real `/optimize-energy` call, confirms no `.env` is
   inside, then publishes `ghcr.io/muhammadmilon/bup-hackathon-preli-2026`.
3. **Deploy** — only after the tests pass. Streams a `git archive` of the
   commit to the VPS, where it becomes a new release; then runs
   `scripts/check_deployment.py` against the public URL, which replays every
   response against the full rule set.

Pull requests run steps 1–2 only.

### How a release lands

```
/opt/campus-energy/
├── current -> releases/20260918150053-8ab3685   what systemd runs
├── releases/                                    last 5 kept for rollback
├── venv/                                        rebuilt only when requirements.txt changes
├── bin/release.sh, bin/ci-receive.sh            root-owned deploy tooling
└── .env                                         API keys; never shipped by CI
```

[`deploy/release.sh`](deploy/release.sh) stages the code as a new root-owned
release, installs dependencies if they changed, import-checks it as the service
user, flips the `current` symlink and restarts. If `/health` does not answer
within 30 s it **switches back to the previous release automatically** and the
CI job fails.

### Why CI cannot hurt the other sites

The GitHub Actions key is installed in `authorized_keys` as

```
restrict,command="/opt/campus-energy/bin/ci-receive.sh" ssh-ed25519 … campus-energy-ci
```

so whatever the client asks for, sshd runs [`ci-receive.sh`](deploy/ci-receive.sh):
no shell, no port forwarding, no PTY. It reads one size-capped tarball from
stdin and hands it to `release.sh`. Deploys are serialized with `flock`.

The service itself runs as an unprivileged user with `ProtectSystem=strict`,
`ProtectHome`, `NoNewPrivileges` and a 2 GB memory ceiling.

### Repository settings used by the workflow

| Kind     | Name              | Value |
| -------- | ----------------- | ----- |
| Variable | `VPS_HOST`        | VPS IPv4 |
| Secret   | `VPS_SSH_KEY`     | private half of the CI deploy key |
| Secret   | `VPS_KNOWN_HOSTS` | `ssh-keyscan -t ed25519 <VPS IP>` output |

## Provisioning a server from scratch

```bash
# on your machine
ssh-keygen -t ed25519 -N "" -C campus-energy-ci -f ci_key
scp .env ci_key.pub root@VPS_IP:/root/
git archive --format=tar.gz HEAD | ssh root@VPS_IP \
  "mkdir -p /root/campus-energy && tar -xzf - -C /root/campus-energy"

# on the VPS
install -D -m 640 /root/.env /opt/campus-energy/.env
CI_DEPLOY_PUBKEY_FILE=/root/ci_key.pub bash /root/campus-energy/deploy/setup.sh VPS_IP
```

[`deploy/setup.sh`](deploy/setup.sh) is idempotent. It installs only the
packages that are missing (never upgrades existing ones), creates the `campus`
user and virtualenv, installs the systemd unit and the deploy tooling, performs
the first release, and adds the nginx site. It refuses to continue if another
site already claims the same `server_name`, restores the previous state if
`nginx -t` fails, and only ever *reloads* nginx. If ufw is active it makes sure
HTTP is allowed; it never enables or disables the firewall.

Then add the three repository settings above and push.

## Operating

| Task                  | Command |
| --------------------- | ------- |
| Live logs             | `journalctl -u campus-energy -f` |
| Status                | `systemctl status campus-energy` |
| Active release        | `readlink /opt/campus-energy/current` |
| Manual rollback       | `ln -sfn /opt/campus-energy/releases/<id> /opt/campus-energy/current && systemctl restart campus-energy` |
| Change an API key     | edit `/opt/campus-energy/.env`, then `systemctl restart campus-energy` |
| nginx logs            | `tail -f /var/log/nginx/campus-energy.*.log` |
| Verify from anywhere  | `python scripts/check_deployment.py http://82.112.237.249` |

## Troubleshooting

**Responses say `via rules`** — the API keys are missing or rejected. The
`plan_summary` carries the reason (`groq unavailable: ...`), and so does the
log. Fix `.env` and restart.

**502 Bad Gateway** — uvicorn is down: `journalctl -u campus-energy -n 50`.
A malformed `.env` line is the usual cause; every line must be `KEY=value`.

**Deploy job fails at "Ship release"** — the release script's output is in the
job log; the previous release is still live.
