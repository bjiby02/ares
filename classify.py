"""Map one utterance onto an action. The model never names a destination."""

from __future__ import annotations

import json
import os
import re
import urllib.request

import jev

HAZARD_WORDS = (
    ("northeast", "NE"),
    ("northwest", "NW"),
    ("southeast", "SE"),
    ("southwest", "SW"),
    ("north", "N"),
    ("south", "S"),
    ("east", "E"),
    ("west", "W"),
)
HAZARD_CODES = {code for _, code in HAZARD_WORDS}
IMMOBILE_EXTRA = (
    "pinned", "crushed", "buried", "can't move", "cannot move", "cant move",
    "can't stand", "cannot stand", "can't walk", "cannot walk", "cant walk",
    "leg is broken", "legs are broken", "under a", "collapsed on",
    "door is blocked", "blocked in", "no way out", "windows barred",
)

TRAPPED = (
    "can't get out",
    "cannot get out",
    "cant get out",
    "trapped",
    "i'm stuck",
    "im stuck",
    "i am stuck",
    "can't breathe",
    "cannot breathe",
    "can't leave",
    "cannot leave",
    "can't walk",
    "cannot walk",
    "fire is inside",
    "fire inside",
    "not breathing",
    "stopped breathing",
    "unconscious",
    "heart attack",
)
MEDICAL = (
    "hurt",
    "injured",
    "bleeding",
    "blood",
    "chest pain",
    "broken",
    "dizzy",
)
FIRE = ("smoke", "fire", "flames", "wildfire", "burning")
WATER = ("flood", "flooding", "water is rising", "water rising")
VAGUE = {
    "here",
    "somewhere",
    "outside",
    "home",
    "my house",
    "the house",
    "this building",
    "a building",
}
DIRECTIONS = {"north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest"}
REPEAT = {
    "repeat",
    "say that again",
    "say again",
    "what did you say",
    "pardon",
    "come again",
}
CLOSE = {
    "i made it",
    "i'm safe",
    "im safe",
    "i am safe",
    "we are safe",
    "we're safe",
    "i got out",
    "i am out",
    "i'm out",
    "i'm safe now",
    "i am safe now",
}
CORRECT = {
    "no",
    "nope",
    "wrong",
    "wrong place",
    "that's wrong",
    "that is wrong",
    "not that one",
    "the other one",
}

PROMPT = """You route one emergency phone call. Reply with JSON only, no markdown:
{"action":"guide"|"escalate"|"ask"|"repeat"|"correct"|"close","need":"medical"|"fire"|"shelter"|null,"mobility":"mobile"|"trapped"|null,"hazard":"N"|"NE"|"E"|"SE"|"S"|"SW"|"W"|"NW"|null,"place":string|null}

escalate when the caller is trapped, cannot get out, cannot walk, is unconscious, or cannot breathe.
guide when they can move and need a safe building.
ask when they need help but did not say a place that can be found on a map.
repeat when they ask to hear the instruction again.
correct when they reject the place you named and do not give a new one.
close when they say they are safe or made it out.
need is medical when they are hurt, fire when there is smoke or fire, shelter when they need to get away from water.
place is their current location in their words. It is not a hospital and not a destination.
hazard is the direction of the smoke, fire, or flood, or null when they did not say one.
Do not invent a building, a road, or a hospital. Use null when they did not say it."""


def fallback(text: str) -> dict:
    low = " ".join(text.lower().split())
    if low in REPEAT:
        return _clean({"action": "repeat"})
    if low in CLOSE:
        return _clean({"action": "close"})
    if low in CORRECT:
        return _clean({"action": "correct"})
    hazard = _hazard(low)
    place = _place(text)
    need = _need(low)
    if any(phrase in low for phrase in TRAPPED):
        return _clean({"action": "escalate", "need": need, "mobility": "trapped", "hazard": hazard, "place": place})
    if not place:
        return _clean({"action": "ask", "need": need, "mobility": "mobile", "hazard": hazard, "place": None})
    return _clean({"action": "guide", "need": need, "mobility": "mobile", "hazard": hazard, "place": place})


def _need(low: str) -> str | None:
    if any(word in low for word in MEDICAL):
        return "medical"
    if any(word in low for word in FIRE):
        return "fire"
    if any(word in low for word in WATER):
        return "shelter"
    return None


def _hazard(low: str) -> str | None:
    window = low
    match = re.search(r"\b(smoke|fire|flames|flood|flooding)\b.{0,40}", low)
    if match:
        window = match.group(0)
    else:
        pointed = re.search(
            r"\b(?:to the|toward the|from the)\s+(northeast|northwest|southeast|southwest|north|south|east|west)\b",
            low,
        )
        if not pointed:
            return None
        window = pointed.group(0)
    for word, code in HAZARD_WORDS:
        if re.search(rf"\b{word}\b", window):
            return code
    return None


def place_candidates(text: str) -> list[str]:
    """Verbatim place spans. A model may choose one of these and nothing else."""
    found: list[str] = []
    pattern = re.compile(
        r"\b(?:at|near|inside|on|by|in)\s+(.+?)(?=$|[,.]|\s+\b(?:and|but|with|there|smoke|fire|flood)\b)",
        re.I,
    )
    for match in pattern.finditer(text):
        clean = _usable_place(match.group(1))
        if clean and clean not in found:
            found.append(clean)
    single = _place(text)
    if single and single not in found:
        found.insert(0, single)
    return found[:6]


