"""A phone call's audio, carried into GPT-Live.

Twilio opens a media stream. This process opens a GPT-Live WebSocket in G.711
μ-law and forwards the bytes both ways. The decision core still chooses every
sentence. GPT-Live only speaks it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import threading
import time
import traceback
from collections.abc import Callable

import tools

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LIVE_URL = "api.openai.com"
MAX_FRAME = 8_000_000


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(opcode: int, payload: bytes, *, mask: bool) -> bytes:
    first = 0x80 | (opcode & 0x0F)
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        head = bytes([first, mask_bit | length])
    elif length < 65536:
        head = bytes([first, mask_bit | 126]) + length.to_bytes(2, "big")
    else:
        head = bytes([first, mask_bit | 127]) + length.to_bytes(8, "big")
    if not mask:
        return head + payload
    key = os.urandom(4)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return head + key + masked


def take_frame(buf: bytes) -> tuple[int, bool, bytes, bytes] | None:
    """Pop one frame from a buffer. None means the buffer is short a frame."""
    if len(buf) < 2:
        return None
    first, second = buf[0], buf[1]
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = int.from_bytes(buf[2:4], "big")
        offset = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = int.from_bytes(buf[2:10], "big")
        offset = 10
    if length > MAX_FRAME:
        raise ValueError("frame too large")
    masked = bool(second & 0x80)
    end = offset + (4 if masked else 0) + length
    if len(buf) < end:
        return None
    data = buf[offset + (4 if masked else 0):end]
    if masked:
        key = buf[offset:offset + 4]
        data = bytes(byte ^ key[index % 4] for index, byte in enumerate(data))
    return first & 0x0F, bool(first & 0x80), data, buf[end:]


def phone_session() -> dict:
    """The same voice contract as the browser, in the codec a phone already uses."""
    session = tools.session_config("")
    session["audio"] = {
        "format": {"type": "audio/pcmu", "rate": 8000},
        "output": {"voice": "vesper"},
    }
    return session


def _echo(said: str, last: str) -> bool:
    def words(text: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())

    heard, prior = words(said), words(last)
    if not heard or not prior:
        return False
    if heard == prior or heard in prior or prior in heard:
        return True
    pieces = heard.split()
    known = set(prior.split())
    hits = sum(1 for word in pieces if word in known)
    return len(pieces) >= 5 and hits / len(pieces) >= 0.7


class _Reader:
    def __init__(self, read: Callable[[int], bytes], leftover: bytes = b"") -> None:
        self.read = read
        self.buf = leftover
        self.frag = b""
        self.frag_op = 0

    def messages(self):
        while True:
            got = None
            try:
                got = take_frame(self.buf)
            except ValueError:
                return
            if got is None:
                try:
                    chunk = self.read(65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.buf += chunk
                continue
            opcode, fin, data, self.buf = got
            if opcode == 0:
                self.frag += data
                if fin and self.frag_op:
                    yield self.frag_op, self.frag
                    self.frag = b""
                    self.frag_op = 0
                continue
            if opcode in (8, 9, 10):
                yield opcode, data
                continue
            if not fin:
                self.frag_op = opcode
                self.frag = data
                continue
            yield opcode, data


def _connect_live() -> tuple[ssl.SSLSocket, bytes]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise OSError("OPENAI_API_KEY is not set")
    raw = socket.create_connection((LIVE_URL, 443), timeout=20)
    live = ssl.create_default_context().wrap_socket(raw, server_hostname=LIVE_URL)
    nonce = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET /v1/live/sessions HTTP/1.1\r\n"
        f"Host: {LIVE_URL}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {nonce}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {key}\r\n"
        "User-Agent: ares/1.0\r\n"
        "\r\n"
    )
    live.sendall(request.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = live.recv(4096)
        if not chunk:
            raise OSError("GPT-Live closed during the handshake")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise OSError(head.decode("utf-8", "replace")[:300])
    live.settimeout(None)
    return live, rest


def serve(
    rfile,
    wfile,
    connection,
    on_turn: Callable[[dict], str],
    on_hearing: Callable[[str, str], None],
    allowed: Callable[[dict], bool],
) -> None:
    """Block until the phone call or the GPT-Live session ends.

    GPT-Live is opened only after the stream's first message checks out.
    A rejected socket must not be answered with HTTP 401: Twilio treats that
    as the end of the call.
    """
    try:
        connection.settimeout(20)
    except OSError:
        pass
    state = _Call(rfile, wfile, connection, on_turn, on_hearing, allowed)
    try:
        state.read_phone()
    finally:
        state.shutdown()


class _Call:
    def __init__(self, rfile, wfile, connection, on_turn, on_hearing, allowed) -> None:
        self.rfile = rfile
        self.wfile = wfile
        self.connection = connection
        self.live = None
        self.on_turn = on_turn
        self.on_hearing = on_hearing
        self.allowed = allowed
        self.twilio_lock = threading.Lock()
        self.live_lock = threading.Lock()
        self.buf_lock = threading.Lock()
        self.closed = False
        self.ready = False
        self.started = False
        self.stream_sid = ""
        self.phone = "phone"
        self.city = ""
        self.region = ""
        self.call_sid = ""
        self.caller = ""
        self.last_line = ""
        self.gen = 0
        self.delegated = False
        self.timer: threading.Timer | None = None

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.timer:
            self.timer.cancel()
        for sock in (self.live, self.connection):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def send_phone(self, payload: dict) -> None:
        frame = encode_frame(1, json.dumps(payload).encode(), mask=False)
        with self.twilio_lock:
            self.wfile.write(frame)
            self.wfile.flush()

    def send_live(self, payload: dict) -> None:
        if self.live is None:
            return
        frame = encode_frame(1, json.dumps(payload).encode(), mask=True)
        with self.live_lock:
            try:
                self.live.sendall(frame)
            except OSError:
                self.shutdown()

    def clear_audio(self) -> None:
        if not self.stream_sid:
            return
        try:
            self.send_phone({"event": "clear", "streamSid": self.stream_sid})
        except OSError:
            pass

    def speak(self, line: str) -> None:
        line = " ".join((line or "").split())
        if not line or line == self.last_line:
            return
        self.last_line = line
        self.clear_audio()
        self.send_live({
            "type": "session.instructions.append",
            "event_id": "say-" + str(time.time_ns()),
            "delegation_id": None,
            "content": (
                "Say exactly these words, once, then stop and listen. "
                "Do not add a name or any other words: " + line
            ),
        })

    def ask(self, said: str) -> None:
        said = " ".join((said or "").split())
        if said and _echo(said, self.last_line):
            return
        self.gen += 1
        generation = self.gen
        form = {
            "SpeechResult": said,
            "From": self.phone,
            "CallSid": self.call_sid,
            "FromCity": self.city,
            "FromState": self.region,
        }

        def work() -> None:
            try:
                say = self.on_turn(form) or ""
            except Exception:
                traceback.print_exc()
                say = "I did not catch that. Tell me what is happening, and where you are."
            if generation != self.gen or self.closed:
                return
            self.speak(say)

        threading.Thread(target=work, daemon=True).start()

    def schedule(self, delay: float) -> None:
        with self.buf_lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(delay, self.flush)
            self.timer.daemon = True
            self.timer.start()

    def flush(self) -> None:
        with self.buf_lock:
            said = " ".join(self.caller.split())
            self.caller = ""
            self.delegated = False
        if said:
            print(f"gpt-live heard from={self.phone} said={said!r}", flush=True)
            self.ask(said)

    def maybe_start(self) -> None:
        if self.started or not self.stream_sid:
            return
        self.started = True
        self.send_live({
            "type": "session.start",
            "event_id": "event_start",
            "session": phone_session(),
        })

    def on_live(self, event: dict) -> None:
        kind = event.get("type") or ""
        if kind == "session.started":
            self.ready = True
            session = (event.get("session") or {}).get("id") or ""
            print(f"gpt-live phone session={session} from={self.phone}", flush=True)
            self.ask("")
            return
        if kind == "session.output_audio.delta" and self.stream_sid:
            delta = event.get("delta") or ""
            if delta:
                self.send_phone({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": delta},
                })
            return
        if kind == "session.input_transcript.delta":
            delta = event.get("delta") or ""
            if not delta:
                return
            with self.buf_lock:
                self.caller += delta
                heard = self.caller
            try:
                self.on_hearing(self.phone, heard)
            except Exception:
                traceback.print_exc()
            # Words arrive in pieces. Wait for the model to hand the turn back,
            # and only fall back if the caller has been quiet for a few seconds.
            self.schedule(0.45 if self.delegated else 3.5)
            return
        if kind == "session.delegation.created":
            self.delegated = True
            self.schedule(0.45)
            return
        if kind == "error":
            print(f"gpt-live phone error: {event.get('error')}", flush=True)

    def on_phone(self, event: dict) -> None:
        kind = event.get("event") or ""
        if kind == "dtmf":
            digit = (event.get("dtmf") or {}).get("digit") or ""
            print(f"gpt-live phone digit={digit}", flush=True)
            return
        if kind == "start":
            if self.live is not None:
                return
            start = event.get("start") or {}
            self.stream_sid = start.get("streamSid") or event.get("streamSid") or ""
            custom = start.get("customParameters") or {}
            self.phone = custom.get("from") or self.phone
            self.city = custom.get("city") or ""
            self.region = custom.get("region") or ""
            self.call_sid = custom.get("call") or ""
            if not self.allowed(custom):
                print(f"gpt-live phone rejected from={self.phone}", flush=True)
                self.shutdown()
                return
            try:
                live, leftover = _connect_live()
            except OSError as exc:
                print(f"gpt-live phone failed: {exc}", flush=True)
                self.shutdown()
                return
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
            self.live = live
            print(f"gpt-live phone stream={self.stream_sid} from={self.phone}", flush=True)
            threading.Thread(target=self.read_live, args=(leftover,), daemon=True).start()
            self.maybe_start()
            return
        if kind == "media" and self.ready:
            payload = (event.get("media") or {}).get("payload") or ""
            if payload:
                self.send_live({"type": "session.input_audio.append", "audio": payload})
            return
        if kind == "stop":
            self.shutdown()

    def read_live(self, leftover: bytes) -> None:
        try:
            reader = _Reader(self.live.recv, leftover)
            for opcode, data in reader.messages():
                if self.closed:
                    return
                if opcode == 8:
                    return
                if opcode == 9:
                    try:
                        with self.live_lock:
                            self.live.sendall(encode_frame(10, data, mask=True))
                    except OSError:
                        return
                    continue
                if opcode != 1 or not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                self.on_live(event)
        except Exception:
            traceback.print_exc()
        finally:
            self.shutdown()

    def read_phone(self) -> None:
        try:
            # read() on a BufferedReader blocks until it has the whole 64 KB.
            # Twilio's first frames are a few hundred bytes, so the call would
            # sit silent until enough audio had piled up to fill the buffer.
            # read1() hands back whatever has arrived, which is what a socket needs.
            pump = getattr(self.rfile, "read1", None) or self.rfile.read
            reader = _Reader(pump)
            for opcode, data in reader.messages():
                if self.closed:
                    return
                if opcode == 8:
                    return
                if opcode == 9:
                    self.send_phone_raw(encode_frame(10, data, mask=False))
                    continue
                if opcode != 1 or not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                self.on_phone(event)
                if self.closed:
                    return
        except Exception:
            traceback.print_exc()
        finally:
            if not self.started and not self.closed:
                print("gpt-live phone closed before the session started", flush=True)

    def send_phone_raw(self, frame: bytes) -> None:
        with self.twilio_lock:
            self.wfile.write(frame)
            self.wfile.flush()
