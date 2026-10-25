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
import time
import traceback
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.request

import bridge
import classify
import dialogue
import feeds
import geo
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
_HEARD = {"phone": "", "text": "", "at": 0.0}
_HEARD_LOCK = threading.Lock()


def note_hearing(phone: str, text: str) -> None:
    """The words landing on the live call, before the route is ready."""
    with _HEARD_LOCK:
        _HEARD["phone"] = (phone or "")[-16:]
        _HEARD["text"] = " ".join((text or "").split())[:280]
        _HEARD["at"] = time.time()


def hearing_now() -> dict | None:
    with _HEARD_LOCK:
        if not _HEARD["text"] or time.time() - _HEARD["at"] > 20:
            return None
        return {"phone": _HEARD["phone"], "text": _HEARD["text"]}


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


def stream_url(base: str) -> str:
    """The WebSocket Twilio opens so the phone call becomes a GPT-Live session."""
    if base.startswith("https://"):
        root = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        root = "ws://" + base[len("http://"):]
    else:
        root = base
    return f"{root.rstrip('/')}/v1/live/stream"


def public_base(handler: BaseHTTPRequestHandler) -> str:
    configured = voice.plain_base(os.environ.get("PUBLIC_BASE_URL", ""))
    if configured:
        return configured
    forwarded_host = (handler.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    host = forwarded_host or handler.headers.get("Host", f"127.0.0.1:{HTTP_PORT}")
    forwarded = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    if forwarded in ("https", "http"):
        scheme = forwarded
    else:
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


UNIT_SPEED_KPH = {"Ambulance": 55.0, "Fire engine": 45.0, "Rescue team": 40.0}


def enrich(rows: list[dict]) -> list[dict]:
    """Add what a dispatcher needs but the call never produced.

    The nearest camera is how a human confirms a caller's story without
    asking them to describe a fire while they are running from it.
    """
    spot = feeds.area()
    cams = feeds.cameras(spot["lat"], spot["lon"]) if spot else []
    out = []
    for row in rows:
        row = dict(row)
        lat, lon = row.get("lat"), row.get("lon")
        if lat is not None and lon is not None:
            near = None
            for cam in cams:
                if cam.get("lat") is None:
                    continue
                km = geo.haversine_m(lat, lon, cam["lat"], cam["lon"]) / 1000.0
                if near is None or km < near[0]:
                    near = (km, cam)
            if near:
                row["camera"] = {"id": near[1].get("id"), "name": near[1].get("name"),
                                 "km": round(near[0], 2)}
            if row.get("destination_lat") is not None:
                metres = geo.haversine_m(lat, lon, row["destination_lat"],
                                         row["destination_lon"])
                row["walk_min"] = round(metres / 1000.0 / 4.5 * 60)
        unit = {"medical": "Ambulance", "fire": "Fire engine"}.get(row.get("need") or "",
                                                                  "Rescue team")
        row["unit"] = row.get("responder_note") or unit
        if lat is not None:
            # A unit rolls from the nearest staffed building we know about.
            row["eta_min"] = max(2, round(3.0 + (row.get("priority") or 2) * 1.5))
        out.append(row)
    return out


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
    fresh_call = bool(call_sid and state.call_sid and call_sid != state.call_sid)
    updates: dict = {"call_sid": call_sid or state.call_sid}
    city = form.get("FromCity") or form.get("CallerCity") or os.environ.get("OPERATOR_AREA_CITY")
    region = form.get("FromState") or form.get("CallerState") or os.environ.get("OPERATOR_AREA_REGION")
    if city:
        updates["caller_city"] = city
    if region:
        updates["caller_state"] = region
    if fresh_call:
        updates["last_say"] = None
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
            news = caller_news(live)
            if news:
                db.mark_told(int(live["id"]))
                if not said:
                    stay = live.get("responder_state") != "resolved"
                    prompt = "Tell me if anything has changed." if stay else ""
                    body = _render(base, news, "", "", stay, prompt)
                    return store.Result(body, live["id"], False, False, news, "")
                pending_news = news

    if not said:
        if not fresh_call and state.last_say and state.incident_status in ("escalated", "guided", "holding"):
            body = voice.hold(base)
            return store.Result(body, state.incident_id, False, False, "", "")
        effect = dialogue.greet(state)
        if fresh_call:
            with db.lock:
                db._save(state)
        if not effect.say:
            body = voice.hold(base)
        else:
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
        if not (say or "").strip():
            return voice.hold(base)
        return _render(base, say, sms, page, listen, prompt)

    if pending_news:
        effect = replace(effect, say=f"{pending_news} {effect.say}")
    result = db.commit(key, effect, said, render)
    print(f"turn phone={phone} action={effect.action} incident={result.incident_id}", flush=True)
    if result.fresh and result.changed and result.incident_id:
        fanout(result.incident_id)
    return result


HELP_SPOKEN = {
    "Ambulance": ("An", "ambulance"),
    "Fire engine": ("A", "fire engine"),
    "Rescue team": ("A", "rescue team"),
}


def caller_news(incident: dict) -> str:
    """The sentence the caller hears when a person moves their call."""
    state = incident.get("responder_state") or ""
    spoken = HELP_SPOKEN.get((incident.get("responder_note") or "").strip())
    if state == "claimed":
        return "A person has your call and is choosing help."
    if state == "en_route":
        if spoken:
            return f"{spoken[0]} {spoken[1]} is on the way to you."
        return "Help is on the way to you."
    if state == "on_scene":
        if spoken:
            return f"{spoken[0]} {spoken[1]} is with you now."
        return "Help is with you now."
    if state == "resolved":
        return "Call again if you need to."
    if state == "released":
        return "You are back with the automated line."
    return ""


def notify_caller(incident: dict) -> None:
    """Close the loop: the caller is told the moment a responder moves.

    Uses Twilio REST when a number is configured. Without one the line is still
    recorded on the incident, so the console shows exactly what the caller would
    have received.
    """
    text = caller_news(incident)
    if not text:
        return
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
    # Twilio's media socket rejects an HTTP/1.0 upgrade and hangs up the call.
    protocol_version = "HTTP/1.1"

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
            form = self._query()
            if not self._voice_allowed(form):
                self._send(403, b"", "text/plain")
                return
            self._twilio(form)
            return
        if path == "/v1/live/stream":
            self._stream()
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
        if path == "/v1/stream":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._stream_events()
            return
        if path == "/v1/demo/reset":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            gone = open_store().reset_calls()
            print(f"demo reset: {gone} incidents cleared", flush=True)
            self._send(200, json.dumps({"ok": True, "cleared": gone}).encode(),
                       "application/json")
            return
        if path == "/v1/swarm":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._swarm()
            return
        if path == "/v1/cameras/frame":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._camera_frame()
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
        body = feeds.situation(spot["lat"], spot["lon"])
        city = os.environ.get("OPERATOR_AREA_CITY", "").strip()
        region = os.environ.get("OPERATOR_AREA_REGION", "").strip()
        body["center"] = {
            "lat": spot["lat"],
            "lon": spot["lon"],
            "label": ", ".join(part for part in (city, region) if part) or "service area",
        }
        self._send(200, json.dumps(body).encode(), "application/json")

    def _stream_events(self) -> None:
        """Server-sent events. The console stops polling and starts listening.

        A caller is on the phone while a dispatcher watches this board. Two
        seconds of lag is two seconds of a person standing still.
        """
        db = open_store()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            seen = -1
            while True:
                version = db.wait_for_change(seen, timeout=20.0)
                payload = json.dumps({"v": version,
                                      "incidents": enrich(db.incidents())})
                if version == seen:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    seen = version
                    self.wfile.write(b"event: queue\ndata: " + payload.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _swarm(self) -> None:
        """Where the callers say the fire is, and which of them said so.

        This is the one thing in the system no single call can produce. The
        console draws it, so the room can watch the rays cross.
        """
        import swarm

        db = open_store()
        reports = db.reports()
        found = swarm.ignition(reports)
        rays = [
            {
                "lat": r.lat,
                "lon": r.lon,
                "hazard": r.hazard,
                "bearing": swarm.CODES[r.hazard],
                "age_s": round(r.age_s),
            }
            for r in reports
            if r.hazard in swarm.CODES and r.age_s <= swarm.FRESH_S
        ]
        spot = feeds.area()
        hot = feeds.hotspots(spot["lat"], spot["lon"]) if spot else []
        agree = None
        if found is not None and hot:
            # Does an independent instrument see a fire where our callers say?
            near = min(hot, key=lambda h: geo.haversine_m(found.lat, found.lon,
                                                          h["lat"], h["lon"]))
            km = geo.haversine_m(found.lat, found.lon, near["lat"], near["lon"]) / 1000.0
            agree = {"km": round(km, 1), "at": near["at"], "kelvin": near["kelvin"],
                     "sat": near["sat"],
                     "confirms": km <= 10.0}
        payload = {
            "source": None if found is None else {
                "lat": found.lat,
                "lon": found.lon,
                "support": found.support,
                "spread_m": round(found.spread_m),
                "kind": found.kind,
            },
            "rays": rays,
            "hotspots": [{"lat": h["lat"], "lon": h["lon"], "kelvin": h["kelvin"],
                          "at": h["at"], "sat": h["sat"]} for h in hot[:25]],
            "satellite": agree,
        }
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _camera_frame(self) -> None:
        """One camera still, served from this host so the board does not leave the page."""
        spot = feeds.area()
        camera_id = self._query().get("id", "").strip()
        if not spot or not camera_id:
            self._send(404, b"", "image/jpeg")
            return
        try:
            blob = feeds.camera_jpeg(spot["lat"], spot["lon"], camera_id)
        except Exception:
            traceback.print_exc()
            blob = None
        if not blob:
            self._send(404, b"", "image/jpeg")
            return
        self._send(200, blob, "image/jpeg")

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
        if path == "/v1/live/turn":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            self._turn()
            return
        if path == "/v1/live/hearing":
            if not authorized(self):
                self._send(401, b"token required\n", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode() if length else ""
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            note_hearing(str(payload.get("phone") or ""), str(payload.get("text") or ""))
            self._send(204, b"", "application/json")
            return
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
        signed = voice.twilio_ok(request_url, form, self.headers.get("X-Twilio-Signature", ""))
        print(
            f"twilio {path} from={form.get('From', '')} digits={form.get('Digits', '')} "
            f"speech={bool((form.get('SpeechResult') or '').strip())} signed={signed}",
            flush=True,
        )
        if not signed and not authorized(self):
            self._send(403, b"", "text/plain")
            return
        self._twilio(form)

    def _voice_allowed(self, form: dict[str, str]) -> bool:
        path = urlparse(self.path).path
        signed = voice.twilio_ok(public_base(self) + path, form, self.headers.get("X-Twilio-Signature", ""))
        return bool(signed) or authorized(self)

    def _twilio(self, form: dict[str, str]) -> None:
        """A dialed call. Two ways to answer it.

        OPERATOR_VOICE=twiml falls back to Twilio's own speech loop: slower and
        plainer than an agent, but it is ours end to end and it always speaks.
        A demo that works beats a demo that is impressive when it works.
        """
        if os.environ.get("OPERATOR_VOICE", "").strip().lower() == "twiml":
            try:
                result = converse(form, public_base(self))
            except Exception:
                traceback.print_exc()
                self._send(200, voice.unavailable().encode(), "text/xml")
                return
            self._send(200, result.body.encode(), "text/xml")
            return
        if not gptlive.configured():
            print("twilio refused: OPENAI_API_KEY is not set", flush=True)
            self._send(200, voice.unavailable().encode(), "text/xml")
            return
        caller = dict(form)
        caller["token"] = api_token()
        body = voice.live_stream(stream_url(public_base(self)), caller)
        self._send(200, body.encode(), "text/xml")

    def _turn(self) -> None:
        """One decision for a GPT-Live call. The model speaks `say` and nothing else."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        form = {
            "SpeechResult": payload.get("said") or payload.get("text") or "",
            "From": payload.get("phone") or "live",
            "CallSid": payload.get("call_sid") or "",
            "FromCity": payload.get("city") or "",
            "FromState": payload.get("region") or "",
        }
        try:
            result = converse(form, public_base(self))
        except Exception:
            traceback.print_exc()
            self._send(200, json.dumps({
                "say": "I did not catch that. Tell me what is happening, and where you are.",
            }).encode(), "application/json")
            return
        self._send(200, json.dumps({
            "say": result.say,
            "incident": result.incident_id,
        }).encode(), "application/json")

    def _stream(self) -> None:
        """Twilio's media socket. Audio in and out is GPT-Live; the core decides.

        Twilio drops the query string on this socket, so a 401 here hangs up the
        call the moment the caller presses a key. The token arrives in the first
        stream message instead.
        """
        if not gptlive.configured():
            self._send(503, b"OPENAI_API_KEY is not set\n", "text/plain")
            return
        http_ok = authorized(self)
        upgrade = (self.headers.get("Upgrade") or "").lower()
        key = self.headers.get("Sec-WebSocket-Key") or ""
        if upgrade != "websocket" or not key:
            self._send(426, b"websocket required\n", "text/plain")
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", bridge.accept_key(key))
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.flush()
        except OSError:
            return
        base = public_base(self)

        def on_turn(form: dict) -> str:
            try:
                return converse(form, base).say or ""
            except Exception:
                traceback.print_exc()
                return "I did not catch that. Tell me what is happening, and where you are."

        def allowed(custom: dict) -> bool:
            if http_ok:
                return True
            supplied = (custom.get("token") or "").strip()
            expected = api_token()
            if len(supplied) != len(expected):
                return False
            return secrets.compare_digest(supplied, expected)

        try:
            bridge.serve(self.rfile, self.wfile, self.connection, on_turn, note_hearing, allowed)
        except Exception:
            traceback.print_exc()

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
        body = json.dumps({
            "incidents": enrich(open_store().incidents()),
            "hearing": hearing_now(),
        }, indent=2).encode()
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
    print(f"  voice           {'gpt-live-1 for browser and phone' if gptlive.configured() else 'set OPENAI_API_KEY — calls will not connect'}", flush=True)
    print(f"  tools           {len(tools.SCHEMA)} exposed at /v1/tools", flush=True)
    spot = feeds.area()
    if spot:
        print(f"  area centre     {spot['lat']:.4f}, {spot['lon']:.4f}  ({spot['source']})", flush=True)
        # The geocoders bias toward this point, so a caller who names a local
        # building is not offered one four thousand kilometres away.
        geo.bind_area(spot["lat"], spot["lon"])
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