def _place(text: str) -> str | None:
    match = re.search(r"\b(?:at|near|inside|on|by)\s+(.+)", text, re.I)
    if not match:
        match = re.search(r"\bin\s+(.+)", text, re.I)
    if not match:
        return None
    chunk = match.group(1)
    # Same terminators place_candidates already uses. Without the comma,
    # "at Geisel Library, I need to get out" is searched whole and fuzzy-matches
    # Geisler Library in Pella, Iowa.
    chunk = re.split(r"[,.]", chunk, maxsplit=1)[0]
    chunk = re.split(r"\b(?:and|but|with|there|smoke|fire|flood)\b", chunk, maxsplit=1, flags=re.I)[0]
    return _usable_place(chunk)


def _usable_place(chunk: str) -> str | None:
    clean = " ".join(chunk.strip(" ,.").split())
    folded = clean.casefold()
    if not clean or folded in VAGUE or folded in DIRECTIONS:
        return None
    if folded.startswith("the ") and folded[4:] in DIRECTIONS:
        return None
    return clean


def _ground_mobility(text: str, result: dict) -> dict:
    """Escalation must be grounded in words the caller actually said.

    Being hurt is not the same as being stuck. A model may not invent immobility,
    and it may not escalate without it. Medical need is always preserved, so a
    downgraded caller is routed to a hospital rather than told to stay still.
    """
    trapped = result.get("mobility") == "trapped"
    escalating = result.get("action") == "escalate"
    if not (trapped or escalating):
        return result
    low = " ".join(text.split()).casefold()
    if any(p in low for p in TRAPPED) or any(p in low for p in IMMOBILE_EXTRA):
        return result                      # they said it; believe them
    out = dict(result)
    out["mobility"] = "mobile"
    if any(word in low for word in MEDICAL):
        out["need"] = out.get("need") or "medical"
    out["action"] = "guide" if out.get("place") else "ask"
    print("escalation ungrounded: no immobility in the utterance, downgraded", flush=True)
    return out


def _ground(text: str, result: dict) -> dict:
    place = result.get("place")
    if not isinstance(place, str) or not place.strip():
        return result
    spoken = " ".join(text.split())
    place = " ".join(place.split())
    if place.casefold() not in spoken.casefold():
        result = dict(result)
        result["place"] = None
        return result
    trimmed = re.sub(r"^(?:beside|next to|around|outside of|outside)\s+", "", place, flags=re.I)
    if trimmed.casefold() in spoken.casefold():
        place = trimmed
    if place == result.get("place"):
        return result
    result = dict(result)
    result["place"] = place
    return result


def _clean(result: dict) -> dict:
    action = result.get("action")
    if action not in ("guide", "escalate", "ask", "repeat", "correct", "close"):
        action = "ask"
    need = result.get("need")
    if need not in ("medical", "fire", "shelter"):
        need = None
    mobility = result.get("mobility")
    if mobility not in ("mobile", "trapped"):
        mobility = "trapped" if action == "escalate" else None
    if mobility == "trapped":
        action = "escalate"
    hazard = result.get("hazard")
    if hazard not in HAZARD_CODES:
        hazard = None
    place = result.get("place")
    if not isinstance(place, str) or not place.strip() or place.strip().casefold() in VAGUE:
        place = None
    else:
        place = place.strip()
    if action == "guide" and place is None:
        action = "ask"
    return {"action": action, "need": need, "mobility": mobility, "hazard": hazard, "place": place}


def _openai(text: str) -> dict:
    body = json.dumps(
        {
            "model": os.environ.get("OPERATOR_MODEL", "gpt-4o-mini"),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }
    ).encode()
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode())
    return json.loads(payload["choices"][0]["message"]["content"])


def _anthropic(text: str) -> dict:
    body = json.dumps(
        {
            "model": os.environ.get("OPERATOR_MODEL", "claude-3-5-haiku-latest"),
            "max_tokens": 220,
            "temperature": 0,
            "system": PROMPT,
            "messages": [{"role": "user", "content": text}],
        }
    ).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode())
    raw = "".join(block.get("text", "") for block in payload.get("content", [])).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(raw)


def classify(text: str) -> dict:
    spans = place_candidates(text)
    if os.environ.get("TYPESAFE_API_KEY"):
        try:
            decision = _clean(_ground_mobility(text, _ground(text, jev.judge(text, spans))))
            print(
                f"jev action={decision['action']} need={decision['need']} hazard={decision['hazard']}",
                flush=True,
            )
            return decision
        except (OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"jev unavailable: {exc}", flush=True)
    try:
        if os.environ.get("OPENAI_API_KEY"):
            return _clean(_ground_mobility(text, _ground(text, _openai(text))))
        if os.environ.get("ANTHROPIC_API_KEY"):
            return _clean(_ground_mobility(text, _ground(text, _anthropic(text))))
    except (OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError):
        pass
    return fallback(text)
