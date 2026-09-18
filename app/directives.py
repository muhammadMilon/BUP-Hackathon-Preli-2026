"""Directive guardrails and the deterministic fallback interpreter.

Two responsibilities:

1. ``normalize_entry`` -- treat LLM output as untrusted structured data. It is
   coerced into the exact shape required by Section 04/08, repaired where the
   repair is unambiguous, and rejected outright otherwise.
2. ``rule_based_interpret`` -- a regex interpreter used when the LLM is
   unavailable or returns something unrepairable, so the service degrades to a
   valid answer instead of a 500.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import ALLOWED_DIRECTIVE_TYPES, Battery
from app.timeparse import extract_hours

SHAPE: Dict[str, Tuple[str, ...]] = {
    "solar_reduction": ("hours", "factor"),
    "minimum_battery_reserve": ("hours", "minimum_energy_kwh"),
    "no_charge_window": ("hours",),
    "no_discharge_window": ("hours",),
    "max_grid_window": ("hours", "max_grid_kwh"),
}


class DirectiveError(ValueError):
    """Raised when an interpretation cannot be repaired into a valid directive."""


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
def _clean_hours(raw: Any) -> List[int]:
    if not isinstance(raw, (list, tuple)):
        raise DirectiveError("hours must be a list")
    out: set[int] = set()
    for item in raw:
        if isinstance(item, bool):
            raise DirectiveError("hours must be integers")
        if isinstance(item, str):
            item = item.strip()
            if not re.fullmatch(r"-?\d+", item):
                raise DirectiveError("hour value is not an integer")
            item = int(item)
        if isinstance(item, float):
            if not float(item).is_integer():
                raise DirectiveError("hour value is not a whole hour")
            item = int(item)
        if not isinstance(item, int):
            raise DirectiveError("hours must be integers")
        if not 0 <= item <= 23:
            raise DirectiveError("hour value is outside 0..23")
        out.add(item)
    if not out:
        raise DirectiveError("hours must not be empty")
    return sorted(out)


def _finite_number(raw: Any, field: str) -> float:
    if isinstance(raw, bool):
        raise DirectiveError(field + " must be a number")
    if isinstance(raw, str):
        try:
            raw = float(raw.strip().rstrip("%"))
        except ValueError as exc:
            raise DirectiveError(field + " is not numeric") from exc
    if not isinstance(raw, (int, float)):
        raise DirectiveError(field + " must be a number")
    value = float(raw)
    if not math.isfinite(value):
        raise DirectiveError(field + " must be finite")
    return value


def normalize_entry(entry: Any, note_index: int, battery: Battery) -> Dict[str, Any]:
    """Coerce one raw interpretation into a guardrail-compliant entry.

    Raises DirectiveError when the entry cannot be salvaged, which lets the
    caller fall back to the rule-based interpreter for that single note.
    """
    if not isinstance(entry, dict):
        raise DirectiveError("interpretation entry must be an object")

    dtype = entry.get("directive_type")
    if isinstance(dtype, str):
        dtype = dtype.strip().lower().replace("-", "_").replace(" ", "_")
    if dtype not in ALLOWED_DIRECTIVE_TYPES:
        raise DirectiveError("unsupported directive_type")

    explanation = entry.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        explanation = "Interpreted from the operator note."
    explanation = explanation.strip()[:400]

    applies = entry.get("applies")
    adj = entry.get("structured_adjustment")

    if dtype == "no_op":
        # applies=false / adjustment=null are mandatory for no_op.
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": explanation,
        }

    if applies is False:
        # A concrete directive the model also marked inapplicable is contradictory.
        raise DirectiveError("applies=false is only allowed for no_op")

    if not isinstance(adj, dict):
        raise DirectiveError("structured_adjustment must be an object")

    hours = _clean_hours(adj.get("hours"))
    clean: Dict[str, Any] = {"hours": hours}

    if dtype == "solar_reduction":
        factor = _finite_number(adj.get("factor"), "factor")
        if factor > 1.0:
            # Tolerate a percentage written as 20 instead of 0.2.
            if factor <= 100.0:
                factor = factor / 100.0
            else:
                raise DirectiveError("factor out of range")
        if factor < 0.0:
            raise DirectiveError("factor must be >= 0")
        clean["factor"] = round(min(1.0, factor), 6)

    elif dtype == "minimum_battery_reserve":
        reserve = _finite_number(adj.get("minimum_energy_kwh"), "minimum_energy_kwh")
        if reserve < 0:
            raise DirectiveError("minimum_energy_kwh must be non-negative")
        if reserve > battery.capacity_kwh:
            raise DirectiveError("minimum_energy_kwh exceeds battery capacity")
        clean["minimum_energy_kwh"] = round(reserve, 6)

    elif dtype == "max_grid_window":
        cap = _finite_number(adj.get("max_grid_kwh"), "max_grid_kwh")
        if cap < 0:
            raise DirectiveError("max_grid_kwh must be non-negative")
        clean["max_grid_kwh"] = round(cap, 6)

    for key in list(clean):
        if key not in SHAPE[dtype]:
            clean.pop(key)
    for key in SHAPE[dtype]:
        if key not in clean:
            raise DirectiveError("missing " + key + " for " + dtype)

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": dtype,
        "structured_adjustment": clean,
        "explanation": explanation,
    }


def no_op_entry(
    note_index: int,
    reason: str = "This note does not affect today's energy schedule.",
) -> Dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": reason,
    }


# --------------------------------------------------------------------------- #
# Deterministic fallback interpreter
# --------------------------------------------------------------------------- #
FRACTIONS = {
    "half": 0.5, "halves": 0.5, "third": 1 / 3, "thirds": 1 / 3,
    "quarter": 0.25, "quarters": 0.25, "fourth": 0.25, "fourths": 0.25,
    "fifth": 0.2, "fifths": 0.2, "sixth": 1 / 6, "eighth": 0.125,
    "tenth": 0.1, "tenths": 0.1,
}
MULTIPLIERS = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "1": 1, "2": 2, "3": 3, "4": 4}

_SOLAR = re.compile(
    r"\b(solar|pv|photovoltaic|panel|panels|rooftop|roof-top|array|irradian\w+|sunlight|sun)\b",
    re.I,
)
_SOLAR_CHANGE = re.compile(
    r"\b(drop\w*|down|reduc\w+|decreas\w+|declin\w+|fall\w*|lower|cut|curtail\w*|derat\w+|"
    r"limited|shade\w*|cloud\w*|overcast|dust\w*|dirty|clean\w+|wash\w+|maintenance|offline|"
    r"output|generation|production|yield|availab\w+|halv\w+|dim\w+|weaker?)\b",
    re.I,
)

_CHARGE = re.compile(
    r"\b(charg\w+|topp?ing\s+up|top[- ]?up|tops?\s+up|recharg\w+|store\s+energy|absorb\w*)\b",
    re.I,
)
_DISCHARGE = re.compile(
    r"\b(discharg\w+|drain\w*|draw\w*\s+(?:from|on)\s+the\s+batter\w+|"
    r"batter\w+\s+(?:output|support)|"
    r"(?:supply|deliver\w*|feed\w*)\s+(?:power\s+)?from\s+the\s+batter\w+|"
    r"use\s+the\s+batter\w+|batter\w+\s+power|"
    r"batter\w+\s+(?:\w+\s+){0,4}?(?:used?|using|supply|supplies|serve|cover|meet|support))\b",
    re.I,
)
_NEGATION = re.compile(
    r"\b(no|not|never|don't|dont|do\s+not|cannot|can't|cant|must\s+not|may\s+not|should\s+not|"
    r"avoid|prohibit\w*|forbid\w*|disallow\w*|disable\w*|unavailable|offline|out\s+of\s+service|"
    r"suspend\w*|halt\w*|stop\w*|pause\w*|block\w*|locked\s+out|refrain|hold\s+off|isolat\w*|"
    r"inhibit\w*|restrict\w*|skip|withhold\w*|freeze|frozen)\b",
    re.I,
)

_RESERVE = re.compile(
    r"\b(reserve|at\s+least|no\s+lower\s+than|not\s+(?:drop|go|fall)\s+below|"
    r"never\s+(?:drop|go|fall)\s+below|minimum|min\.?|floor|keep|maintain|retain|hold|preserve|"
    r"buffer|back-?up|stay\s+(?:at\s+or\s+)?above|remain\s+(?:at\s+or\s+)?above)\b",
    re.I,
)
_RESERVE_STRONG = re.compile(
    r"\b(in\s+reserve|reserve\s+of|reserve\s+level|at\s+least|no\s+lower\s+than|"
    r"(?:not|never)\s+(?:\w+\s+){0,8}?(?:drop|go|fall|dip|sink)\s+below|"
    r"minimum\s+of|floor\s+of|buffer\s+of|"
    r"stay\s+(?:at\s+or\s+)?above|remain\s+(?:at\s+or\s+)?above|keep\s+(?:at\s+least|above))\b",
    re.I,
)
_BATTERY = re.compile(r"\b(batter\w+|bess|storage|state\s+of\s+charge|soc)\b", re.I)
_SOC_PHRASE = re.compile(
    r"\bstate[- ]of[- ]charge\b|\bsoc\b|\bcharge\s+level\b|\bcharge\s+state\b|"
    r"\blevel\s+of\s+charge\b",
    re.I,
)

_GRID = re.compile(r"\b(grid|import\w*|utility|mains|feeder|transformer)\b", re.I)
_CAP = re.compile(
    r"\b(not\s+exceed|no\s+more\s+than|at\s+most|cap\w*|capped|limit\w*|max\w*|ceiling|"
    r"stay\s+(?:under|below)|keep\s+\w+\s+(?:under|below)|under|below|restrict\w*|throttl\w*)\b",
    re.I,
)


def _mask_soc(text: str) -> str:
    """Blank out "state of charge" so it cannot match the charging verb."""
    return _SOC_PHRASE.sub(" battery level ", text)


def _pct(text: str) -> Optional[float]:
    """Return the *remaining* usable solar fraction described by ``text``."""
    t = text.lower()

    # "...drops to 20%", "treated as roughly 25%", "running at 40%".
    # The lookahead keeps "down to 25% lower" out of the remaining-fraction branch.
    m = re.search(
        r"\b(?:to|at|of|as)\s+(?:about|around|roughly|approximately|~|just|only|some)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)"
        r"(?!\s*(?:reduction|drop|decrease|decline|loss|cut|shortfall|lower|less|dip|derating))", t)
    if m:
        return float(m.group(1)) / 100.0
    # "25% of the forecast", "20% of its rated output".
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)\s*of\s+(?:its|the|their|a)?\s*"
        r"(?:normal|usual|typical|rated|nameplate|expected|full|capacity|nominal|peak|forecast|"
        r"baseline|potential|output|generation|production)", t)
    if m:
        return float(m.group(1)) / 100.0

    m = re.search(
        r"(?:reduc\w+|drop\w*|decreas\w+|declin\w+|down|cut|fall\w*|lower|less|loss|curtail\w*)"
        r"(?:\s+\w+){0,3}?\s+by\s+(?:about|around|roughly|approximately|~)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)", t)
    if m:
        return max(0.0, 1.0 - float(m.group(1)) / 100.0)
    m = re.search(
        r"(?:about|around|roughly|approximately|~|an?)?\s*(\d+(?:\.\d+)?)\s*"
        r"(?:%|percent|per\s*cent)\s*"
        r"(?:reduction|drop|decrease|decline|loss|cut|shortfall|lower|less|dip|derating)", t)
    if m:
        return max(0.0, 1.0 - float(m.group(1)) / 100.0)

    # "one-fifth", "a third", and bare "about half of" / "half the output". A bare
    # fraction word needs "half" or a following "of" so "quarter past one" stays out.
    m = re.search(r"\b(one|two|three|four|five|a|an|\d)\s*[- ]\s*(" + "|".join(FRACTIONS) + r")\b", t) or \
        re.search(r"\b()(half|halves)\b(?!\s*(?:past|an?\s+hour))", t) or \
        re.search(r"\b()(" + "|".join(FRACTIONS) + r")\s+of\b", t)
    if m:
        frac = min(1.0, MULTIPLIERS.get(m.group(1), 1) * FRACTIONS[m.group(2)])
        before = t[max(0, m.start() - 45):m.start()]
        says_remaining = re.search(
            r"\b(to|of|at|leave\w*|leaving|remain\w*|produce\w*|generat\w*|deliver\w*|only|"
            r"about|roughly)\s*$",
            before)
        says_loss = re.search(
            r"\b(reduc\w+|drop\w*|cut|lose|losing|loss|less|lower|down|decreas\w+)\b", before)
        if says_loss and not says_remaining:
            return max(0.0, 1.0 - frac)
        return frac

    if re.search(r"\b(cut|slash\w*|drop\w*)\s+in\s+half\b", t) or re.search(r"\bhalv\w*\b", t):
        return 0.5
    if re.search(r"\b(no|zero|nil)\s+(?:solar|pv|output|generation|production)\b", t) or \
       re.search(r"\b(completely|fully|totally|entirely)\s+"
                 r"(?:offline|shaded|covered|down|out|disabled|dark|blocked)\b", t) or \
       re.search(r"\b(?:solar|pv|panels?|array)\b[^.]{0,45}?"
                 r"\b(?:offline|out\s+of\s+service|shut\s+down|disconnected|producing\s+nothing)\b", t):
        return 0.0
    return None


def _kwh_value(text: str, battery: Battery) -> Optional[float]:
    t = text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:kwh|kw-?h|kilowatt[- ]?hours?)", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)", t)
    if m and battery.capacity_kwh > 0:
        return float(m.group(1)) / 100.0 * battery.capacity_kwh
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:units?|kw)\b", t)
    if m:
        return float(m.group(1))
    return None


def _score_solar(t: str) -> float:
    # A solar directive must actually name the solar resource.
    if not _SOLAR.search(t):
        return 0.0
    score = 2.0
    if _SOLAR_CHANGE.search(t):
        score += 1.0
    if _pct(t) is not None:
        score += 1.5
    return score


def _score_no_charge(t: str) -> float:
    masked = _mask_soc(t)
    if not (_CHARGE.search(masked) and _NEGATION.search(masked)):
        return 0.0
    return 5.0 if _BATTERY.search(t) else 4.0


def _score_no_discharge(t: str) -> float:
    masked = _mask_soc(t)
    if not (_DISCHARGE.search(masked) and _NEGATION.search(masked)):
        return 0.0
    return 5.0 if _BATTERY.search(t) else 4.0


def _score_reserve(t: str) -> float:
    if not re.search(r"\d", t):
        return 0.0
    masked = _mask_soc(t)
    # "do not charge the battery" also matches _RESERVE via "keep"/"hold"; defer to those.
    if _NEGATION.search(masked) and (_CHARGE.search(masked) or _DISCHARGE.search(masked)):
        return 0.0
    if _BATTERY.search(t) and _RESERVE.search(t):
        return 6.0
    # Reserve notes frequently omit the word "battery" entirely.
    if _RESERVE_STRONG.search(t) and re.search(r"(kwh|kw-?h|kilowatt|%|percent)", t, re.I):
        return 5.5
    return 0.0


def _score_grid_cap(t: str) -> float:
    if not (_GRID.search(t) and _CAP.search(t) and re.search(r"\d", t)):
        return 0.0
    return 6.0


def rule_based_interpret(note: str, note_index: int, battery: Battery) -> Dict[str, Any]:
    """Interpret a single note without an LLM. Always returns a valid entry."""
    text = note.strip()
    hours = extract_hours(text)

    scores = {
        "max_grid_window": _score_grid_cap(text),
        "minimum_battery_reserve": _score_reserve(text),
        "no_charge_window": _score_no_charge(text),
        "no_discharge_window": _score_no_discharge(text),
        "solar_reduction": _score_solar(text),
    }
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 2.5:
        return no_op_entry(note_index)

    if hours is None:
        # A directive with no stated window covers the whole planning horizon.
        hours = list(range(24))

    adj: Dict[str, Any] = {"hours": hours}
    if best == "solar_reduction":
        factor = _pct(text)
        if factor is None:
            return no_op_entry(note_index)
        adj["factor"] = round(max(0.0, min(1.0, factor)), 6)
        why = "Usable solar falls to {:.0%} of the forecast during these hours.".format(adj["factor"])
    elif best == "minimum_battery_reserve":
        value = _kwh_value(text, battery)
        if value is None:
            return no_op_entry(note_index)
        value = max(0.0, min(float(value), battery.capacity_kwh))
        adj["minimum_energy_kwh"] = round(value, 6)
        why = "Battery energy must stay at or above {:g} kWh during these hours.".format(value)
    elif best == "max_grid_window":
        value = _kwh_value(text, battery)
        if value is None:
            return no_op_entry(note_index)
        adj["max_grid_kwh"] = round(max(0.0, float(value)), 6)
        why = "Grid import is capped at {:g} kWh during these hours.".format(value)
    elif best == "no_charge_window":
        why = "Battery charging is unavailable during these hours."
    else:
        why = "Battery discharging is unavailable during these hours."

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": best,
        "structured_adjustment": adj,
        "explanation": why,
    }
