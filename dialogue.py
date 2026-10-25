"""One turn of a phone call. No network and no database in here."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace

import geo
import swarm

OPENING = "I'm here. Tell me what is happening, and where you are."


@dataclass
class CallState:
    phone: str
    call_sid: str = ""
    caller_city: str | None = None
    caller_state: str | None = None
    place: str | None = None
    origin_name: str | None = None
    origin_label: str | None = None
    origin_lat: float | None = None
    origin_lon: float | None = None
    hazard: str | None = None
    need: str | None = None
    mobility: str | None = None
    pending: list[dict] | None = None
    last_say: str | None = None
    story: str | None = None
    incident_id: int | None = None
    incident_status: str | None = None
    help_sent: str | None = None
    destination_name: str | None = None


@dataclass
class Effect:
    say: str
    state: CallState
    sms: str = ""
    responder_sms: str = ""
    incident: dict | None = None
    listen: bool = True
    prompt: str = ""
    action: str = "ask"


def state_from(raw: dict | None, phone: str) -> CallState:
    if not raw:
        return CallState(phone=phone)
    known = {item.name for item in fields(CallState)}
    data = {key: raw.get(key) for key in known}
    data["phone"] = raw.get("phone") or phone
    return CallState(**data)


def state_to(state: CallState) -> dict:
    return {item.name: getattr(state, item.name) for item in fields(CallState)}


OPEN_STATUS = ("escalated", "guided", "holding")


def _resume(state: CallState) -> str:
    """What a caller hears when they dial back on a call that is still open.

    They already told us this. Asking again wastes the one thing they do not
    have. The line picks up where it stopped, then asks one short question.
    """
    where = state.origin_label or state.place
    opening = f"I still have your call from {where}." if where else "I still have your call."
    helper = {
        "Ambulance": "An ambulance is on the way to you.",
        "Fire engine": "A fire engine is on the way to you.",
    }.get(state.help_sent or "", "A rescue team is on the way to you." if state.help_sent else "")
    if state.incident_status == "escalated":
        tail = "Are you still inside?"
    elif state.destination_name:
        helper = helper or ""
        opening = f"{opening} I sent you toward {state.destination_name}."
        tail = "Are you on your way?"
    else:
        tail = "Has anything changed?"
    return " ".join(part for part in (opening, helper, tail) if part)


def greet(state: CallState) -> Effect:
    if state.pending and len(state.pending) >= 2:
        first = state.pending[0].get("label") or state.pending[0].get("name")
        second = state.pending[1].get("label") or state.pending[1].get("name")
        return Effect(say=f"Which one? {first}, or {second}?", state=state, action="ask")
    if state.last_say and state.incident_status in OPEN_STATUS:
        return Effect(say="", state=state, action="greet")
    if state.incident_status in OPEN_STATUS:
        line = _resume(state)
        return Effect(say=line, state=replace(state, last_say=line), action="resume")
    return Effect(say=OPENING, state=state, action="greet")


def step(state: CallState, said: str, *, classify, origins, route, nearby=None) -> Effect:
    text = " ".join(said.split())
    state = _note(state, text)
    if _bare(text, "repeat"):
        return _repeat(state)
    if _bare(text, "close"):
        return _close(state, text)
    if state.pending:
        return _from_pending(state, text, classify, origins, route, nearby)
    if _bare(text, "correct"):
        return _reject(state)
    return _from_decision(state, text, classify(text), origins, route, nearby)


def _from_pending(state: CallState, text: str, classify, origins, route, nearby=None) -> Effect:
    hits = [geo.hit_from_dict(item) for item in state.pending or []]
    pick = match_choice(text, hits)
    decision = classify(text)
    if decision["action"] == "escalate":
        if pick is not None:
            state = _adopt(state, pick)
        elif decision.get("place") and decision["place"].casefold() != (state.place or "").casefold():
            state = _take(replace(state, pending=None, origin_lat=None, origin_lon=None), decision)
        return _escalate(state, text, origins)
    if pick is not None:
        kept = dict(decision)
        kept["place"] = None
        kept["action"] = "guide"
        return _finish(_adopt(_take(state, kept), pick), text, route, nearby)
    if decision.get("place") and decision["place"].casefold() != (state.place or "").casefold():
        cleared = replace(state, pending=None, origin_lat=None, origin_lon=None, origin_name=None, origin_label=None)
        return _from_decision(cleared, text, decision, origins, route, nearby)
    if decision["action"] == "close":
        return _close(state, text)
    if (decision["action"] == "correct" or _rejects(text)) and not decision.get("place"):
        return _reject(state)
    if len(hits) >= 2:
        say = f"Which one? {hits[0].label}, or {hits[1].label}?"
    elif hits:
        say = f"Say if you mean {hits[0].label}."
    else:
        say = "Where are you?"
    return Effect(say=say, state=replace(state, last_say=say), action="ask")


def _from_decision(state: CallState, text: str, decision: dict, origins, route, nearby=None) -> Effect:
    if decision["action"] == "repeat":
        return _repeat(state)
    if decision["action"] == "close":
        return _close(state, text)
    if (decision["action"] == "correct" or _rejects(text)) and not decision.get("place"):
        return _reject(state)
    state = _take(state, decision)
    if decision["action"] == "escalate" or state.mobility == "trapped":
        return _escalate(state, text, origins)
    if not state.place:
        return Effect(say="Where are you?", state=state, action="ask")
    if state.origin_lat is None:
        return _resolve(state, text, origins, route, nearby)
    return _finish(state, text, route, nearby)


def _resolve(state: CallState, text: str, origins, route, nearby=None) -> Effect:
    try:
        hits = origins(state.place)
    except geo.LookupError:
        return _outage(state, text)
    chosen = geo.narrow(state.place or "", hits or [], state.caller_city, state.caller_state)
    if not chosen:
        return Effect(say="I could not find that place. Where are you?", state=replace(state, place=None), action="ask")
    if len(chosen) > 1:
        pending = [geo.hit_to_dict(hit) for hit in chosen]
        say = f"I found {chosen[0].label}, and {chosen[1].label}. Which one?"
        return Effect(say=say, state=replace(state, pending=pending, last_say=say), action="ask")
    return _finish(_adopt(state, chosen[0]), text, route, nearby)


def _finish(state: CallState, text: str, route, nearby=None) -> Effect:
    heard = None
    if state.hazard is None and nearby is not None:
        # This caller named no direction. Earlier callers nearby may already have.
        heard = swarm.infer(nearby(state.origin_lat, state.origin_lon),
                            state.origin_lat, state.origin_lon)
        if heard is not None:
            # A defaulted "shelter" is not a claim about the hazard. What the
            # neighbours actually saw outranks it. A stated need does not.
            need = heard.kind if state.need in (None, "shelter") else state.need
            state = replace(state, hazard=heard.hazard, need=need)
    try:
        dest = route(state.origin_lat, state.origin_lon, state.hazard, state.need,
                     state.origin_label or state.origin_name or state.place)
    except geo.LookupError:
        return _outage(state, text)
    if dest is None:
        if state.hazard:
            direction = "I will not send you toward the danger. Stay in a solid building, or move away from it on foot."
            return _holding(state, text, direction, refused=True)
        direction = "I could not find a staffed building nearby. Tell me another landmark."
        return _holding(state, text, direction)
    origin = state.origin_label or state.place or "there"
    direction = f"From {origin}. {geo.instruction(dest, _hazard_kind(state))}"
    if heard is not None:
        # Say where it came from. The caller is being told something they did not report.
        direction = f"{heard.phrase} {direction}"
    status = "guided"
    incident = _record(state, status, _priority(status, state.need), text, dest, direction,
                       hazard_inferred=heard is not None)
    state, direction, incident = _speak_help(state, direction, incident)
    say = _once(state, direction)
    saved = replace(state, last_say=direction, incident_status=status, pending=None,
                    destination_name=dest.name)
    return Effect(
        say=say,
        state=saved,
        incident=incident,
        action="guide",
    )


def _escalate(state: CallState, text: str, origins) -> Effect:
    state = replace(state, mobility="trapped", pending=None)
    if state.place and state.origin_lat is None:
        try:
            hits = origins(state.place) or []
        except geo.LookupError:
            hits = []
        chosen = geo.narrow(state.place, hits, state.caller_city, state.caller_state) if hits else []
        if chosen:
            state = _adopt(state, chosen[0])
    line = _stay(state.need)
    if state.origin_label:
        heard = f"I have you at {state.origin_label}."
    elif state.place:
        heard = f"I have you at {state.place}."
    else:
        heard = "What building are you in?"
    direction = f"{line} {heard}"
    update = state.incident_status == "escalated"
    incident = _record(state, "escalated", 0, text, None, line)
    state, direction, incident = _speak_help(state, direction, incident)
    say = _once(state, direction)
    saved = replace(state, last_say=direction, incident_status="escalated")
    return Effect(
        say=say,
        state=saved,
        responder_sms=_page(saved, text, update),
        incident=incident,
        listen=True,
        action="escalate",
    )


def _holding(state: CallState, text: str, direction: str, *, refused: bool = False) -> Effect:
    incident = _record(state, "holding", _priority("holding", state.need, refused=refused),
                       text, None, direction)
    state, direction, incident = _speak_help(state, direction, incident)
    say = _once(state, direction)
    saved = replace(state, last_say=direction, incident_status="holding", pending=None)
    return Effect(
        say=say,
        state=saved,
        incident=incident,
        action="hold",
    )


def _outage(state: CallState, text: str) -> Effect:
    direction = "I cannot look up a route right now. If you can move, get away from the danger. If you cannot get out, stay low."
    return _holding(state, text, direction)


def _reject(state: CallState) -> Effect:
    cleared = replace(
        state,
        place=None,
        origin_name=None,
        origin_label=None,
        origin_lat=None,
        origin_lon=None,
        pending=None,
        last_say="Where are you?",
        destination_name=None,
    )
    return Effect(say="Where are you?", state=cleared, action="correct")


def _once(state: CallState, direction: str) -> str:
    """Say an instruction one time. A later turn does not read it again."""
    if direction and direction == (state.last_say or ""):
        return ""
    return direction


def _repeat(state: CallState) -> Effect:
    say = state.last_say or "Where are you?"
    return Effect(say=say, state=state, prompt="Say if that place is wrong.", action="repeat")


def _close(state: CallState, text: str) -> Effect:
    if not state.incident_id:
        return Effect(
            say="You do not have an open incident. Tell me what is happening, and where you are.",
            state=state,
            action="close",
        )
    page = ""
    if state.incident_status == "escalated":
        page = f"OPERATOR incident {{incident}} closed. Caller says they are safe.\n{state.phone}"
    say = "Call again if you need to."
    incident = _record(state, "closed", 0 if state.incident_status == "escalated" else 4, text, None, "closed")
    saved = replace(state, incident_status="closed", pending=None, last_say=say)
    return Effect(say=say, state=saved, responder_sms=page, incident=incident, listen=False, action="close")


def _take(state: CallState, decision: dict) -> CallState:
    data: dict = {}
    if decision.get("hazard"):
        data["hazard"] = decision["hazard"]
    if decision.get("need"):
        data["need"] = decision["need"]
    elif decision.get("action") == "guide" and not state.need:
        data["need"] = "shelter"
    if decision.get("mobility") == "trapped":
        data["mobility"] = "trapped"
    elif decision.get("mobility") and state.mobility != "trapped":
        data["mobility"] = decision["mobility"]
    elif decision.get("action") == "guide" and not state.mobility:
        data["mobility"] = "mobile"
    place = decision.get("place")
    if place and place.casefold() != (state.place or "").casefold():
        data.update(
            place=place,
            origin_name=None,
            origin_label=None,
            origin_lat=None,
            origin_lon=None,
            pending=None,
        )
    return replace(state, **data) if data else state


def _adopt(state: CallState, hit: geo.Hit) -> CallState:
    return replace(
        state,
        place=state.place or hit.name,
        origin_name=hit.name,
        origin_label=hit.label,
        origin_lat=hit.lat,
        origin_lon=hit.lon,
        pending=None,
    )


def _record(state: CallState, status: str, priority: int, summary: str, dest: geo.Place | None,
            instruction: str, hazard_inferred: bool = False) -> dict:
    return {
        "hazard_inferred": hazard_inferred,
        "status": status,
        "priority": priority,
        "need": state.need,
        "mobility": state.mobility,
        "place_text": state.place,
        "place_name": state.origin_label or state.origin_name or state.place,
        "lat": state.origin_lat,
        "lon": state.origin_lon,
        "hazard": state.hazard,
        "destination_name": dest.name if dest else None,
        "destination_lat": dest.lat if dest else None,
        "destination_lon": dest.lon if dest else None,
        "instruction": instruction,
        "summary": (state.story or " ".join(summary.split()))[:500],
        "phone": state.phone,
        "call_sid": state.call_sid,
    }


def _page(state: CallState, summary: str, update: bool) -> str:
    place = state.origin_label or state.place or "place unknown"
    hazard = state.hazard or "unknown"
    words = " ".join(summary.split())
    if len(words) > 180:
        words = words[:177] + "..."
    kind = "update" if update else "incident"
    return f"OPERATOR P0 {kind} {{incident}}\n{state.phone}\n{place}\nhazard {hazard}\n{words}"


def _priority(status: str, need: str | None, *, refused: bool = False) -> int:
    """Where this call sits in the queue a human works down.

    A refused caller shares the top band with a trapped one. We told them every
    way out was unsafe, so staying put is the whole of our advice. Only a person
    can improve on that, which makes them the most dispatcher-shaped call we have.
    """
    if status == "escalated" or refused:
        return 0
    if need == "medical":
        return 1
    return 2


def _unit(state: CallState) -> str | None:
    """The help this call needs. Chosen here, never by a second model call."""
    if state.need == "medical":
        return "Ambulance"
    if state.need == "fire":
        return "Fire engine"
    if state.need == "shelter" or state.mobility == "trapped":
        return "Rescue team"
    return None


def _speak_help(state: CallState, direction: str, incident: dict) -> tuple[CallState, str, dict]:
    """The agent takes the call. The unit is spoken once, in this same sentence."""
    if state.help_sent or not (state.origin_label or state.place):
        return state, direction, incident
    unit = _unit(state)
    if not unit:
        return state, direction, incident
    direction = f"{direction} {_help_line(unit)}"
    incident = dict(incident)
    incident["auto_help"] = unit
    return replace(state, help_sent=unit), direction, incident


def _help_line(unit: str) -> str:
    if unit == "Ambulance":
        return "An ambulance is on the way to you."
    if unit == "Fire engine":
        return "A fire engine is on the way to you."
    return "A rescue team is on the way to you."


def _stay(need: str | None) -> str:
    if need == "shelter":
        return "Get to the highest point you can inside."
    if need == "medical":
        return "Stay still."
    return "Stay low. Do not open the door."


def _hazard_kind(state: CallState) -> str | None:
    if not state.hazard:
        return None
    if state.need == "shelter":
        return "water"
    if state.need in ("fire", "medical"):
        return "smoke"
    return None


def _note(state: CallState, text: str) -> CallState:
    if not text or _bare(text, "repeat") or _bare(text, "correct"):
        return state
    if not state.story:
        return replace(state, story=text)
    if len(text) >= 24:
        return replace(state, story=f"{state.story} / {text}"[:500])
    return state


def _bare(text: str, kind: str) -> bool:
    low = " ".join(text.casefold().split())
    if kind == "repeat":
        return low in {"repeat", "say that again", "say again", "what did you say", "pardon", "come again"}
    if kind == "close":
        if any(phrase in low for phrase in ("i made it", "i'm safe", "i am safe", "im safe", "we're safe", "we are safe")):
            return True
        return low in {"i got out", "i am out", "i'm out"}
    return low in {"no", "nope", "wrong", "wrong place", "that's wrong", "that is wrong", "not that one", "the other one"}


def _rejects(text: str) -> bool:
    low = " ".join(text.casefold().split())
    if low.startswith("no ") or low.startswith("no,") or low.startswith("wrong"):
        return True
    return any(phrase in low for phrase in ("wrong place", "not that", "not right"))


def match_choice(said: str, pending: list[geo.Hit]) -> geo.Hit | None:
    if not pending:
        return None
    low = said.casefold()
    if re.search(r"\b(first|number one)\b", low):
        return pending[0]
    if len(pending) > 1 and re.search(r"\b(second|number two)\b", low):
        return pending[1]
    bags: list[set[str]] = []
    for hit in pending:
        tokens: set[str] = set()
        for part in (hit.name, hit.city, hit.state, hit.district, hit.label):
            tokens.update(token for token in re.findall(r"[a-z0-9]+", part.casefold()) if len(token) > 2)
        bags.append(tokens)
    said_tokens = set(re.findall(r"[a-z0-9]+", low))
    unique_hits = []
    for index, tokens in enumerate(bags):
        others: set[str] = set()
        for other in range(len(bags)):
            if other != index:
                others |= bags[other]
        unique_hits.append(len(said_tokens & (tokens - others)))
    if max(unique_hits) < 1 or unique_hits.count(max(unique_hits)) != 1:
        return None
    return pending[unique_hits.index(max(unique_hits))]
