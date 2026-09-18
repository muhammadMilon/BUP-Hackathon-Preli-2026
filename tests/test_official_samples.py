"""Scores our output against the organizer's public sample pack.

Reads samples/official_sample_cases.json -- drop the full 10-case pack there and
this runs against all of it. For each case it compares the directive
interpretation against the reference ground truth (ignoring explanation wording,
as the rubric specifies) and computes the rubric's optimization ratio:

    quality_ratio = min(1, organizer_optimal_cost / recalculated_team_cost)
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
PACK = os.path.join(os.path.dirname(HERE), "samples", "official_sample_cases.json")
TOL = 0.01


def same(want: dict, got: dict) -> bool:
    """Compare the machine-checked part of two entries (explanation excluded).

    Numbers match within the spec's 0.01 tolerance, so 100 and 100.0 are equal.
    """
    for key in ("note_index", "applies", "directive_type"):
        if want.get(key) != got.get(key):
            return False
    wa, ga = want.get("structured_adjustment"), got.get("structured_adjustment")
    if wa is None or ga is None:
        return wa is None and ga is None
    if set(wa) != set(ga) or wa.get("hours") != ga.get("hours"):
        return False
    return all(isinstance(ga[k], (int, float)) and abs(wa[k] - ga[k]) <= TOL
               for k in wa if k != "hours")


def main() -> int:
    if not os.path.exists(PACK):
        print("no sample pack at", PACK)
        return 0

    pack = json.load(open(PACK, encoding="utf-8"))
    cases = pack.get("cases", pack if isinstance(pack, list) else [])
    client = TestClient(app)

    failures = 0
    ratios = []

    for case in cases:
        payload = case["input"]
        expected = case.get("expected_output", {})
        case_id = case.get("id", payload["scenario_id"])

        resp = client.post("/optimize-energy", json=payload)
        if resp.status_code != 200:
            print("FAIL", case_id, "HTTP", resp.status_code, resp.text[:160])
            failures += 1
            continue

        body = resp.json()
        req = OptimizeRequest.model_validate(payload)

        problems = replay(req, body)
        if problems:
            failures += 1
            print("FAIL", case_id, "- schedule is invalid")
            for p in problems[:5]:
                print("       -", p)
            continue

        # -- interpretation vs reference ground truth -------------------- #
        want = expected.get("directive_interpretation")
        interp_note = ""
        if want:
            got = body["directive_interpretation"]
            mismatches = [(w, g) for w, g in zip(want, got) if not same(w, g)]
            if len(got) != len(want) or mismatches:
                failures += 1
                print("FAIL", case_id, "- interpretation mismatch")
                for w, g in mismatches:
                    print("       note {}: expected {} {}".format(
                        w["note_index"], w["directive_type"], w["structured_adjustment"]))
                    print("                 got      {} {}".format(
                        g["directive_type"], g["structured_adjustment"]))
                continue
            interp_note = "interp {}/{}".format(len(want), len(want))

        # -- cost vs the reference optimum ------------------------------- #
        ref_cost = expected.get("total_cost_bdt")
        ours = body["total_cost_bdt"]
        if ref_cost is not None:
            ratio = 1.0 if abs(ref_cost) <= TOL and abs(ours) <= TOL else min(1.0, ref_cost / ours) if ours > TOL else 1.0
            ratios.append(ratio)
            verdict = "MATCH" if abs(ours - ref_cost) <= TOL else ("BETTER" if ours < ref_cost else "WORSE")
            print("OK   {:<10s} {}  cost {:>10,.2f} vs reference {:>10,.2f}  [{}]  ratio {:.4f}".format(
                case_id, interp_note, ours, ref_cost, verdict, ratio))
            if verdict == "WORSE":
                failures += 1
        else:
            print("OK   {:<10s} {}  cost {:>10,.2f}  (no reference cost in pack)".format(
                case_id, interp_note, ours))

    if ratios:
        avg = sum(ratios) / len(ratios)
        print("\noptimization quality: {:.4f} average ratio -> {:.2f}/10 rubric points".format(avg, avg * 10))
    print("official samples: {} failure(s) over {} case(s)".format(failures, len(cases)))
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
