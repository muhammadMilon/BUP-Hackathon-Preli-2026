"""Point this at a deployed URL to verify it the way the judge harness will.

    python scripts/check_deployment.py https://your-service.onrender.com

Checks /health, posts every public sample case, replays each response against
the rules, and reports latency. Exit code is non-zero if anything fails.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.schemas import OptimizeRequest  # noqa: E402
from app.validate import replay  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "public_cases.json")


def main(base_url: str) -> int:
    base_url = base_url.rstrip("/")
    failures = 0

    with httpx.Client(timeout=60.0) as client:
        started = time.perf_counter()
        try:
            health = client.get(base_url + "/health")
        except httpx.HTTPError as exc:
            print("FAIL  /health unreachable:", exc)
            return 1
        elapsed = (time.perf_counter() - started) * 1000
        ok = health.status_code == 200 and health.json().get("status") == "ok"
        print(("OK   " if ok else "FAIL "), "GET /health -> {} in {:.0f} ms".format(health.status_code, elapsed))
        failures += not ok

        for payload in json.load(open(SAMPLES, encoding="utf-8")):
            started = time.perf_counter()
            try:
                resp = client.post(base_url + "/optimize-energy", json=payload)
            except httpx.HTTPError as exc:
                print("FAIL ", payload["scenario_id"], "request failed:", exc)
                failures += 1
                continue
            elapsed = (time.perf_counter() - started) * 1000

            if resp.status_code != 200:
                print("FAIL ", payload["scenario_id"], "HTTP", resp.status_code, resp.text[:200])
                failures += 1
                continue

            body = resp.json()
            problems = replay(OptimizeRequest.model_validate(payload), body)
            if problems:
                failures += 1
                print("FAIL ", payload["scenario_id"], "-", len(problems), "rule violation(s)")
                for p in problems[:6]:
                    print("        -", p)
            else:
                print("OK   ", "{}  {:.0f} ms  cost {:,.2f} BDT  peak {:.1f} kWh  {}".format(
                    payload["scenario_id"], elapsed, body["total_cost_bdt"], body["peak_grid_kwh"],
                    [e["directive_type"] for e in body["directive_interpretation"]]))

        bad = client.post(base_url + "/optimize-energy", content=b"{not json",
                          headers={"Content-Type": "application/json"})
        ok = bad.status_code in (400, 422)
        print(("OK   " if ok else "FAIL "), "malformed JSON ->", bad.status_code)
        failures += not ok

    print("\n{} failure(s)".format(failures))
    return failures


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(1 if main(sys.argv[1]) else 0)
