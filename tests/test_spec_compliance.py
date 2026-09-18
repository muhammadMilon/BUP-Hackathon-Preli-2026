"""Asserts the contract in the problem statement, clause by clause.

Every check below names the section it enforces, so a spec change is easy to
trace back to the code that has to move.
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_DISABLED", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.schemas import OptimizeRequest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples", "public_cases.json")

ALLOWED_TYPES = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
                 "no_discharge_window", "max_grid_window", "no_op"}
SHAPES = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}
TOL = 0.01

client = TestClient(app)
results = []


def check(section: str, description: str, condition: bool, detail: str = "") -> None:
    results.append((section, description, bool(condition), detail))


def main() -> int:
    # -- Section 06: API contract --------------------------------------- #
    health = client.get("/health")
    check("06", "GET /health returns 200 with status ok",
          health.status_code == 200 and health.json() == {"status": "ok"},
          str(health.status_code))

    check("06.1", "malformed JSON returns 400",
          client.post("/optimize-energy", content=b"{nope").status_code == 400)
    check("06.1", "structurally invalid request returns 400",
          client.post("/optimize-energy", json={"scenario_id": "x"}).status_code == 400)

    payload = json.load(open(SAMPLES, encoding="utf-8"))[0]

    short_hours = json.loads(json.dumps(payload))
    short_hours["hours"] = short_hours["hours"][:23]
    check("07", "a 23-hour request is rejected",
          client.post("/optimize-energy", json=short_hours).status_code == 400)

    dup_hours = json.loads(json.dumps(payload))
    dup_hours["hours"][5]["hour"] = 6
    check("07", "duplicate hour indices are rejected",
          client.post("/optimize-energy", json=dup_hours).status_code == 400)

    empty_note = json.loads(json.dumps(payload))
    empty_note["operator_notes"] = ["   "]
    check("07", "an empty operator note is rejected",
          client.post("/optimize-energy", json=empty_note).status_code == 400)

    # -- Sections 05/09/10/11: a full successful response ---------------- #
    resp = client.post("/optimize-energy", json=payload)
    check("06", "valid scenario returns 200", resp.status_code == 200, resp.text[:120])
    if resp.status_code != 200:
        return report()

    body = resp.json()
    req = OptimizeRequest.model_validate(payload)
    bat = req.battery
    hours = req.hours_sorted()
    tariff = [h.tariff_bdt_per_kwh for h in hours]
    demand = [h.demand_kwh for h in hours]

    # 10.1 top-level fields
    for field, kind in (("scenario_id", str), ("directive_interpretation", list),
                        ("hourly_plan", list), ("total_grid_kwh", (int, float)),
                        ("total_cost_bdt", (int, float)), ("peak_grid_kwh", (int, float)),
                        ("plan_summary", str)):
        check("10.1", "response has {} ({})".format(field, kind if isinstance(kind, type) else "number"),
              field in body and isinstance(body[field], kind))
    check("10.1", "scenario_id echoes the request", body.get("scenario_id") == payload["scenario_id"])
    check("10.1", "plan_summary is non-empty", bool(body.get("plan_summary", "").strip()))

    # 05.1 / 10.2 interpretation clauses
    entries = body["directive_interpretation"]
    check("05.1", "one interpretation entry per operator note",
          len(entries) == len(payload["operator_notes"]),
          "{} vs {}".format(len(entries), len(payload["operator_notes"])))
    check("05.1", "entries are returned in note_index order 0..N-1",
          [e.get("note_index") for e in entries] == list(range(len(entries))))

    types_ok = all(e.get("directive_type") in ALLOWED_TYPES for e in entries)
    check("04", "every directive_type is a supported value", types_ok,
          str([e.get("directive_type") for e in entries]))

    applies_ok = all(
        (e["applies"] is False and e["structured_adjustment"] is None)
        if e["directive_type"] == "no_op"
        else (e["applies"] is True and isinstance(e["structured_adjustment"], dict))
        for e in entries)
    check("05.1", "applies=false only for no_op; no_op always has a null adjustment", applies_ok)

    shape_ok = all(
        set(e["structured_adjustment"]) == SHAPES[e["directive_type"]]
        for e in entries if e["directive_type"] != "no_op")
    check("04", "structured_adjustment matches the required shape per type", shape_ok)

    hours_ok = True
    for e in entries:
        adj = e["structured_adjustment"]
        if not adj:
            continue
        hs = adj["hours"]
        hours_ok &= (isinstance(hs, list) and len(hs) > 0
                     and all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in hs)
                     and hs == sorted(set(hs)))
    check("05.1", "hours are unique ascending integers in 0..23", hours_ok)

    factors_ok = all(0.0 <= e["structured_adjustment"]["factor"] <= 1.0
                     for e in entries if e["directive_type"] == "solar_reduction")
    check("08", "solar_reduction factor stays within 0..1", factors_ok)

    reserves_ok = all(0.0 <= e["structured_adjustment"]["minimum_energy_kwh"] <= bat.capacity_kwh
                      for e in entries if e["directive_type"] == "minimum_battery_reserve")
    check("08", "reserve values are non-negative and within capacity", reserves_ok)

    explanations_ok = all(isinstance(e.get("explanation"), str) and e["explanation"].strip() for e in entries)
    check("10.2", "every entry carries an explanation", explanations_ok)

    # 10.3 / 11.3 plan shape and physics
    plan = body["hourly_plan"]
    check("11.3", "hourly_plan holds 24 unique hours 0..23",
          len(plan) == 24 and sorted(p["hour"] for p in plan) == list(range(24)))

    by_hour = {p["hour"]: p for p in plan}
    finite_ok = all(
        all(isinstance(p[k], (int, float)) and math.isfinite(p[k]) and p[k] >= -TOL
            for k in ("grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh"))
        for p in plan)
    check("11.3", "plan values are finite and non-negative", finite_ok)

    action_ok = all(p["battery_action"] in ("charge", "discharge", "idle") for p in plan)
    check("10.3", "battery_action is charge, discharge or idle", action_ok)
    idle_ok = all(abs(p["battery_kwh"]) <= TOL for p in plan if p["battery_action"] == "idle")
    check("10.3", "battery_kwh is 0 whenever the action is idle", idle_ok)

    rate_ok = all(
        p["battery_kwh"] <= (bat.max_charge_kwh_per_hour if p["battery_action"] == "charge"
                             else bat.max_discharge_kwh_per_hour) + TOL
        for p in plan if p["battery_action"] != "idle")
    check("09.3", "hourly charge/discharge rate limits are respected", rate_ok)

    energy = bat.initial_energy_kwh
    transitions_ok = bounds_ok = balance_ok = True
    for h in range(24):
        row = by_hour[h]
        delta = (row["battery_kwh"] if row["battery_action"] == "charge"
                 else -row["battery_kwh"] if row["battery_action"] == "discharge" else 0.0)
        transitions_ok &= abs(row["battery_energy_after_kwh"] - (energy + delta)) <= TOL
        energy = row["battery_energy_after_kwh"]
        bounds_ok &= (bat.minimum_energy_kwh - TOL) <= energy <= (bat.capacity_kwh + TOL)
        charge = row["battery_kwh"] if row["battery_action"] == "charge" else 0.0
        discharge = row["battery_kwh"] if row["battery_action"] == "discharge" else 0.0
        balance_ok &= abs(row["grid_kwh"] + row["solar_used_kwh"] + discharge
                          - demand[h] - charge) <= TOL

    check("09.1", "battery state transitions follow the stated arithmetic", transitions_ok)
    check("09.2", "battery energy stays within [minimum, capacity]", bounds_ok)
    check("09.5", "the energy balance equation holds every hour", balance_ok)
    check("09.6", "final battery energy equals the initial energy",
          abs(energy - bat.initial_energy_kwh) <= TOL,
          "{} vs {}".format(energy, bat.initial_energy_kwh))

    # 11.2 directives actually applied downstream
    solar_dir = next((e for e in entries if e["directive_type"] == "solar_reduction"), None)
    if solar_dir:
        adj = solar_dir["structured_adjustment"]
        applied_ok = all(
            by_hour[h]["solar_used_kwh"] <= hours[h].solar_kwh * adj["factor"] + TOL for h in adj["hours"])
        check("11.2", "solar_reduction is applied to the schedule, not just extracted", applied_ok)
    nc = next((e for e in entries if e["directive_type"] == "no_charge_window"), None)
    if nc:
        applied_ok = all(by_hour[h]["battery_action"] != "charge" for h in nc["structured_adjustment"]["hours"])
        check("11.2", "no_charge_window is honoured in the schedule", applied_ok)

    solar_ok = all(by_hour[h]["solar_used_kwh"] <= hours[h].solar_kwh + TOL for h in range(24))
    check("09.4", "solar used never exceeds the available solar", solar_ok)

    # 11.3 aggregates recalculated from the plan
    check("11.3", "total_grid_kwh matches the plan",
          abs(body["total_grid_kwh"] - sum(p["grid_kwh"] for p in plan)) <= TOL)
    check("11.3", "peak_grid_kwh matches the plan",
          abs(body["peak_grid_kwh"] - max(p["grid_kwh"] for p in plan)) <= TOL)
    check("11.3", "total_cost_bdt matches the plan",
          abs(body["total_cost_bdt"] - sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan)) <= TOL)

    return report()


def report() -> int:
    failed = [r for r in results if not r[2]]
    for section, description, ok, detail in results:
        print("{}  sec {:<5s} {}{}".format("OK  " if ok else "FAIL", section, description,
                                        ("  [" + detail + "]") if detail and not ok else ""))
    print("\nspec compliance: {}/{} checks passed".format(len(results) - len(failed), len(results)))
    return len(failed)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
