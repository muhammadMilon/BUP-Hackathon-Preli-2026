"""End-to-end check: run every sample scenario through the real endpoint and
replay the result with the independent validator.

Runs with LLM_DISABLED=1 by default so it exercises the deterministic path and
needs no API keys. Unset that to test the live Grok/Gemini chain.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.schemas import OptimizeRequest  # noqa: E402
from app.validate import replay  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples", "public_cases.json")


def baseline_cost(req: OptimizeRequest) -> float:
    """Cost of the naive plan: free solar, no battery shifting."""
    total = 0.0
    for hour in req.hours_sorted():
        total += max(0.0, hour.demand_kwh - hour.solar_kwh) * hour.tariff_bdt_per_kwh
    return total


def main() -> int:
    client = TestClient(app)
    failures = 0

    health = client.get("/health")
    assert health.status_code == 200 and health.json().get("status") == "ok", "health check failed"
    print("OK   GET /health -> 200 {'status': 'ok'}")

    for payload in json.load(open(SAMPLES, encoding="utf-8")):
        resp = client.post("/optimize-energy", json=payload)
        if resp.status_code != 200:
            print("FAIL", payload["scenario_id"], "HTTP", resp.status_code, resp.text[:200])
            failures += 1
            continue

        body = resp.json()
        req = OptimizeRequest.model_validate(payload)
        problems = replay(req, body)
        base = baseline_cost(req)
        saving = (base - body["total_cost_bdt"]) / base * 100 if base else 0.0

        if problems:
            failures += 1
            print("FAIL", payload["scenario_id"])
            for p in problems[:8]:
                print("       -", p)
        else:
            types = [e["directive_type"] for e in body["directive_interpretation"]]
            print("OK   {}  cost {:>10,.2f} BDT  ({:.1f}% under naive)  peak {:>6.1f} kWh  {}".format(
                payload["scenario_id"], body["total_cost_bdt"], saving, body["peak_grid_kwh"], types))

    # Malformed and structurally invalid requests must not 500.
    bad = client.post("/optimize-energy", content=b"{not json")
    print(("OK  " if bad.status_code == 400 else "FAIL"), "malformed JSON ->", bad.status_code)
    failures += bad.status_code != 400

    short = client.post("/optimize-energy", json={"scenario_id": "X", "operator_notes": ["hi"],
                                                  "hours": [], "battery": {}})
    print(("OK  " if short.status_code == 400 else "FAIL"), "invalid schema ->", short.status_code)
    failures += short.status_code != 400

    print("\nend-to-end: {} failure(s)".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
