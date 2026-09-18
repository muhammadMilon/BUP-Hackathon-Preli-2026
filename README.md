# Smart Campus Energy Optimization

[![CI/CD](https://github.com/muhammadMilon/BUP-Hackathon-Preli-2026/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/muhammadMilon/BUP-Hackathon-Preli-2026/actions/workflows/ci-cd.yml)

LLM-assisted operator-directive interpretation and 24-hour energy scheduling for
the BUP CSE Fest 2026 hackathon preliminary round.

| | |
| --- | --- |
| **Health** | `http://82.112.237.249/health` |
| **Main endpoint** | `POST http://82.112.237.249/optimize-energy` |
| **Docker fallback** | `docker pull ghcr.io/muhammadmilon/bup-hackathon-preli-2026:latest` |
| **Official samples** | 10/10 interpretations correct, cost equal to the reference optimum on all 10 (ratio 1.0000) |

The service reads short natural-language operator notes, converts the relevant
ones into structured directives, validates those directives with deterministic
guardrails, folds them into a linear program, and returns the cheapest 24-hour
schedule that satisfies every energy, battery and directive rule.

## Endpoints

| Method | Path               | Purpose                                              |
| ------ | ------------------ | ---------------------------------------------------- |
| GET    | `/health`          | Readiness probe. Returns `{"status": "ok"}`.          |
| POST   | `/optimize-energy` | Interpretation + 24-hour plan for one scenario.       |
| GET    | `/`                | Service banner (not exercised by the judge).          |

Status codes: `200` success, `400` malformed JSON or structurally invalid
request, `500` controlled internal error with no stack trace in the body.

## How a request flows

```
request ──► pydantic schema validation            400 on anything malformed
        ──► interpret notes   Groq ──► Gemini ──► regex rules
        ──► guardrails        untrusted model output is repaired or rejected
                              per note, never silently invented
        ──► constraints       effective solar, reserve floors, grid caps,
                              charge/discharge blackouts
        ──► linear program    HiGHS, exact cost minimum
        ──► final replay      independent re-verification of our own answer
        ──► response
```

### Interpretation tiers

1. **Groq** — primary. OpenAI-compatible chat completions in JSON mode. Tries
   `GROQ_MODEL`, then `llama-3.3-70b-versatile`, `openai/gpt-oss-120b`,
   `llama-3.1-8b-instant`, moving on only when a model id is rejected.
2. **xAI Grok** — optional extra tier, active only when `XAI_API_KEY` is set.
   (Note: xAI's *Grok* and *Groq* are unrelated companies.)
3. **Gemini (Google)** — used when the tiers above error, time out, or return
   unusable JSON. Constrained by a `responseSchema`.
4. **Deterministic regex rules** — `app/directives.py`. Never fails, so a
   provider outage costs accuracy rather than the whole response. Currently
   22/22 on the worked examples and paraphrases in `tests/test_interpret.py`.

One further deterministic check runs on top: a `solar_reduction` aimed at hours
whose forecast solar is zero cannot be what an operator meant, so when the
regex reader finds a daylight window for the same note we take its hours and
keep the model's factor. This catches the one failure mode observed in live
testing — a bare "from one until three" read as 01:00 rather than 13:00, which
Groq got wrong 4 times in 10 before the check and 0 times in 10 after it.

A model reply is treated as untrusted data. `normalize_entry` repairs what is
unambiguously repairable (unsorted or duplicated hours, `20` written for a
`0.2` factor, a missing explanation) and rejects the rest — unknown directive
types, out-of-range hours, non-finite numbers, reserves above capacity,
`applies=false` on a real directive, `no_op` carrying an adjustment. A rejected
entry falls back to the rule-based reading of *that one note*; the rest of the
response is unaffected.

### The optimizer

Four variables per hour — grid, solar used, charge, discharge — solved as a
linear program with SciPy/HiGHS:

```
minimise   Σ tariff[h] · grid[h]
s.t.       grid[h] + solar_used[h] + discharge[h] − charge[h] = demand[h]
           0 ≤ solar_used[h] ≤ effective_solar[h]
           0 ≤ charge[h] ≤ max_charge          (0 in a no_charge window)
           0 ≤ discharge[h] ≤ max_discharge    (0 in a no_discharge window)
           0 ≤ grid[h] ≤ max_grid[h]           (where a cap applies)
           reserve[h] ≤ initial + Σ_{k≤h}(charge[k] − discharge[k]) ≤ capacity
           Σ charge[h] = Σ discharge[h]        (end-of-day neutrality)
```

Because every directive is folded into the bounds and the reserve vector before
the solve, the LP optimum is by construction the cheapest *compliant* schedule —
not a cheap schedule that is patched afterwards. Overlapping directives compose
conservatively: solar factors multiply, reserve floors take the maximum, grid
caps take the minimum.

If an extracted directive set is genuinely impossible, the solver relaxes only
directive-imposed constraints, in order, and says so in `plan_summary`. Base
GridWise rules are never relaxed.

### Self-verification

`app/validate.py` is an independent replay of the finished response — it
re-derives effective solar, the battery trajectory, the balance equation and
the aggregates from the request alone. The service runs it on its own output
before responding and keeps the conservative fallback plan only when that plan
actually breaks fewer rules.

## Quickstart from a clean machine

Requires **Python 3.10+** and nothing else. Copy-paste the whole block:

```bash
git clone <REPO_URL> campus-energy
cd campus-energy

python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
#   .venv\Scripts\Activate.ps1

pip install -r requirements.txt

cp .env.example .env          # Windows: copy .env.example .env
# Open .env and paste your GROQ_API_KEY and GEMINI_API_KEY (see Configuration)

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The service is ready as soon as uvicorn prints `Application startup complete`
(typically under two seconds). In a second terminal:

**1. Health check**

```bash
curl http://localhost:8000/health
```

```json
{"status":"ok"}
```

**2. A public sample case**

```bash
python -c "import json;print(json.dumps(json.load(open('samples/public_cases.json'))[0]))" > case1.json
curl -X POST http://localhost:8000/optimize-energy \
     -H "Content-Type: application/json" \
     --data-binary @case1.json
```

Expected shape (abridged — `hourly_plan` has all 24 entries):

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
     "explanation": "Usable solar falls to 20% of the forecast during these hours."},
    {"note_index": 1, "applies": true, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]},
     "explanation": "Battery charging is unavailable during these hours."},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null,
     "explanation": "This note does not affect today's energy schedule."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 180.0, "solar_used_kwh": 0.0,
     "battery_action": "idle", "battery_kwh": 0.0, "battery_energy_after_kwh": 200.0}
  ],
  "total_grid_kwh": 5215.0,
  "total_cost_bdt": 54359.0,
  "peak_grid_kwh": 359.0,
  "plan_summary": "Interpreted 3 operator note(s) via groq ..."
}
```

**3. Or use the helper script / the browser**

```bash
python scripts/try_case.py 1          # posts a sample case, prints it readably
python scripts/try_case.py 1 --full   # all 24 plan rows
python scripts/try_case.py SAMPLE-01  # a case from the organizer pack
```

Interactive API docs with a prefilled request body, ready to edit and send:
<http://localhost:8000/docs>

**4. Verify it against the organizer's own reference answer**

```bash
python tests/test_official_samples.py
```

```
OK   SAMPLE-01  interp 2/2  cost  38,365.00 vs reference  38,365.00  [MATCH]  ratio 1.0000
...
OK   SAMPLE-10  interp 3/3  cost  41,620.00 vs reference  41,620.00  [MATCH]  ratio 1.0000
optimization quality: 1.0000 average ratio -> 10.00/10 rubric points
official samples: 0 failure(s) over 10 case(s)
```

Without API keys the service still runs and answers correctly through the
deterministic interpreter — `plan_summary` will then say `via rules` instead of
`via groq`.

## Tests

```bash
python tests/run_all.py                  # everything below, in order
```

| Suite                     | Covers                                                          |
| ------------------------- | --------------------------------------------------------------- |
| `test_interpret.py`       | note → directive accuracy on the worked examples and paraphrases |
| `test_llm_chain.py`       | Groq success, Gemini fallback, rules fallback, guardrail repair  |
| `test_spec_compliance.py` | 40 clause-by-clause assertions against a live response           |
| `test_end_to_end.py`      | sample scenarios through the real endpoint, replayed             |
| `test_stress.py`          | 250 randomised scenarios + deliberately hostile model output      |

All run without API keys — the provider chain is exercised with a mocked HTTP
transport, and the rest run with `LLM_DISABLED=1`. Unset that to test against
live Groq/Gemini.

Against a deployed URL:

```bash
python scripts/check_deployment.py http://82.112.237.249
```

## Configuration

| Variable              | Default            | Purpose                                      |
| --------------------- | ------------------ | -------------------------------------------- |
| `GROQ_API_KEY`             | —                          | Groq credentials (primary interpreter).         |
| `GROQ_MODEL`               | `llama-3.3-70b-versatile`  | Preferred Groq model id.                        |
| `GEMINI_API_KEY`           | —                          | Gemini credentials (fallback interpreter).      |
| `GEMINI_MODEL`             | `gemini-2.5-flash`         | Preferred Gemini model id.                      |
| `XAI_API_KEY`              | unset                      | Optional. Adds an xAI Grok tier after Groq.     |
| `XAI_MODEL`                | `grok-4-fast`              | Preferred xAI model id.                         |
| `LLM_TIMEOUT_SECONDS`      | `8`                        | Per-call HTTP timeout.                          |
| `LLM_TOTAL_BUDGET_SECONDS` | `15`                       | Budget for the whole interpretation stage.      |
| `LLM_DISABLED`             | unset                      | `1` skips every provider (tests only).          |
| `LOG_LEVEL`                | `INFO`                     | Standard logging level.                         |

Identical note sets are cached in-process, so a repeated scenario skips the
model call entirely.

### Model / provider disclosure

| Role | Provider | Model identifier | Endpoint |
| ---- | -------- | ---------------- | -------- |
| Primary interpreter | Groq | `llama-3.3-70b-versatile`, falling back to `openai/gpt-oss-120b`, `llama-3.1-8b-instant` | `https://api.groq.com/openai/v1/chat/completions` (OpenAI-compatible, JSON mode) |
| Optional extra tier | xAI (Grok) | `grok-4-fast`, falling back to `grok-3-mini`, `grok-2-1212` | `https://api.x.ai/v1/chat/completions` — only active when `XAI_API_KEY` is set |
| Fallback interpreter | Google | `gemini-2.5-flash`, falling back to `gemini-2.0-flash` | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` (constrained `responseSchema`) |
| Optimizer | — (local) | SciPy `linprog`, HiGHS backend | in-process |

Get keys at <https://console.groq.com/keys> and
<https://aistudio.google.com/apikey> — both free, neither needs a payment card.
**Groq and Grok are different companies**: a `gsk_...` key from Groq will not
work as `XAI_API_KEY`, and an `xai-...` key will not work as `GROQ_API_KEY`.

### Latency budget

The rubric scores p95 latency and treats anything past 30 s as a failure, so the
interpretation stage is bounded twice: `LLM_TIMEOUT_SECONDS` caps a single
provider call, and `LLM_TOTAL_BUDGET_SECONDS` caps Groq + its model retries +
Gemini combined. When the budget runs out the deterministic interpreter answers
instead, so a slow provider costs accuracy, never a timeout. The LP solve itself
is ~2 ms; a full request with no model call measures ~65 ms end to end.

### Secret handling

`.env` is gitignored and excluded from the Docker image via `.dockerignore`.
Keys are read from the environment only — never committed, never logged, never
included in an API response. Error paths return a short message (`{"error":
"Internal error while optimizing the scenario"}`) with the stack trace going to
the server log only. `deploy/docker_publish.sh` refuses to push an image that
contains a `.env` file.

## Deploying

Production is a Hostinger VPS: nginx on port 80 in front of three uvicorn
workers under systemd, shipped by GitHub Actions on every push to `main`.

```
push ─► test (6 suites) ─┬─► docker: build, keyless smoke test, publish to GHCR
                         └─► deploy: new release on the VPS ─► health check
                                     (auto-rollback on failure) ─► public replay check
```

Releases are atomic (a symlink flip) and the previous five are kept for
rollback. The CI key can only deliver a release tarball: it is pinned to a
forced command server-side, so it has no shell on the host. **[DEPLOY.md](DEPLOY.md)**
covers the layout, provisioning a fresh server, and operations.

### Docker fallback image

Published by CI after the image passes a keyless smoke test:

```bash
docker pull ghcr.io/muhammadmilon/bup-hackathon-preli-2026:latest
docker run -d -p 8000:8000 \
  -e GROQ_API_KEY=... -e GEMINI_API_KEY=... \
  ghcr.io/muhammadmilon/bup-hackathon-preli-2026:latest
curl http://localhost:8000/health
```

Exposed port **8000**, bound to `0.0.0.0`, running as a non-root user with a
built-in `HEALTHCHECK`. Required environment variable names are `GROQ_API_KEY`
and `GEMINI_API_KEY`; everything else has a working default. The container is
functional without any keys (deterministic interpreter).

To publish to another registry, `bash deploy/docker_publish.sh <registry/user/name> <tag>`
runs the same checks locally and refuses to push an image that contains a `.env`.

## Dependencies

| Package          | Version  | Why                                              |
| ---------------- | -------- | ------------------------------------------------ |
| `fastapi`        | 0.115.6  | HTTP routing and request handling                |
| `uvicorn`        | 0.34.0   | ASGI server                                      |
| `pydantic`       | 2.10.4   | Strict request/response schema validation        |
| `httpx`          | 0.28.1   | Async HTTP client for the Groq and Gemini calls  |
| `scipy`          | 1.15.0   | `linprog` / HiGHS — the optimizer                |
| `numpy`          | 2.2.1    | Constraint matrix assembly                       |
| `python-dotenv`  | 1.0.1    | Loads `.env` in local development                |

No other runtime dependency. Development used `pyflakes` for linting; the test
suites use only the standard library plus `fastapi.testclient`.

**Credits.** FastAPI (Sebastián Ramírez), Uvicorn (Encode), Pydantic, httpx
(Encode), SciPy/HiGHS, NumPy, python-dotenv. Language models: Groq and Google
Gemini (optionally xAI Grok), called over their public HTTP APIs. Claude Code was used as an
AI coding assistant during development; the architecture, the guardrail design,
the LP formulation and the test strategy are the team's own.

## Known limitations

- **Overlapping solar directives compound.** Two `solar_reduction` windows on
  the same hour multiply their factors. The spec does not define this case; the
  product is the conservative choice, since using *less* solar can never make a
  schedule invalid. Overlapping reserves take the maximum and overlapping grid
  caps take the minimum, both per the spec's "most restrictive wins" reading.
- **A relevant note with no stated time window applies to all 24 hours.** There
  is no other defensible default, but a note meaning "for the next hour" would
  be over-applied.
- **Bare clock times are genuinely ambiguous.** The prompt and the daylight
  check both push "from one until three" toward the afternoon, matching the
  Problem Statement's worked example. A note truly meaning 1 AM would be
  misread — but for solar that reading is meaningless anyway.
- **The deterministic fallback is weaker than the LLM** on unusual paraphrases.
  It exists to keep the service answering during a provider outage, not to match
  the model's coverage. It scores 22/22 on the published examples and their
  paraphrases, but hidden wording could fall outside its patterns.
- **No grid export.** Surplus solar is curtailed, per Section 9.4.
- **In-process cache only.** Running multiple workers means each has its own
  cache; this affects latency slightly, never correctness.
- **If the extracted directives are mutually impossible**, the solver relaxes
  directive constraints in a fixed order and says so in `plan_summary`. Base
  GridWise rules are never relaxed. Organizer scoring scenarios are guaranteed
  feasible, so this path should not trigger during judging.

## Rules compliance

| Spec clause                                          | Where it is enforced                                   |
| ---------------------------------------------------- | ------------------------------------------------------ |
| §02 LLM in the interpretation path                    | `llm.py` — Groq, then Gemini; rules only as a safety net |
| §04 six directive types, exact adjustment shapes      | `directives.SHAPE`, `normalize_entry`                   |
| §05.1 one entry per note, in `note_index` order       | `llm._align`                                            |
| §05.1 exclusive end hour ("1 PM to 3 PM" → `[13,14]`) | `timeparse.extract_hours`                               |
| §05.1 `applies=false` only for `no_op`                | `normalize_entry`                                       |
| §05.1 factor is the remaining fraction                | prompt + `directives._pct`                              |
| §05.2 minimise grid cost                              | `optimizer._solve_lp` objective                         |
| §05.3 directive → constraint mapping                  | `optimizer.Constraints`                                 |
| §06 endpoint names, 200/400/500 codes                 | `main.py`                                               |
| §08 guardrails on untrusted model output              | `normalize_entry`, per-note fallback                    |
| §08 final replay of the finished schedule             | `validate.replay`, called before responding             |
| §09 battery state, bounds, rate limits, balance       | LP constraints + `validate.replay`                      |
| §09.6 end-of-day battery neutrality                   | LP equality row + drift correction in `_finalize`       |
| §10 response schema                                   | `main.py`, verified by `tests/test_spec_compliance.py`  |
| §11.3 aggregates recomputed from `hourly_plan`        | `main._apply_plan`                                      |
| §11.5 0.01 tolerance                                  | `validate.TOL`                                          |

`tests/test_spec_compliance.py` asserts 40 of these clauses directly against a
live response and prints the section number for each.

From the **Participant Guide & Evaluation Rubric**:

| Rubric category | Points | How this submission addresses it |
| --------------- | -----: | -------------------------------- |
| LLM Directive Interpretation | 25 | Groq → Gemini chain; relevance/type/hours/values all machine-checked against the reference pack; paraphrase robustness comes from the model, with the regex tier as backup |
| Directive Application & Constraint Correctness | 25 | Directives become LP constraints *before* the solve, then `validate.replay` re-verifies the finished plan |
| Optimization Quality | 10 | Exact LP optimum. On all 10 official samples our cost equals the organizer's reference optimum (ratio 1.0000) |
| API Contract & Schema | 10 | Strict Pydantic models; 400 on malformed/invalid; `test_spec_compliance.py` |
| Performance & Reliability | 10 | ~65 ms without a model call; bounded LLM budget so p95 stays low and nothing can reach the 30 s timeout; no 5xx on malformed input; no secrets in logs or responses |
| Deployment & Docker Fallback | 10 | Live on a VPS with CI/CD, atomic releases and auto-rollback; CI publishes the Docker image only after a keyless `/health` + `/optimize-energy` smoke test and a no-`.env` check |
| Documentation & Local Reproducibility | 10 | This README: clean quickstart, env var names, model/provider table, solver disclosure, sample test command with expected output, dependencies, limitations, secret handling |

## Layout

```
app/
  main.py         FastAPI endpoints, response assembly, self-check
  llm.py          Groq -> Gemini -> rules interpretation chain
  directives.py   guardrails + deterministic interpreter
  timeparse.py    natural-language time windows (exclusive end hour)
  optimizer.py    constraint assembly and the linear program
  validate.py     independent replay of a finished response
  schemas.py      strict request/response models
samples/          public and official sample scenarios
scripts/          deployment smoke test
tests/            interpretation, end-to-end and stress suites
deploy/           VPS provisioning, atomic releases, systemd unit, nginx site
.github/workflows CI/CD: test -> Docker image -> deploy
```
