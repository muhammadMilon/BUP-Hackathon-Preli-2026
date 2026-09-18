"""Independent replay of a finished response -- the judge's checks, locally.

``replay`` deliberately re-derives everything from the request and the emitted
``hourly_plan`` rather than trusting any solver internals. The service runs it on
its own output before responding (Section 08, "final replay"), and the local
test harness uses it to score sample scenarios.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

from app.optimizer import Constraints
from app.schemas import OptimizeRequest

TOL = 0.01


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("not a number")
    value = float(value)
    if not math.isfinite(value):
        raise TypeError("not finite")
    return value


def replay(req: OptimizeRequest, response: Dict[str, Any]) -> List[str]:
    """Return a list of rule violations. An empty list means the plan is valid."""
    problems: List[str] = []
    directives = response.get("directive_interpretation") or []
    plan = response.get("hourly_plan") or []

    # ---- interpretation shape ------------------------------------------- #
    if len(directives) != len(req.operator_notes):
        problems.append(
            "expected {} interpretation entries, got {}".format(len(req.operator_notes), len(directives)))
    for position, entry in enumerate(directives):
        if entry.get("note_index") != position:
            problems.append("directive_interpretation[{}].note_index is {}, expected {}".format(
                position, entry.get("note_index"), position))
        dtype = entry.get("directive_type")
        if dtype == "no_op":
            if entry.get("applies") is not False or entry.get("structured_adjustment") is not None:
                problems.append("no_op entry {} must have applies=false and a null adjustment".format(position))
        else:
            if entry.get("applies") is not True:
                problems.append("entry {} has directive_type {} but applies is not true".format(position, dtype))
            adj = entry.get("structured_adjustment")
            if not isinstance(adj, dict):
                problems.append("entry {} is missing structured_adjustment".format(position))
            else:
                hours = adj.get("hours")
                if not isinstance(hours, list) or not hours:
                    problems.append("entry {} has no hours".format(position))
                elif hours != sorted(set(hours)) or any(
                        not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 23 for h in hours):
                    problems.append("entry {} hours must be unique ascending integers 0..23".format(position))

    # ---- plan shape ------------------------------------------------------ #
    if len(plan) != 24:
        problems.append("hourly_plan must contain 24 entries, got {}".format(len(plan)))
        return problems
    if sorted(p.get("hour") for p in plan) != list(range(24)):
        problems.append("hourly_plan must cover each hour 0..23 exactly once")
        return problems

    by_hour = {p["hour"]: p for p in plan}
    constraints = Constraints(req, directives)
    bat = req.battery
    energy = float(bat.initial_energy_kwh)

    for h in range(24):
        row = by_hour[h]
        try:
            grid = _num(row.get("grid_kwh"))
            used = _num(row.get("solar_used_kwh"))
            magnitude = _num(row.get("battery_kwh"))
            after = _num(row.get("battery_energy_after_kwh"))
        except TypeError:
            problems.append("hour {} has a non-finite or non-numeric value".format(h))
            continue

        action = row.get("battery_action")
        if action not in ("charge", "discharge", "idle"):
            problems.append("hour {} has invalid battery_action {!r}".format(h, action))
            continue
        if grid < -TOL or used < -TOL or magnitude < -TOL:
            problems.append("hour {} has a negative energy value".format(h))
        if action == "idle" and abs(magnitude) > TOL:
            problems.append("hour {} is idle but battery_kwh is {}".format(h, magnitude))
        if action == "charge" and magnitude > bat.max_charge_kwh_per_hour + TOL:
            problems.append("hour {} charges {} kWh above the hourly limit".format(h, magnitude))
        if action == "discharge" and magnitude > bat.max_discharge_kwh_per_hour + TOL:
            problems.append("hour {} discharges {} kWh above the hourly limit".format(h, magnitude))

        delta = magnitude if action == "charge" else (-magnitude if action == "discharge" else 0.0)
        expected = energy + delta
        if abs(after - expected) > TOL:
            problems.append("hour {} battery_energy_after_kwh is {}, expected {:.4f}".format(h, after, expected))
        energy = after

        if after > bat.capacity_kwh + TOL:
            problems.append("hour {} exceeds battery capacity ({} > {})".format(h, after, bat.capacity_kwh))
        floor = constraints.reserve[h]
        if after < floor - TOL:
            problems.append("hour {} falls below the required reserve ({} < {})".format(h, after, floor))

        if used > constraints.effective_solar[h] + TOL:
            problems.append("hour {} uses {} kWh of solar but only {:.4f} kWh is available".format(
                h, used, constraints.effective_solar[h]))

        charge_kwh = magnitude if action == "charge" else 0.0
        discharge_kwh = magnitude if action == "discharge" else 0.0
        balance = grid + used + discharge_kwh - constraints.demand[h] - charge_kwh
        if abs(balance) > TOL:
            problems.append("hour {} violates the energy balance by {:.4f} kWh".format(h, balance))

        if not constraints.can_charge[h] and charge_kwh > TOL:
            problems.append("hour {} charges inside a no_charge_window".format(h))
        if not constraints.can_discharge[h] and discharge_kwh > TOL:
            problems.append("hour {} discharges inside a no_discharge_window".format(h))
        cap = constraints.max_grid[h]
        if cap is not None and grid > cap + TOL:
            problems.append("hour {} imports {} kWh above the {} kWh cap".format(h, grid, cap))

    if abs(energy - bat.initial_energy_kwh) > TOL:
        problems.append("final battery energy {} does not return to the initial {}".format(
            energy, bat.initial_energy_kwh))

    # ---- reported aggregates -------------------------------------------- #
    total_grid = sum(_num(p["grid_kwh"]) for p in plan)
    peak_grid = max(_num(p["grid_kwh"]) for p in plan)
    cost = sum(_num(p["grid_kwh"]) * constraints.tariff[p["hour"]] for p in plan)
    for field, want in (("total_grid_kwh", total_grid), ("peak_grid_kwh", peak_grid), ("total_cost_bdt", cost)):
        try:
            got = _num(response.get(field))
        except TypeError:
            problems.append("{} is missing or not a finite number".format(field))
            continue
        if abs(got - want) > TOL:
            problems.append("{} is {}, recalculated {:.4f}".format(field, got, want))

    if response.get("scenario_id") != req.scenario_id:
        problems.append("scenario_id does not match the request")

    return problems


def cost_of(plan: Sequence[Dict[str, Any]], tariff: Sequence[float]) -> float:
    return sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan)
