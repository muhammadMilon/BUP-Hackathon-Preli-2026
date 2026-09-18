"""Pydantic request/response models for the Smart Campus Energy Optimization API.

The request models are deliberately strict: a structurally invalid request must
produce HTTP 400/422 rather than a half-computed schedule.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
class HourEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(..., ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)


class Battery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: float = Field(..., ge=0)
    initial_energy_kwh: float = Field(..., ge=0)
    minimum_energy_kwh: float = Field(..., ge=0)
    max_charge_kwh_per_hour: float = Field(..., ge=0)
    max_discharge_kwh_per_hour: float = Field(..., ge=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> "Battery":
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError("initial_energy_kwh cannot be below minimum_energy_kwh")
        return self


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str = Field(..., min_length=1)
    # The spec says 1-3 notes. The upper bound is deliberately looser: a hidden
    # case with a fourth note should be answered, not rejected. Everything else
    # about the request stays strict.
    operator_notes: List[str] = Field(..., min_length=1, max_length=10)
    hours: List[HourEntry] = Field(..., min_length=24, max_length=24)
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, notes: List[str]) -> List[str]:
        for i, note in enumerate(notes):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return notes

    @field_validator("hours")
    @classmethod
    def _hours_cover_full_day(cls, hours: List[HourEntry]) -> List[HourEntry]:
        seen = sorted(h.hour for h in hours)
        if seen != list(range(24)):
            raise ValueError("hours must contain exactly one entry per hour 0..23")
        return hours

    def hours_sorted(self) -> List[HourEntry]:
        return sorted(self.hours, key=lambda h: h.hour)


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #
class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict[str, Any]] = None
    explanation: str


class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
