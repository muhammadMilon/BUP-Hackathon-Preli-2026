"""Provider-chain tests with a mocked HTTP transport -- no API keys needed.

Covers: Grok success and repair, fallback to Gemini on error / garbage / bad
model id, fallback to the regex rules when both providers are down, and
per-note recovery when a model invents a directive type.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["XAI_API_KEY"] = "test-key"
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ.pop("LLM_DISABLED", None)

import httpx  # noqa: E402

import app.llm as llm  # noqa: E402
from app.schemas import OptimizeRequest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples", "public_cases.json")
REQ = OptimizeRequest.model_validate(json.load(open(SAMPLES, encoding="utf-8"))[0])

REAL_CLIENT = httpx.AsyncClient

GROK_OK = {"choices": [{"message": {"content": json.dumps({"interpretations": [
    # Deliberately messy: unsorted hours and a factor written as a percentage.
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [14, 13, 13], "factor": 20}, "explanation": "pv maintenance"},
    {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]}, "explanation": "no charging"},
    {"note_index": 2, "applies": False, "directive_type": "no_op",
     "structured_adjustment": None, "explanation": "menu change"},
]})}}]}

GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": json.dumps({"interpretations": [
    # Gemini's schema is flat; structured_adjustment is rebuilt by _unflatten.
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "hours": [13, 14], "factor": 0.2, "explanation": "pv"},
    {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
     "hours": [14, 15], "explanation": "nc"},
    {"note_index": 2, "applies": False, "directive_type": "no_op", "explanation": "menu"},
]})}]}}]}

GROK_INVENTED = {"choices": [{"message": {"content": json.dumps({"interpretations": [
    {"note_index": 0, "applies": True, "directive_type": "shut_it_all_down",
     "structured_adjustment": {"hours": [1]}, "explanation": "invented"},
    {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]}, "explanation": "ok"},
    {"note_index": 2, "applies": False, "directive_type": "no_op",
     "structured_adjustment": None, "explanation": "menu"},
]})}}]}


def is_grok(request: httpx.Request) -> bool:
    return "x.ai" in str(request.url)


def h_grok_ok(r):
    return httpx.Response(200, json=GROK_OK)


def h_grok_500(r):
    return httpx.Response(500, text="server error") if is_grok(r) else httpx.Response(200, json=GEMINI_OK)


def h_grok_garbage(r):
    if is_grok(r):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Sorry, I can't help."}}]})
    return httpx.Response(200, json=GEMINI_OK)


def h_both_down(r):
    return httpx.Response(503, text="unavailable")


def h_grok_invented(r):
    return httpx.Response(200, json=GROK_INVENTED)


def h_first_model_404(r):
    if is_grok(r) and b"grok-4-fast" in r.content:
        return httpx.Response(404, text="The model does not exist")
    return httpx.Response(200, json=GROK_OK)


def h_grok_fenced(r):
    fenced = "```json\n" + GROK_OK["choices"][0]["message"]["content"] + "\n```"
    return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})


async def run_case(name, handler, expect_provider, expect_types=None):
    llm._CACHE.clear()
    httpx.AsyncClient = lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), timeout=5)
    try:
        entries, provider, warnings = await llm.interpret_notes(REQ)
    finally:
        httpx.AsyncClient = REAL_CLIENT

    types = [e["directive_type"] for e in entries]
    ok = provider == expect_provider
    if expect_types is not None:
        ok = ok and types == expect_types
    ok = ok and [e["note_index"] for e in entries] == [0, 1, 2]
    for entry in entries:
        if entry["directive_type"] == "no_op":
            ok = ok and entry["applies"] is False and entry["structured_adjustment"] is None
        else:
            ok = ok and entry["applies"] is True and isinstance(entry["structured_adjustment"], dict)

    print(("OK   " if ok else "FAIL "), "{:38s} provider={:7s} {}".format(name, provider, types))
    return 0 if ok else 1


async def main() -> int:
    failures = 0
    expected = ["solar_reduction", "no_charge_window", "no_op"]

    failures += await run_case("grok ok (repairs 20 -> 0.2, sorts)", h_grok_ok, "grok", expected)
    failures += await run_case("grok reply in a code fence", h_grok_fenced, "grok", expected)
    failures += await run_case("grok 500 -> gemini", h_grok_500, "gemini", expected)
    failures += await run_case("grok non-JSON -> gemini", h_grok_garbage, "gemini", expected)
    failures += await run_case("both providers down -> rules", h_both_down, "rules", expected)
    failures += await run_case("grok invents a type -> per-note rules", h_grok_invented, "grok", expected)
    failures += await run_case("first model id 404 -> next id", h_first_model_404, "grok", expected)

    # The repair path must have produced a legal factor from the "20" the model sent.
    llm._CACHE.clear()
    httpx.AsyncClient = lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(h_grok_ok), timeout=5)
    try:
        entries, _, _ = await llm.interpret_notes(REQ)
    finally:
        httpx.AsyncClient = REAL_CLIENT
    adj = entries[0]["structured_adjustment"]
    ok = adj == {"hours": [13, 14], "factor": 0.2}
    print(("OK   " if ok else "FAIL "), "guardrail repair ->", adj)
    failures += 0 if ok else 1

    print("\nllm chain: {} failure(s)".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
