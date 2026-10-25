"""TwiML for one turn. The caller hears speech. Texts are a side effect of the call."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from urllib.parse import urlparse


def xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def e164(number: str) -> str:
    cleaned = number.strip().replace(" ", "").replace("-", "")
    if re.fullmatch(r"\+\d{8,15}", cleaned):
        return cleaned
    return ""


def plain_base(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1].strip().rstrip("/")
    return value


def signed_url(request_url: str) -> str:
    configured = plain_base(os.environ.get("PUBLIC_BASE_URL", ""))
    path = urlparse(request_url).path or "/"
    if configured:
        return configured + path
    return request_url.split("?", 1)[0]


def twilio_ok(request_url: str, params: dict[str, str], signature: str) -> bool:
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not token:
        return True
    if not signature:
        return False
    signed = signed_url(request_url) + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(token.encode(), signed.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    if len(expected) != len(signature):
        return False
    return hmac.compare_digest(expected, signature)


def twiml(
    base: str,
    say: str,
    sms: str = "",
    responder_sms: str = "",
    responder_to: str = "",
    listen: bool = True,
    prompt: str = "",
    follow: str = "/gather",
) -> str:
    voice_name = os.environ.get("OPERATOR_VOICE", "Polly.Joanna-Neural")
    action = xml(f"{base.rstrip('/')}{follow}")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Response>",
    ]
    # Voice only. No <Message> is ever emitted: the caller is told everything
    # out loud, and a person running from a fire is not reading a text.
    spoken = f'<Say voice="{xml(voice_name)}">{xml(say)}</Say>'
    if listen:
        # A trial keypress must come back to this line. A speech-only gather
        # treats that key as the end of the call and falls through to Hangup.
        hint = xml("repeat, wrong place, trapped, smoke, fire, flood")
        inner = f"<Say voice=\"{xml(voice_name)}\">{xml(prompt)}</Say>" if prompt else ""
        parts.append(
            f'<Gather input="speech dtmf" action="{action}" method="POST" speechTimeout="auto" '
            f'actionOnEmptyResult="true" timeout="8" language="en-US" hints="{hint}">'
            f"{spoken}{inner}</Gather>"
        )
        parts.append(f'<Redirect method="POST">{action}</Redirect>')
    else:
        parts.append(spoken)
        parts.append("<Hangup/>")
    parts.append("</Response>")
    return "".join(parts)


def live_stream(url: str, caller: dict[str, str]) -> str:
    """Answer a phone call by handing the audio to GPT-Live. No gather loop."""
    pairs = (
        ("token", caller.get("token") or ""),
        ("from", caller.get("From") or caller.get("from") or ""),
        ("city", caller.get("FromCity") or caller.get("city") or ""),
        ("region", caller.get("FromState") or caller.get("region") or ""),
        ("call", caller.get("CallSid") or caller.get("call") or ""),
    )
    params = "".join(
        f'<Parameter name="{xml(name)}" value="{xml(value)}"/>'
        for name, value in pairs if value
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{xml(url)}">{params}</Stream>'
        "</Connect></Response>"
    )


def unavailable() -> str:
    """Spoken only when GPT-Live itself cannot be reached."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Say voice="Polly.Joanna">The line cannot take this call right now.</Say>'
        "<Hangup/>"
        "</Response>"
    )


def hold(base: str) -> str:
    """Keep a trapped caller on the line without asking them to talk."""
    action = xml(f"{base.rstrip('/')}/gather")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Pause length="4"/>'
        f'<Redirect method="POST">{action}</Redirect>'
        "</Response>"
    )
