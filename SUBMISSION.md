# Submission checklist & video script

Everything the Participant Guide asks for, in the order you should do it on the
night. Items marked **YOU** need a human — an account, an upload, a recording.

## The five required deliverables

| # | Deliverable | Status |
| - | ----------- | ------ |
| 1 | Working public endpoint | **YOU** — run `deploy/setup.sh` on the Hostinger VPS (see [DEPLOY.md](DEPLOY.md)) |
| 2 | GitHub repository, private during the event, public after the deadline | **YOU** — create it *after* question reveal, push this code |
| 3 | README & configuration | Done — [README.md](README.md) |
| 4 | Docker fallback image, pullable tag/digest | **YOU** — `bash deploy/docker_publish.sh docker.io/YOUR_USER/campus-energy v1` |
| 5 | 3-minute architecture video | **YOU** — script below |

## Order of operations on the night

```bash
# 1. Create the private GitHub repo (after question reveal), then:
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main

# 2. Deploy to the VPS
scp -r . root@YOUR_VPS_IP:/root/campus-energy
ssh root@YOUR_VPS_IP "cd /root/campus-energy && bash deploy/setup.sh"
ssh root@YOUR_VPS_IP "nano /opt/campus-energy/.env && systemctl restart campus-energy"

# 3. Verify from your own machine, not the VPS
curl http://YOUR_VPS_IP/health
python scripts/check_deployment.py http://YOUR_VPS_IP

# 4. Publish the Docker fallback (on the VPS, which has Docker, or locally)
bash deploy/docker_publish.sh docker.io/YOUR_USER/campus-energy v1

# 5. Record and upload the video, then submit all five items.
```

## Pre-submit verification

Run this and confirm every line passes:

```bash
python tests/run_all.py
```

| Guide requirement | How it is verified |
| ----------------- | ------------------ |
| `/health` reachable, correct body | `test_spec_compliance.py`, `check_deployment.py` |
| `/optimize-energy` exact request/response contract | `test_spec_compliance.py` (40 assertions) |
| One entry per note, `note_index` order, `applies` semantics | `test_spec_compliance.py`, `test_official_samples.py` |
| Only supported directive types; guardrails reject the rest | `test_stress.py` — 12 hostile model replies |
| Hours unique, ascending, 0–23 | `test_spec_compliance.py` |
| Directives applied downstream, not just extracted | `test_spec_compliance.py`, `validate.replay` |
| Energy balance, battery bounds, rate limits, neutrality | `validate.replay` on every response |
| Totals match recalculation from `hourly_plan` | `test_spec_compliance.py` |
| Malformed JSON / bad model output does not crash | `test_spec_compliance.py`, `test_llm_chain.py` |
| Reachable from outside the dev environment | `scripts/check_deployment.py <public URL>` |
| Matches the organizer's reference optimum | `test_official_samples.py` — SAMPLE-01 ratio 1.0000 |

**Before you submit, confirm by hand:**

- [ ] `curl http://YOUR_VPS_IP/health` works **from a phone on mobile data**, not just your laptop
- [ ] `plan_summary` says `via groq` (or `via gemini`), not `via rules` — if it says rules, the API key is wrong
- [ ] No `.env`, key, or token is in the repo: `git log -p | grep -iE "gsk_|AIza|xai-"` returns nothing
- [ ] The GitHub repo is set to public **after** the deadline
- [ ] The Docker image is still pullable from a machine that never built it

## 3-minute video script

Tie-break only — no base points — so keep it technical and unedited. Screen
recording with voiceover is fine. Target 2:45.

### 0:00–0:25 — The problem

> Campus energy scheduling with a twist: operators send free-text notes like
> "panel washing from one until three will leave roughly one-fifth of normal
> solar output." The service has to understand that sentence, turn it into a
> machine-checkable directive, and produce a 24-hour schedule that actually
> obeys it — at minimum cost. Notes can also be distractors that must be ignored.

### 0:25–1:15 — Architecture

Show the diagram from the README.

> Three stages, deliberately separated.
>
> **Interpretation** is the language model's job: Groq primary, Gemini as
> fallback. It returns structured JSON, one entry per note.
>
> **Guardrails** treat that output as untrusted data. Unknown directive types,
> out-of-range hours, non-finite numbers, a reserve above battery capacity,
> `applies=false` on a real directive — all rejected. Unambiguous problems get
> repaired: unsorted hours, or a factor written as `20` when it means `0.2`. If
> one entry can't be salvaged, only that note falls back to the regex reader —
> the rest of the response is unaffected.
>
> **Optimization** is a linear program. Four variables per hour: grid, solar
> used, charge, discharge.

### 1:15–2:00 — Why an LP, and why directives go in *before* the solve

> Every directive becomes part of the model, not a patch afterwards. A
> `solar_reduction` scales the solar upper bound; a `no_charge_window` sets the
> charge bound to zero; `minimum_battery_reserve` raises the floor on the
> running state of charge; `max_grid_window` caps the grid variable. End-of-day
> neutrality is a single equality row.
>
> That matters because the rubric scores directive application separately from
> interpretation. A cheap schedule built on an ignored directive scores zero for
> that case. Because the constraints are in the model, the LP optimum *is* the
> cheapest compliant schedule — it can't trade compliance for cost.
>
> On the organizer's public SAMPLE-01, our cost matches their reference optimum
> exactly: 38,365 BDT, ratio 1.0.

### 2:00–2:30 — Verification

> We wrote an independent replay of the finished response — it re-derives
> effective solar, the battery trajectory, the balance equation and the
> aggregates from the request alone, and the service runs it on its own output
> before answering. The same code powers the test suites.
>
> Show `python tests/run_all.py`: interpretation 22/22, spec compliance 40/40,
> 250 randomised scenarios with zero rule violations, and the provider chain
> tested against mocked Groq and Gemini failures.

### 2:30–2:50 — Running it

> Deployed on a Hostinger VPS: nginx in front of three uvicorn workers under
> systemd. Locally it's `pip install -r requirements.txt` and one uvicorn
> command. Docker fallback image is published and starts with no environment
> variables at all — health check still passes.

### Things to show on screen

1. The architecture diagram (README "How a request flows")
2. `app/optimizer.py` — the LP docstring with the formulation
3. `app/directives.py` — `normalize_entry`, the guardrail function
4. A live `curl` against the deployed VPS returning a real plan
5. `python tests/run_all.py` finishing green

### Do not

- Spend time on slide design — it is scored on technical clarity only
- Show any API key on screen (check your terminal scrollback and `.env`)
- Go over 3:00 — it is a hard limit
