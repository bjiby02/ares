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


def signed_url(request_url: str) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
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
    voice_name = os.environ.get("OPERATOR_VOICE", "Polly.Joanna")
    action = xml(f"{base.rstrip('/')}{follow}")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Response>",
        f'<Say voice="{xml(voice_name)}">{xml(say)}</Say>',
    ]
    # Voice only. No <Message> is ever emitted: the caller is told everything
    # out loud, and a person running from a fire is not reading a text.
    if listen:
        hint = xml("repeat, wrong place, trapped, smoke, fire, flood")
        inner = f"<Say voice=\"{xml(voice_name)}\">{xml(prompt)}</Say>" if prompt else ""
        parts.append(
            f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto" '
            f'timeout="8" language="en-US" hints="{hint}">{inner}</Gather>'
        )
    parts.append("<Hangup/>")
    parts.append("</Response>")
    return "".join(parts)
