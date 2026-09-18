"""Post one scenario to a running service and print the answer readably.

    python scripts/try_case.py                 # sample case 1, local server
    python scripts/try_case.py 2               # sample case 2
    python scripts/try_case.py SAMPLE-01       # a case from the organizer pack
    python scripts/try_case.py 1 --full        # include all 24 plan rows
    python scripts/try_case.py 1 --url http://YOUR_VPS_IP

Also replays the response against every GridWise rule, so you see immediately
whether the schedule is valid -- not just whether the request returned 200.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.schemas import OptimizeRequest  # noqa: E402
from app.validate import replay  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "samples", "public_cases.json")
OFFICIAL = os.path.join(ROOT, "samples", "official_sample_cases.json")


def load_case(selector: str) -> dict:
    public = json.load(open(PUBLIC, encoding="utf-8"))
    if selector.isdigit():
        index = int(selector) - 1
        if not 0 <= index < len(public):
            raise SystemExit("case {} not found; there are {} public cases".format(selector, len(public)))
        return public[index]

    wanted = selector.upper()
    for case in public:
        if case["scenario_id"].upper() == wanted:
            return case
    if os.path.exists(OFFICIAL):
        for case in json.load(open(OFFICIAL, encoding="utf-8")).get("cases", []):
            if case.get("id", "").upper() == wanted or case["input"]["scenario_id"].upper() == wanted:
                return case["input"]
    raise SystemExit("no case named {!r}".format(selector))


def main() -> int:
    args = [a for a in sys.argv[1:]]
    full = "--full" in args
    if full:
        args.remove("--full")
    url = "http://127.0.0.1:8000"
    if "--url" in args:
        at = args.index("--url")
        url = args[at + 1].rstrip("/")
        del args[at:at + 2]
    selector = args[0] if args else "1"

    case = load_case(selector)
    print("POST {}/optimize-energy".format(url))
    print("scenario: {}".format(case["scenario_id"]))
    print("operator notes:")
    for i, note in enumerate(case["operator_notes"]):
        print("  [{}] {}".format(i, note))

    request = urllib.request.Request(
        url + "/optimize-energy",
        data=json.dumps(case).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
            status = response.status
    except urllib.error.HTTPError as exc:
        print("\nHTTP {} -- {}".format(exc.code, exc.read().decode(errors="replace")[:400]))
        return 1
    except urllib.error.URLError as exc:
        print("\ncannot reach {} -- {}".format(url, exc.reason))
        print("is the server running?  uvicorn app.main:app --host 0.0.0.0 --port 8000")
        return 1
    elapsed = (time.perf_counter() - started) * 1000

    print("\nHTTP {} in {:.0f} ms".format(status, elapsed))

    print("\ninterpretation")
    for entry in body["directive_interpretation"]:
        print("  [{}] {:<24} applies={:<5} {}".format(
            entry["note_index"], entry["directive_type"], str(entry["applies"]),
            entry["structured_adjustment"]))
        print("       {}".format(entry["explanation"]))

    plan = body["hourly_plan"]
    print("\nschedule" + ("" if full else "  (battery hours only -- pass --full for all 24)"))
    print("   h     grid    solar  action       kWh    after")
    for row in plan:
        if full or row["battery_action"] != "idle":
            print("  {:2d} {:8.1f} {:8.1f}  {:<9} {:6.1f} {:8.1f}".format(
                row["hour"], row["grid_kwh"], row["solar_used_kwh"],
                row["battery_action"], row["battery_kwh"], row["battery_energy_after_kwh"]))

    print("\ntotals   grid {:,.1f} kWh   cost {:,.2f} BDT   peak {:,.1f} kWh".format(
        body["total_grid_kwh"], body["total_cost_bdt"], body["peak_grid_kwh"]))
    print("summary  {}".format(body["plan_summary"]))

    problems = replay(OptimizeRequest.model_validate(case), body)
    print("\nvalidation: " + ("PASSED -- 0 rule violations" if not problems
                              else "{} violation(s)".format(len(problems))))
    for problem in problems[:8]:
        print("  - {}".format(problem))

    provider = body["plan_summary"].split("via ")[1].split(" ")[0] if "via " in body["plan_summary"] else "?"
    if provider == "rules":
        print("\nNOTE: answered by the deterministic interpreter, not an LLM.")
        print("      Check GROQ_API_KEY / GEMINI_API_KEY in .env -- the graded path needs a model.")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
