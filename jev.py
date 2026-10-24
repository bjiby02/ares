"""Jev harness. The model answers narrow questions. This file decides what the call does."""

from __future__ import annotations

import json
import os
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# Walking someone is high stakes. Below this, ask instead of guiding.
PLACE_FLOOR = 0.6
# Missing a trapped caller is worse than an extra question.
TRAPPED_FLOOR = 0.6
ACT = 0.6
HAZARD_FLOOR = 0.55

HAZARD_CRITERIA = {
    "N": "The danger is to the north of the caller.",
    "NE": "The danger is to the northeast of the caller.",
    "E": "The danger is to the east of the caller.",
    "SE": "The danger is to the southeast of the caller.",
    "S": "The danger is to the south of the caller.",
    "SW": "The danger is to the southwest of the caller.",
    "W": "The danger is to the west of the caller.",
    "NW": "The danger is to the northwest of the caller.",
    "none": "The caller did not say a direction.",
}


def questions(spans: list[str]) -> dict:
    asked = {
        "trapped": {
            "type": "noul",
            "instructions": "The caller cannot get out, cannot move, cannot breathe, or is unconscious.",
        },
        "safe": {
            "type": "noul",
            "instructions": "The caller says they are safe now or have made it out.",
        },
        "repeat": {
            "type": "noul",
            "instructions": "The caller is asking to hear the last instruction again.",
        },
        "reject": {
            "type": "noul",
            "instructions": "The caller says the understood place is the wrong place.",
        },
        "medical": {
            "type": "noul",
            "instructions": "The caller is hurt, bleeding, or needs a hospital.",
        },
        "fire": {
            "type": "noul",
            "instructions": "There is smoke, fire, or flames.",
        },
        "water": {
            "type": "noul",
            "instructions": "There is flood water or rising water.",
        },
        "hazard": {
            "type": "choice",
            "instructions": "Which direction is the smoke, fire, or water from the caller?",
            "criteria": HAZARD_CRITERIA,
        },
    }
    if spans:
        criteria = {span: "These exact words are where the caller is." for span in spans}
        criteria["none"] = "None of these is a place the caller is at."
        asked["place"] = {
            "type": "choice",
            "instructions": "Which span is the caller's current location? A direction or a hospital is not their location.",
            "criteria": criteria,
        }
    return asked


def decide(spans: list[str], answers: dict) -> dict:
    """Turn Jev's probabilities into one action. No network."""
    trapped = _noul(answers, "trapped")
    safe = _noul(answers, "safe")
    repeat = _noul(answers, "repeat")
    reject = _noul(answers, "reject")
    hazard = _hazard(answers)
    place = _place(spans, answers)
    need = _need(answers)
    if safe >= 0.75 and trapped < TRAPPED_FLOOR:
        return _decision("close", None, None, hazard, None)
    if repeat >= 0.75 and trapped < TRAPPED_FLOOR:
        return _decision("repeat", None, None, hazard, None)
    if trapped >= TRAPPED_FLOOR:
        return _decision("escalate", "trapped", need, hazard, place)
    if reject >= 0.7 and place is None:
        return _decision("correct", "mobile", need, hazard, None)
    if place is None:
        return _decision("ask", "mobile", need, hazard, None)
    return _decision("guide", "mobile", need, hazard, place)


def judge(text: str, spans: list[str]) -> dict:
    payload = _post(text, questions(spans))
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Jev returned no answers")
    return decide(spans, answers)


def _post(text: str, asked: dict) -> dict:
    body = json.dumps(
        {
            "model": os.environ.get("OPERATOR_JEV_MODEL", "jev-latest"),
            "state": text,
            "questions": asked,
        }
    ).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        return json.loads(response.read().decode())


def _decision(action: str, mobility: str | None, need: str | None, hazard: str | None, place: str | None) -> dict:
    return {"action": action, "mobility": mobility, "need": need, "hazard": hazard, "place": place}


def _noul(answers: dict, name: str) -> float:
    item = answers.get(name) or {}
    if item.get("type") != "noul":
        return 0.0
    try:
        value = float(item["noul"])
    except (TypeError, ValueError, KeyError):
        return 0.0
    return min(1.0, max(0.0, value))


def _choice(answers: dict, name: str) -> tuple[str | None, float]:
    item = answers.get(name) or {}
    if item.get("type") != "choice":
        return None, 0.0
    try:
        confidence = float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    choice = item.get("choice")
    if not isinstance(choice, str):
        return None, 0.0
    return choice, min(1.0, max(0.0, confidence))


def _hazard(answers: dict) -> str | None:
    choice, confidence = _choice(answers, "hazard")
    if choice not in HAZARD_CRITERIA or choice == "none" or confidence < HAZARD_FLOOR:
        return None
    return choice


def _place(spans: list[str], answers: dict) -> str | None:
    choice, confidence = _choice(answers, "place")
    if choice not in spans or confidence < PLACE_FLOOR:
        return None
    return choice


def _need(answers: dict) -> str | None:
    if _noul(answers, "medical") >= ACT:
        return "medical"
    if _noul(answers, "fire") >= ACT:
        return "fire"
    if _noul(answers, "water") >= ACT:
        return "shelter"
    return None
