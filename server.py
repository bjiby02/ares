"""Operator. The caller dials. This process triages the call and keeps the incident."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import traceback
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.request

import classify
import dialogue
import feeds
import gptlive
import directory
import store
import tools
import voice

ROOT = Path(__file__).resolve().parent
CERT_DIR = ROOT / "devcert"
HTTP_PORT = 8810
HTTPS_PORT = 8457
STORE: store.Store | None = None
_OPEN = threading.Lock()


def lan_ip() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 1))
        ip = sock.getsockname()[0]
        sock.close()
    except OSError:
        return None
    if ip.startswith("127."):
        return None
    return ip


def ensure_cert(ip: str | None) -> tuple[Path, Path]:
    CERT_DIR.mkdir(exist_ok=True)
    names = ["DNS:localhost", "IP:127.0.0.1"]
    if ip:
        names.append(f"IP:{ip}")
    san = ",".join(names)
    cert = CERT_DIR / "cert.pem"
    key = CERT_DIR / "key.pem"
    stamp = CERT_DIR / "san.txt"
    if cert.exists() and key.exists() and stamp.exists() and stamp.read_text() == san:
        return cert, key
    config = CERT_DIR / "san.cnf"
    config.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=req_distinguished_name",
                "x509_extensions=v3_req",
                "prompt=no",
                "[req_distinguished_name]",
                "CN=Operator",
                "[v3_req]",
                f"subjectAltName={san}",
                "keyUsage=digitalSignature,keyEncipherment",
                "extendedKeyUsage=serverAuth",
                "",
            ]
        )
    )
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "3",
            "-nodes", "-keyout", str(key), "-out", str(cert),
            "-config", str(config),
        ],
        check=True,
        capture_output=True,
    )
    stamp.write_text(san)
    return cert, key


def api_token() -> str:
    env = os.environ.get("OPERATOR_API_TOKEN", "").strip()
    if env:
        return env
    path = ROOT / "data" / "api_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(18)
    path.write_text(token + "\n")
    return token


def open_store() -> store.Store:
    global STORE
    with _OPEN:
        if STORE is None:
            path = os.environ.get("OPERATOR_DB", str(ROOT / "data" / "operator.db"))
            STORE = store.Store(path)
            directory.bind(STORE)
        return STORE


def public_base(handler: BaseHTTPRequestHandler) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    host = handler.headers.get("Host", f"127.0.0.1:{HTTP_PORT}")
    scheme = "https" if isinstance(handler.connection, ssl.SSLSocket) else "http"
    return f"{scheme}://{host}"


def authorized(handler: BaseHTTPRequestHandler) -> bool:
    expected = api_token()
    header = handler.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = parse_qs(urlparse(handler.path).query).get("token", [""])[-1]
    if len(supplied) != len(expected):
        return False
    return secrets.compare_digest(supplied, expected)


def _render(base: str, say: str, sms: str, page: str, listen: bool, prompt: str) -> str:
    return voice.twiml(
        base,
        say,
        sms,
        page,
        os.environ.get("OPERATOR_RESPONDER_NUMBER", ""),
        listen,
        prompt,
    )


def converse(form: dict[str, str], base: str) -> store.Result:
    db = open_store()
    said = " ".join((form.get("SpeechResult") or "").split())
    call_sid = (form.get("CallSid") or "").strip()
    phone = (form.get("From") or form.get("phone") or call_sid or "local").strip()
    key = hashlib.sha256(f"{call_sid}\n{said}".encode()).hexdigest() if call_sid and said else None
    if key:
        cached = db.cached(key)
        if cached:
            print(f"replay phone={phone}", flush=True)
            return cached
    state = db.load(phone)
    updates: dict = {"call_sid": call_sid or state.call_sid}
    city = form.get("FromCity") or form.get("CallerCity") or os.environ.get("OPERATOR_AREA_CITY")
    region = form.get("FromState") or form.get("CallerState") or os.environ.get("OPERATOR_AREA_REGION")
    if city:
        updates["caller_city"] = city
    if region:
        updates["caller_state"] = region
    state = replace(state, **updates)

    # Responder news reaches the caller on their very next turn, and a supervised
    # incident means a human has the call: the model stops answering.
    pending_news = ""
    live = db.open_for(phone)
    if live:
        if live.get("supervised"):
            say = ("A dispatcher is on this call with you now. Stay on the line.")
            body = _render(base, say, "", "", True, "")
            return store.Result(body, live["id"], False, False, say, "")
        if not live.get("caller_told") and live.get("responder_state"):
            news = RESPONDER_LINES.get(live["responder_state"], "").format(id=live["id"])
            if news:
                db.mark_told(int(live["id"]))
                if not said:
                    body = _render(base, news, "", "", True, "Tell me if anything has changed.")
                    return store.Result(body, live["id"], False, False, news, "")
                pending_news = news

    if not said:
        effect = dialogue.greet(state)
        body = _render(base, effect.say, "", "", effect.listen, effect.prompt)
        return store.Result(body, state.incident_id, False, False, effect.say, "")
    effect = dialogue.step(
        state,
        said,
        classify=classify.classify,
        origins=directory.search,
        route=directory.route,
        nearby=lambda lat, lon: db.reports(exclude_phone=state.phone),
    )

    def render(say: str, sms: str, page: str, listen: bool, prompt: str) -> str:
        return _render(base, say, sms, page, listen, prompt)

    if pending_news:
        effect = replace(effect, say=f"{pending_news} {effect.say}")
    result = db.commit(key, effect, said, render)
    print(f"turn phone={phone} action={effect.action} incident={result.incident_id}", flush=True)
    if result.fresh and result.changed and result.incident_id:
        fanout(result.incident_id)
    return result


RESPONDER_LINES = {
    "claimed": "Incident {id}. A dispatcher has your report and is looking at it now.",
    "en_route": "Incident {id}. Help is on the way to you.",
    "on_scene": "Incident {id}. Responders are at your location now.",
    "resolved": "Incident {id} is closed. Call again if you need to.",
    "released": "Incident {id}. You are back with the automated line.",
}


def notify_caller(incident: dict) -> None:
    """Close the loop: the caller is told the moment a responder moves.

    Uses Twilio REST when a number is configured. Without one the line is still
    recorded on the incident, so the console shows exactly what the caller would
    have received.
    """
    state = incident.get("responder_state") or ""
    line = RESPONDER_LINES.get(state)
    if not line:
        return
    text = line.format(id=incident.get("id"))
    # Voice only. The news is owed until the caller's next turn speaks it.
    print(f"caller news queued incident={incident.get('id')} line={text!r}", flush=True)


def fanout(incident_id: int) -> None:
    url = os.environ.get("OPERATOR_INCIDENT_WEBHOOK", "").strip()
    if not url:
        return
    record = open_store().incident(incident_id)
    if record is None:
        return
    body = json.dumps({"event": "incident.updated", "incident": record}).encode()

    def send() -> None:
        try:
            request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=1.5).read()
        except OSError as exc:
            print(f"webhook failed: {exc}", flush=True)

    threading.Thread(target=send, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b'{"ok":true,"service":"operator"}', "application/json")
            return
        if path == "/voice":
            self._twilio(self._query())
            return
        if path == "/preview":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._preview()
            return
        if path in ("/talk", "/talk.html", "/talk.js"):
            self._console(path)
            return
        if path in ("/console", "/console.html", "/console.js"):
            self._console(path)
            return
        if path == "/v1/trace":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._send(200, json.dumps({
                "trace": tools.trace(),
                "voice": gptlive.status(public_base(self)),
            }).encode(), "application/json")
            return
        if path == "/v1/voice":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._send(200, json.dumps(gptlive.status(public_base(self))).encode(),
                       "application/json")
            return
        if path == "/v1/tools/schema":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            body = json.dumps({"tools": tools.SCHEMA, "instructions": tools.PROMPT,
                               "session": tools.session_config(public_base(self)),
                               "session_update": tools.session_update()}).encode()
            self._send(200, body, "application/json")
            return
        if path == "/v1/situation":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._situation()
            return
        if path == "/v1/incidents":
            self._incidents()
            return
        if path.startswith("/v1/incidents/"):
            self._incident(path.rsplit("/", 1)[-1])
            return
        if path in ("", "/"):
            self._send(200, b"Operator answers the phone. Point Twilio Voice at /voice.\n", "text/plain; charset=utf-8")
            return
        self._send(404, b"Not found\n", "text/plain")

    def _situation(self) -> None:
        """Cached live feeds for the console. Never blocks on a cold network."""
        spot = feeds.area()
        if not spot:
            self._send(503, b'{"error":"OPERATOR_AREA_CITY is not set"}', "application/json")
            return
        body = json.dumps(feeds.situation(spot["lat"], spot["lon"])).encode()
        self._send(200, body, "application/json")

    def _console(self, path: str) -> None:
        """The dispatch console. Static, token is supplied by the query string."""
        if "talk" in path:
            name = "talk.js" if path.endswith(".js") else "talk.html"
        else:
            name = "console.js" if path.endswith(".js") else "console.html"
        kind = "application/javascript" if name.endswith(".js") else "text/html; charset=utf-8"
        try:
            body = (ROOT / "web" / name).read_bytes()
        except OSError:
            self._send(404, b"console not built\n", "text/plain")
            return
        self._send(200, body, kind)

    def _respond(self, raw_id: str) -> None:
        """A dispatcher acts on an incident. This is the responder half of the loop."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode() if length else ""
        try:
            payload = json.loads(body) if body.lstrip().startswith("{") else {
                k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        except json.JSONDecodeError:
            payload = {}
        try:
            incident_id = int(raw_id)
        except ValueError:
            self._send(400, b'{"error":"bad incident id"}', "application/json")
            return
        state = (payload.get("state") or "").strip()
        updated = open_store().respond(
            incident_id, state,
            who=(payload.get("who") or "dispatch").strip(),
            note=(payload.get("note") or "").strip(),
            supervised=payload.get("supervised"),
        )
        if updated is None:
            self._send(400, json.dumps({
                "error": "unknown incident or state",
                "states": list(store.Store.RESPONDER_STATES)}).encode(), "application/json")
            return
        print(f"responder {state} incident={incident_id} by={updated.get('responder')}", flush=True)
        notify_caller(updated)
        fanout(incident_id)
        self._send(200, json.dumps({"ok": True, "incident": updated}).encode(), "application/json")

    def _tool(self, name: str) -> None:
        """One tool call from the voice model. Returns the string it must speak."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        payload: dict = {}
        if raw.lstrip().startswith("{"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
        else:
            parsed = parse_qs(raw, keep_blank_values=True)
            payload = {k: v[-1] for k, v in parsed.items()}
        args = payload.get("arguments")
        if isinstance(args, str):
            try:
                payload.update(json.loads(args))
            except json.JSONDecodeError:
                pass
        elif isinstance(args, dict):
            payload.update(args)
        phone = (payload.get("phone") or payload.get("call_id") or "live").strip()
        spoken = payload.get("spoken") or payload.get("text") or ""
        try:
            out = tools.call(open_store(), name, phone, spoken)
        except Exception as exc:  # a tool must never break the call
            traceback.print_exc()
            out = {"ok": False, "say": "I did not catch that. Tell me what is happening, "
                                       "and where you are.", "error": str(exc)}
        self._send(200, json.dumps(out).encode(), "application/json")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/live/offer":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            sdp = self.rfile.read(length).decode() if length else ""
            out = gptlive.negotiate(public_base(self), sdp)
            self._send(200 if out.get("ok") else 502,
                       json.dumps(out).encode(), "application/json")
            return
        if path.startswith("/v1/incidents/") and path.endswith("/respond"):
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._respond(path.split("/")[3])
            return
        if path.startswith("/v1/tools/"):
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._tool(path.rsplit("/", 1)[-1])
            return
        if path not in ("/voice", "/gather"):
            self._send(404, b"Not found\n", "text/plain")
            return
        form = self._form()
        request_url = public_base(self) + path
        if not voice.twilio_ok(request_url, form, self.headers.get("X-Twilio-Signature", "")):
            self._send(403, b"", "text/plain")
            return
        self._twilio(form)

    def _twilio(self, form: dict[str, str]) -> None:
        try:
            result = converse(form, public_base(self))
        except Exception:
            traceback.print_exc()
            fallback = voice.twiml(public_base(self), "I did not catch that. Tell me what is happening, and where you are.")
            self._send(200, fallback.encode(), "text/xml")
            return
        self._send(200, result.twiml.encode(), "text/xml")

    def _preview(self) -> None:
        query = self._query()
        form = {
            "SpeechResult": query.get("q", ""),
            "From": query.get("phone", "demo"),
            "FromCity": query.get("city", ""),
            "FromState": query.get("region", ""),
        }
        try:
            result = converse(form, public_base(self))
        except Exception:
            traceback.print_exc()
            self._send(502, b'{"error":"lookup failed"}\n', "application/json")
            return
        body = json.dumps(
            {"say": result.say, "sms": result.sms, "incident": result.incident_id},
            indent=2,
        ).encode()
        self._send(200, body, "application/json")

    def _incidents(self) -> None:
        if not authorized(self):
            self._send(401, b"token required\n", "text/plain")
            return
        body = json.dumps({"incidents": open_store().incidents()}, indent=2).encode()
        self._send(200, body, "application/json")

    def _incident(self, raw_id: str) -> None:
        if not authorized(self):
            self._send(401, b"token required\n", "text/plain")
            return
        if not raw_id.isdigit():
            self._send(404, b"Not found\n", "text/plain")
            return
        record = open_store().incident(int(raw_id))
        if record is None:
            self._send(404, b"Not found\n", "text/plain")
            return
        self._send(200, json.dumps(record, indent=2).encode(), "application/json")


def serve() -> None:
    ip = lan_ip()
    open_store()
    token = api_token()
    httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cert, key = ensure_cert(ip)
    tls = ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    tls.socket = context.wrap_socket(tls.socket, server_side=True)
    number = os.environ.get("OPERATOR_NUMBER", "")
    responder = os.environ.get("OPERATOR_RESPONDER_NUMBER", "")
    print("Operator", flush=True)
    print(f"  health          http://127.0.0.1:{HTTP_PORT}/health", flush=True)
    print(f"  incidents       http://127.0.0.1:{HTTP_PORT}/v1/incidents?token={token}", flush=True)
    if ip:
        print(f"  tls             https://{ip}:{HTTPS_PORT}", flush=True)
    if number:
        print(f"  call            {number}", flush=True)
    else:
        print("  caller          dial the Twilio number. Nothing else.", flush=True)
    if responder:
        print(f"  responder sms   {responder}", flush=True)
    else:
        print("  responder sms   set OPERATOR_RESPONDER_NUMBER to page a human on trapped calls", flush=True)
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        print(f"  twilio voice    {base}/voice", flush=True)
    else:
        print("  twilio          set PUBLIC_BASE_URL to the tunnel, then point Voice at /voice", flush=True)
    if not os.environ.get("TWILIO_AUTH_TOKEN"):
        print("  signature       off until TWILIO_AUTH_TOKEN is set", flush=True)
    if os.environ.get("TYPESAFE_API_KEY"):
        print("  harness         Jev", flush=True)
    elif os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        print("  harness         model backup until TYPESAFE_API_KEY is set", flush=True)
    else:
        print("  harness         rules until TYPESAFE_API_KEY is set", flush=True)
    print(f"  voice           {'gpt-live-1 / webrtc' if gptlive.configured() else 'twilio twiml (set OPENAI_API_KEY for gpt-live)'}", flush=True)
    print(f"  tools           {len(tools.SCHEMA)} exposed at /v1/tools", flush=True)
    spot = feeds.area()
    if spot:
        print(f"  area centre     {spot['lat']:.4f}, {spot['lon']:.4f}  ({spot['source']})", flush=True)
        feeds.prewarm(spot["lat"], spot["lon"])
    else:
        print("  area centre     unset — set OPERATOR_AREA_CITY", flush=True)
    area_city = os.environ.get("OPERATOR_AREA_CITY", "")
    area_region = os.environ.get("OPERATOR_AREA_REGION", "")
    if area_city or area_region:
        print(f"  service area    {', '.join(part for part in (area_city, area_region) if part)}", flush=True)
    tls.serve_forever()


if __name__ == "__main__":
    serve()
