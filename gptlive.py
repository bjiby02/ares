"""GPT-Live-1 session bootstrap.

Creates a full-duplex voice session whose only capabilities are the four tools in
tools.py. The model holds the conversation; the decision core owns every place
name. Without OPENAI_API_KEY this module reports not-configured and the Twilio
TwiML path keeps running, so the transport can be swapped without a rewrite.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import tools

API = "https://api.openai.com/v1/live/sessions"
MODEL = os.environ.get("ARES_LIVE_MODEL", "gpt-live-1")
TIMEOUT = 8.0


def configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def status(public_base: str) -> dict:
    """What the console and the operator need to know about the voice layer."""
    cfg = tools.session_config(public_base)
    cfg["model"] = MODEL
    return {
        "configured": configured(),
        "model": MODEL,
        "transport": "gpt-live/webrtc" if configured() else "twilio/twiml",
        "tool_endpoint": f"{public_base.rstrip('/')}/v1/tools",
        "tool_count": len(tools.SCHEMA),
        "tool_names": [t["name"] for t in tools.SCHEMA],
        "session": cfg,
        "reason": None if configured() else "OPENAI_API_KEY is not set",
        "note": ("gpt-live-1 is WebRTC only: a session needs an SDP offer from a "
                 "browser. There is no server-side session mint and no SIP dial-in "
                 "on this API surface."),
    }


def negotiate(public_base: str, sdp: str) -> dict:
    """Take the browser's SDP offer, hand back GPT-Live's answer.

    The session carries the tool schemas and the four-rule prompt, so the model
    arrives already unable to name a building.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return {"ok": False, "error": "OPENAI_API_KEY is not set"}
    if not sdp:
        return {"ok": False, "error": "an SDP offer is required"}
    session = tools.session_config(public_base)
    session["model"] = MODEL
    body = {"transport": {"type": "webrtc", "sdp": sdp}, "session": session}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": True, "answer": raw}          # bare SDP answer
        answer = (data.get("transport") or {}).get("sdp") or data.get("sdp")
        return {"ok": True, "answer": answer, "session": data}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"gpt-live negotiate {exc.code}: {detail}", flush=True)
        return {"ok": False, "error": f"HTTP {exc.code}", "detail": detail}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
