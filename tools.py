"""The four tools a voice model may call. It may call nothing else.

Every tool returns a dict with a `say` string. The model's only job is to speak
`say` back verbatim. It never composes an instruction and never names a building,
which is what makes an invented hospital structurally impossible rather than
merely discouraged.

Pure over the existing core: classify, geo, swarm, dialogue. No new policy lives
here, so the acceptance tests still cover the behaviour this exposes.
"""
from __future__ import annotations

import json
import os
import threading
import time

import classify
import dialogue
import directory
import geo

SCHEMA = [
    {
        "type": "function",
        "name": "find_place",
        "description": (
            "Resolve where the caller is from their own words. Call this the moment they "
            "name any location. Returns either one place, two to choose between, or none."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spoken": {"type": "string", "description": "The caller's words, verbatim."},
            },
            "required": ["spoken"],
        },
    },
    {
        "type": "function",
        "name": "plan_route",
        "description": (
            "Choose a destination and produce the instruction to speak. Only call after "
            "find_place returned exactly one place. Returns a refusal when every building "
            "is inside the hazard cone; speak the refusal as given."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spoken": {"type": "string", "description": "The caller's words, verbatim."},
            },
            "required": ["spoken"],
        },
    },
    {
        "type": "function",
        "name": "escalate",
        "description": (
            "The caller cannot move, cannot breathe, or is unconscious. Call immediately "
            "and stop asking questions. Never returns a map."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spoken": {"type": "string", "description": "The caller's words, verbatim."},
            },
            "required": ["spoken"],
        },
    },
    {
        "type": "function",
        "name": "close_call",
        "description": "The caller says they are safe or have made it out.",
        "parameters": {
            "type": "object",
            "properties": {
                "spoken": {"type": "string", "description": "The caller's words, verbatim."},
            },
            "required": ["spoken"],
        },
    },
]

PROMPT = """You are Ares, an emergency line. A person in danger is on the phone.

1. You may never name a building, road, hospital or shelter. Call a tool instead.
2. When a tool returns "say", speak it back exactly. Do not paraphrase, do not add to it.
3. If they cannot move, cannot breathe, or are unconscious, call escalate at once and stop asking questions.
4. While a tool is running, keep the line alive with short acknowledgements. Never go silent.

Ask only two things: what is happening, and where they are. Keep every turn short."""

NAMES = {"find_place", "plan_route", "escalate", "close_call"}

# Live/async telemetry. The console draws these two lanes, so the room can see
# that the voice path never stops while the reasoning path is still working.
_TRACE: list[dict] = []
_TRACE_LOCK = threading.Lock()
TRACE_MAX = 40


def _record(entry: dict) -> None:
    with _TRACE_LOCK:
        _TRACE.append(entry)
        del _TRACE[:-TRACE_MAX]


def trace() -> list[dict]:
    with _TRACE_LOCK:
        return list(_TRACE)


def _turn(store, phone: str, spoken: str) -> dialogue.Effect:
    """One turn through the real core, with swarm inference attached."""
    state = store.load(phone)
    # The service area is what keeps SeaWorld in San Diego instead of Orlando.
    # converse() sets this from the Twilio form; the tool path must too.
    if not state.caller_city:
        state = dialogue.replace(state, caller_city=os.environ.get("OPERATOR_AREA_CITY") or None)
    if not state.caller_state:
        state = dialogue.replace(state, caller_state=os.environ.get("OPERATOR_AREA_REGION") or None)
    return dialogue.step(
        state,
        spoken,
        classify=classify.classify,
        origins=directory.search,
        route=directory.route,
        nearby=lambda lat, lon: store.reports(exclude_phone=phone),
    )


def call(store, name: str, phone: str, spoken: str) -> dict:
    """Dispatch one tool call. Unknown names are refused, not guessed."""
    if name not in NAMES:
        return {"ok": False, "say": "", "error": f"unknown tool {name}"}
    spoken = " ".join((spoken or "").split())
    if not spoken:
        return {"ok": False, "say": "Tell me what is happening, and where you are.",
                "action": "ask"}

    t0 = time.time()
    effect = _turn(store, phone, spoken)
    elapsed = time.time() - t0

    def render(say, sms, page, listen, prompt):
        return json.dumps({"say": say, "sms": sms})

    result = store.commit(None, effect, spoken, render)

    out = {
        "ok": True,
        "say": result.say,
        "sms": result.sms,
        "action": effect.action,
        "incident": result.incident_id,
        "listen": effect.listen,
    }
    st = effect.state
    out["place"] = st.origin_label or st.place
    out["hazard"] = st.hazard
    out["need"] = st.need
    out["mobility"] = st.mobility

    # find_place is the only tool that reports a pending choice
    if effect.action == "ask" and st.pending:
        out["choices"] = [p.get("label") or p.get("name") for p in st.pending]

    out["ms"] = round(elapsed * 1000)
    _record({
        "at": time.strftime("%H:%M:%S"),
        "tool": name,
        "phone": phone[-4:],
        "spoken": spoken[:70],
        "action": effect.action,
        "say": result.say[:90],
        "ms": out["ms"],
        "place": out["place"],
        "hazard": out["hazard"],
        "swarm": "other callers nearby report" in (result.say or ""),
        "incident": result.incident_id,
    })
    return out


def session_config(public_base: str) -> dict:
    """Body for POST /v1/live/sessions.

    The voice model delegates every route and escalation to this application.
    The browser returns the line's sentence for the model to speak.
    """
    del public_base
    return {
        "model": "gpt-live-1",
        "instructions": (
            "You are a calm emergency dispatcher on a live call. You do not know the caller's name. "
            "Never say a person's name.\n"
            "When an instruction gives you words to say, say those words once, in a steady voice, "
            "then stop and listen. Do not add a building, a road, or a hospital. Do not repeat yourself.\n"
            "After the caller finishes a sentence, delegate to the application and stay silent "
            "until the next instruction."
        ),
        "delegation": {"type": "client"},
        "audio": {"output": {"voice": "vesper"}},
    }


def session_update() -> dict:
    """Sent over the data channel the moment it opens. This is what arms the tools."""
    return {
        "type": "session.update",
        "session": {
            "instructions": PROMPT,
            "tools": SCHEMA,
            "tool_choice": "auto",
            "audio": {
                "input": {"transcription": {"model": "gpt-live-transcribe"},
                          "turn_detection": {"type": "server_vad"}},
                "output": {"voice": "vesper"},
            },
        },
    }
