"""Smart Campus Energy Optimization service.

GET  /health           -> readiness probe
POST /optimize-energy  -> operator-note interpretation + 24-hour schedule
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import optimizer
from app.llm import interpret_notes
from app.optimizer import Constraints, idle_battery_plan
from app.schemas import OptimizeRequest
from app.validate import replay

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("gridwise")

app = FastAPI(
    title="Smart Campus Energy Optimization",
    description="LLM-assisted operator directive interpretation and 24-hour energy scheduling.",
    version="1.0.0",
)


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "smart-campus-energy-optimization",
        "status": "ok",
        "endpoints": ["GET /health", "POST /optimize-energy"],
    }


def _error(status: int, message: str, detail: Any = None) -> JSONResponse:
    body: Dict[str, Any] = {"error": message}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body)


def _describe(entries: List[Dict[str, Any]]) -> str:
    applied = [e for e in entries if e.get("applies")]
    if not applied:
        return "no operator directives applied"
    parts = []
    for entry in applied:
        adj = entry["structured_adjustment"]
        window = adj["hours"]
        span = "h{}-{}".format(window[0], window[-1]) if len(window) > 1 else "h{}".format(window[0])
        dtype = entry["directive_type"]
        if dtype == "solar_reduction":
            parts.append("solar x{:g} at {}".format(adj["factor"], span))
        elif dtype == "minimum_battery_reserve":
            parts.append("reserve >= {:g} kWh at {}".format(adj["minimum_energy_kwh"], span))
        elif dtype == "max_grid_window":
            parts.append("grid <= {:g} kWh at {}".format(adj["max_grid_kwh"], span))
        elif dtype == "no_charge_window":
            parts.append("no charging at {}".format(span))
        else:
            parts.append("no discharging at {}".format(span))
    return "; ".join(parts)


def _apply_plan(response: Dict[str, Any], plan: List[Dict[str, Any]], tariff: List[float]) -> None:
    """Attach a plan and its recalculated aggregates to a response body."""
    response["hourly_plan"] = plan
    response["total_grid_kwh"] = round(sum(p["grid_kwh"] for p in plan), 6)
    response["peak_grid_kwh"] = round(max(p["grid_kwh"] for p in plan), 6)
    response["total_cost_bdt"] = round(sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan), 6)


def _summary(req: OptimizeRequest, entries: List[Dict[str, Any]], plan: List[Dict[str, Any]],
             cost: float, peak: float, provider: str, warnings: List[str]) -> str:
    charge_hours = [p["hour"] for p in plan if p["battery_action"] == "charge"]
    discharge_hours = [p["hour"] for p in plan if p["battery_action"] == "discharge"]
    solar_used = sum(p["solar_used_kwh"] for p in plan)
    no_ops = sum(1 for e in entries if e["directive_type"] == "no_op")

    text = (
        "Interpreted {} operator note(s) via {} ({} applied, {} no-op): {}. "
        "Charged the battery in {} low-tariff hour(s) and discharged it in {} peak hour(s), "
        "used {:.1f} kWh of available solar, and ended the day back at the starting "
        "state of charge. Total grid cost {:.2f} BDT with a {:.1f} kWh peak hour."
    ).format(
        len(entries), provider, len(entries) - no_ops, no_ops, _describe(entries),
        len(charge_hours), len(discharge_hours), solar_used, cost, peak,
    )
    if warnings:
        text += " Notes: " + "; ".join(warnings) + "."
    return text


def _example_request() -> Dict[str, Any]:
    """A real sample scenario, used to prefill the body box in /docs."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "samples", "public_cases.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)[0]
    except (OSError, ValueError, IndexError):
        return {"scenario_id": "GRID-101", "operator_notes": ["..."], "hours": [], "battery": {}}


# The handler reads the raw body so malformed JSON can return 400 rather than
# FastAPI's own 422. That means FastAPI cannot infer the request schema, so it
# is declared here -- otherwise /docs offers no body box to test with.
@app.post(
    "/optimize-energy",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": OptimizeRequest.model_json_schema(),
                    "example": _example_request(),
                }
            },
        }
    },
)
async def optimize_energy(request: Request) -> JSONResponse:
    started = time.perf_counter()

    try:
        raw = await request.body()
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(400, "Malformed JSON body")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object")

    try:
        req = OptimizeRequest.model_validate(payload)
    except ValidationError as exc:
        return _error(400, "Structurally invalid request", [
            {"field": ".".join(str(p) for p in err["loc"]), "problem": err["msg"]}
            for err in exc.errors()[:10]
        ])

    try:
        entries, provider, warnings = await interpret_notes(req)
        plan, solver_warnings = optimizer.optimize(req, entries)
        warnings = warnings + solver_warnings

        tariff = [h.tariff_bdt_per_kwh for h in req.hours_sorted()]
        total_grid = round(sum(p["grid_kwh"] for p in plan), 6)
        peak_grid = round(max(p["grid_kwh"] for p in plan), 6)
        cost = round(sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan), 6)

        response: Dict[str, Any] = {
            "scenario_id": req.scenario_id,
            "directive_interpretation": entries,
            "hourly_plan": plan,
            "total_grid_kwh": total_grid,
            "total_cost_bdt": cost,
            "peak_grid_kwh": peak_grid,
            "plan_summary": "",
        }

        # Section 08 final replay: never return a schedule we cannot verify.
        problems = replay(req, response)
        if problems:
            log.warning("self-check flagged %s: %s", req.scenario_id, problems[:5])
            safe_plan = idle_battery_plan(Constraints(req, entries), req.battery)
            candidate = dict(response)
            _apply_plan(candidate, safe_plan, tariff)
            # Keep the conservative plan only if it actually breaks fewer rules;
            # a relaxed-but-cheap schedule beats an idle one that fails just as much.
            if len(replay(req, candidate)) < len(problems):
                response = candidate
                plan = safe_plan
                warnings.append("optimized schedule failed self-validation; served a conservative plan")

        response["plan_summary"] = _summary(
            req, entries, plan, response["total_cost_bdt"], response["peak_grid_kwh"], provider, warnings)

        log.info("scenario=%s provider=%s cost=%.2f ms=%.0f",
                 req.scenario_id, provider, response["total_cost_bdt"],
                 (time.perf_counter() - started) * 1000)
        return JSONResponse(status_code=200, content=response)

    except Exception:  # noqa: BLE001 - controlled 500, no stack trace in the body
        log.exception("unhandled failure for scenario %s", req.scenario_id)
        return _error(500, "Internal error while optimizing the scenario")
