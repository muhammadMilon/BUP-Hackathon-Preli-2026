# Deploying to a Hostinger VPS

Target: a fresh Hostinger VPS running **Ubuntu 22.04 or 24.04**. End state is
nginx on port 80 in front of three uvicorn workers managed by systemd, so the
judge can call `http://YOUR_VPS_IP/health` and `http://YOUR_VPS_IP/optimize-energy`.

Budget about 10 minutes.

## 1. Create the VPS

In hPanel → **VPS** → **Create**, pick any plan (1 vCPU / 4 GB is plenty),
choose the **Ubuntu 24.04** plain OS template (not a pre-built app template),
and set a root password or SSH key. Note the IPv4 address.

## 2. Upload the project

From your machine, in the project folder:

```bash
scp -r . root@YOUR_VPS_IP:/root/campus-energy
```

On Windows PowerShell the same command works if OpenSSH is installed. If `scp`
is awkward, push the repo to GitHub and clone it on the VPS instead:

```bash
ssh root@YOUR_VPS_IP
git clone https://github.com/YOUR_USER/YOUR_REPO.git /root/campus-energy
```

## 3. Run the setup script

```bash
ssh root@YOUR_VPS_IP
cd /root/campus-energy
bash deploy/setup.sh
```

It installs Python, nginx and ufw; creates a `campus` service user; builds a
virtualenv in `/opt/campus-energy/venv`; installs the systemd unit and the nginx
site; opens the firewall; and prints your public health URL.

## 4. Add the API keys

The first run creates `/opt/campus-energy/.env` from the template. Fill it in:

```bash
nano /opt/campus-energy/.env
```

```
XAI_API_KEY=xai-...................
XAI_MODEL=grok-4-fast
GEMINI_API_KEY=AIza...................
GEMINI_MODEL=gemini-2.5-flash
LLM_TIMEOUT_SECONDS=20
LOG_LEVEL=INFO
```

Then:

```bash
systemctl restart campus-energy
```

Keys: xAI at <https://console.x.ai>, Gemini at <https://aistudio.google.com/apikey>.
Both are needed for the graded path — Grok interprets, Gemini covers Grok
outages. The service still answers with the deterministic interpreter if both
are missing, but that forfeits the LLM requirement in Section 02.

## 5. Verify

```bash
curl http://YOUR_VPS_IP/health
# {"status":"ok"}

/opt/campus-energy/venv/bin/python /opt/campus-energy/scripts/check_deployment.py http://YOUR_VPS_IP
```

The checker posts every sample scenario and replays each response against the
full rule set — battery transitions, energy balance, directive compliance,
recalculated totals — and prints latency per case.

## 6. Submit

```
Health:  http://YOUR_VPS_IP/health
Main:    http://YOUR_VPS_IP/optimize-energy
```

## Pushing a code change later

```bash
scp -r . root@YOUR_VPS_IP:/root/campus-energy
ssh root@YOUR_VPS_IP "cd /root/campus-energy && bash deploy/update.sh"
```

`update.sh` keeps your `.env` and the virtualenv, reinstalls dependencies only
if `requirements.txt` changed, restarts, and waits for health.

## Operating

| Task              | Command                                     |
| ----------------- | ------------------------------------------- |
| Live logs         | `journalctl -u campus-energy -f`            |
| Last 100 lines    | `journalctl -u campus-energy -n 100`        |
| Restart           | `systemctl restart campus-energy`           |
| Status            | `systemctl status campus-energy`            |
| nginx logs        | `tail -f /var/log/nginx/campus-energy.*.log` |
| Reload nginx      | `nginx -t && systemctl reload nginx`        |

## Optional: HTTPS on a domain

Only if you point a domain at the VPS. Plain HTTP on the IP is acceptable for
the round.

```bash
apt install -y certbot python3-certbot-nginx
sed -i 's/server_name _;/server_name your-domain.com;/' /etc/nginx/sites-available/campus-energy
nginx -t && systemctl reload nginx
certbot --nginx -d your-domain.com
```

## Troubleshooting

**`curl http://YOUR_VPS_IP/health` times out** — check Hostinger's own firewall
in hPanel (VPS → Firewall) allows inbound TCP 80, then `ufw status` on the box.

**502 Bad Gateway** — uvicorn is down: `journalctl -u campus-energy -n 50`. A
malformed `.env` line is the usual cause; every line must be `KEY=value` with no
quotes needed.

**Responses say `provider=rules`** — the API keys are missing or rejected. The
`plan_summary` carries the reason (`grok unavailable: ...`), and so does the
log. Fix the key and restart.

**Slow first request** — the model call dominates. Repeated note sets are cached
in-process, and `LLM_TIMEOUT_SECONDS` bounds the wait before falling through to
Gemini and then to the rules.
