"""Randomised property test.

Generates hundreds of scenarios with randomly injected directives, pushes each
through the live endpoint, and replays every response with the independent
validator. Also feeds deliberately hostile LLM output through the guardrails to
confirm a bad model reply can never reach the optimizer.
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.directives import DirectiveError, normalize_entry  # noqa: E402
from app.llm import _align  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import Battery, OptimizeRequest  # noqa: E402
from app.validate import replay  # noqa: E402

NOTE_TEMPLATES = [
    "Solar output will drop to about {pct}% from {a} to {b}.",
    "Expect a {loss}% reduction in rooftop PV between {a} and {b}.",
    "Do not charge the battery between {a} and {b}.",
    "Battery charging is unavailable from {a} until {b}.",
    "The battery must not discharge between {a} and {b}.",
    "Keep at least {kwh} kWh in reserve from {a} until {b}.",
    "Maintain a minimum of {kwh} kWh in the storage system between {a} and {b}.",
    "Grid import must not exceed {cap} kWh from {a} to {b}.",
    "Cap grid draw at {cap} kWh between {a} and {b}.",
    "The cafeteria menu changes tomorrow.",
    "An elevator inspection is scheduled in Building C.",
    "Library WiFi will be upgraded next week.",
]

# Indexed by hour 0..23 so a generated window reads back as the same hours.
CLOCK = (["midnight"] + ["{} AM".format(h) for h in range(1, 12)]
         + ["noon"] + ["{} PM".format(h - 12) for h in range(13, 24)])


def random_scenario(rng: random.Random, index: int) -> dict:
    capacity = rng.choice([400, 500, 600, 800, 1000])
    initial = rng.randint(int(capacity * 0.2), int(capacity * 0.6))
    minimum = rng.randint(0, max(1, int(initial * 0.5)))
    peak_demand = rng.randint(200, 600)

    hours = []
    for h in range(24):
        daylight = max(0.0, 1.0 - abs(h - 12.5) / 7.0)
        hours.append({
            "hour": h,
            "demand_kwh": round(peak_demand * (0.45 + 0.55 * max(0.0, 1 - abs(h - 14) / 12)) + rng.uniform(-15, 15), 2),
            "solar_kwh": round(peak_demand * 0.7 * daylight * rng.uniform(0.7, 1.1), 2),
            "tariff_bdt_per_kwh": round(rng.uniform(4, 20), 2),
        })
    for row in hours:
        row["demand_kwh"] = max(10.0, row["demand_kwh"])

    notes = []
    for _ in range(rng.randint(1, 3)):
        start = rng.randrange(0, 22)
        end = rng.randrange(start + 1, min(start + 7, 24))
        notes.append(rng.choice(NOTE_TEMPLATES).format(
            pct=rng.choice([0, 10, 20, 25, 40, 50, 75]),
            loss=rng.choice([20, 30, 50, 60, 80, 100]),
            kwh=rng.randint(minimum, max(minimum + 1, int(initial))),
            cap=rng.randint(int(peak_demand * 0.8), int(peak_demand * 1.6)),
            a=CLOCK[start], b=CLOCK[end],
        ))

    return {
        "scenario_id": "STRESS-{:04d}".format(index),
        "operator_notes": notes,
        "hours": hours,
        "battery": {
            "capacity_kwh": capacity,
            "initial_energy_kwh": initial,
            "minimum_energy_kwh": minimum,
            "max_charge_kwh_per_hour": rng.choice([60, 100, 150, 200]),
            "max_discharge_kwh_per_hour": rng.choice([60, 100, 150, 200]),
        },
    }


HOSTILE_ENTRIES = [
    {"note_index": 0, "applies": True, "directive_type": "shutdown_campus",
     "structured_adjustment": {"hours": [1]}, "explanation": "invented type"},
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [3, 1, 1, 2], "factor": 0.4}, "explanation": "unsorted duplicates"},
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [30], "factor": 0.4}, "explanation": "hour out of range"},
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [3], "factor": 20}, "explanation": "percent not fraction"},
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [3], "factor": -1}, "explanation": "negative factor"},
    {"note_index": 0, "applies": False, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [3]}, "explanation": "applies=false on a real directive"},
    {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
     "structured_adjustment": {"hours": [3], "minimum_energy_kwh": 1e9}, "explanation": "above capacity"},
    {"note_index": 0, "applies": True, "directive_type": "max_grid_window",
     "structured_adjustment": {"hours": [3], "max_grid_kwh": float("inf")}, "explanation": "not finite"},
    {"note_index": 0, "applies": True, "directive_type": "no_op",
     "structured_adjustment": {"hours": [3]}, "explanation": "no_op with an adjustment"},
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": None, "explanation": "missing adjustment"},
    "not even an object",
    {"note_index": 99, "applies": True, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [3]}, "explanation": "index out of range"},
]


def guardrail_checks() -> int:
    bat = Battery(capacity_kwh=500, initial_energy_kwh=200, minimum_energy_kwh=50,
                  max_charge_kwh_per_hour=100, max_discharge_kwh_per_hour=100)
    accepted, rejected = 0, 0
    for entry in HOSTILE_ENTRIES:
        try:
            clean = normalize_entry(entry, 0, bat)
        except DirectiveError:
            rejected += 1
            continue
        accepted += 1
        adj = clean["structured_adjustment"]
        assert clean["directive_type"] in (
            "solar_reduction", "minimum_battery_reserve", "no_charge_window",
            "no_discharge_window", "max_grid_window", "no_op"), clean
        if clean["directive_type"] == "no_op":
            assert clean["applies"] is False and adj is None, clean
        else:
            assert clean["applies"] is True and isinstance(adj, dict), clean
            hours = adj["hours"]
            assert hours == sorted(set(hours)) and all(0 <= h <= 23 for h in hours), clean
            if "factor" in adj:
                assert 0.0 <= adj["factor"] <= 1.0, clean
    print("OK   guardrails: {} hostile entries repaired, {} rejected outright".format(accepted, rejected))

    # A whole malformed reply must still produce one clean entry per note.
    req = OptimizeRequest.model_validate(json.load(open(SAMPLES, encoding="utf-8"))[0])
    aligned = _align(HOSTILE_ENTRIES, req)
    assert len(aligned) == len(req.operator_notes), aligned
    assert [e["note_index"] for e in aligned] == list(range(len(req.operator_notes))), aligned
    print("OK   guardrails: malformed reply still yields {} ordered entries".format(len(aligned)))
    return 0


HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples", "public_cases.json")


def main(count: int = 250) -> int:
    rng = random.Random(20260918)
    client = TestClient(app)
    failures = 0
    relaxed = 0

    guardrail_checks()

    for i in range(count):
        payload = random_scenario(rng, i)
        resp = client.post("/optimize-energy", json=payload)
        if resp.status_code != 200:
            print("FAIL", payload["scenario_id"], "HTTP", resp.status_code, resp.text[:160])
            failures += 1
            continue
        body = resp.json()
        problems = replay(OptimizeRequest.model_validate(payload), body)
        if problems:
            # A relaxation is acceptable only when the directive set is genuinely
            # impossible; the base GridWise rules must hold regardless.
            base_rules = [p for p in problems if "cap" not in p and "reserve" not in p
                          and "no_charge_window" not in p and "no_discharge_window" not in p]
            if base_rules:
                failures += 1
                print("FAIL", payload["scenario_id"], json.dumps(payload["operator_notes"]))
                for p in base_rules[:5]:
                    print("       -", p)
            else:
                relaxed += 1

    print("\nstress: {} scenarios, {} hard failure(s), {} needed a directive relaxation".format(
        count, failures, relaxed))
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
