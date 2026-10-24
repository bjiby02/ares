"""Durable calls and incidents. One writer. Swap this for Postgres to add a second host."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dialogue import CallState, Effect, state_from, state_to

WINDOW_SECONDS = 6 * 3600


@dataclass
class Result:
    twiml: str
    incident_id: int | None
    fresh: bool
    changed: bool
    say: str
    sms: str


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(stamp: str) -> float:
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def _fill(text: str, incident_id: int | None) -> str:
    number = str(incident_id) if incident_id else ""
    filled = text.replace("{incident}", number)
    filled = filled.replace("Incident .", "")
    return " ".join(filled.split()) if "\n" not in filled else filled.replace("  ", " ").strip()


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=3000")
        self._schema()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              phone TEXT PRIMARY KEY,
              state_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incidents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT NOT NULL,
              call_sid TEXT,
              status TEXT NOT NULL,
              priority INTEGER NOT NULL,
              need TEXT,
              mobility TEXT,
              place_text TEXT,
              place_name TEXT,
              lat REAL,
              lon REAL,
              hazard TEXT,
              destination_name TEXT,
              destination_lat REAL,
              destination_lon REAL,
              instruction TEXT,
              summary TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_phone ON incidents(phone, updated_at);
            CREATE TABLE IF NOT EXISTS turns (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT,
              call_sid TEXT,
              said TEXT,
              action TEXT,
              incident_id INTEGER,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS responses (
              key TEXT PRIMARY KEY,
              body TEXT NOT NULL,
              say TEXT,
              sms TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS geocache (
              cache_key TEXT PRIMARY KEY,
              payload TEXT NOT NULL,
              fetched_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(incidents)")}
        if "hazard_inferred" not in columns:
            self.db.execute("ALTER TABLE incidents ADD COLUMN hazard_inferred INTEGER DEFAULT 0")
        # Responder side of the loop. A dispatcher claims an incident, moves it to
        # en_route, then resolves it. The caller is told on their next turn.
        for name, decl in (("responder", "TEXT"), ("responder_state", "TEXT"),
                           ("responder_note", "TEXT"), ("responder_at", "TEXT"),
                           ("caller_told", "INTEGER DEFAULT 0"),
                           ("supervised", "INTEGER DEFAULT 0")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE incidents ADD COLUMN {name} {decl}")

    def load(self, phone: str) -> CallState:
        with self.lock:
            row = self.db.execute("SELECT state_json FROM sessions WHERE phone = ?", (phone,)).fetchone()
            state = state_from(json.loads(row["state_json"]) if row else None, phone)
            return self._open_incident(state)

    def cached(self, key: str) -> Result | None:
        with self.lock:
            row = self.db.execute("SELECT body, say, sms FROM responses WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return Result(row["body"], None, False, False, row["say"] or "", row["sms"] or "")

    def cache_get(self, key: str, max_age: int) -> object | None:
        with self.lock:
            row = self.db.execute("SELECT payload, fetched_at FROM geocache WHERE cache_key = ?", (key,)).fetchone()
        if row is None or _age(row["fetched_at"]) > max_age:
            return None
        return json.loads(row["payload"])

    def cache_put(self, key: str, payload: object) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO geocache(cache_key, payload, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
                (key, json.dumps(payload), now()),
            )
            count = self.db.execute("SELECT COUNT(*) AS n FROM geocache").fetchone()["n"]
            if count > 5000:
                self.db.execute(
                    "DELETE FROM geocache WHERE cache_key IN (SELECT cache_key FROM geocache ORDER BY fetched_at ASC LIMIT 1000)"
                )

    def commit(self, key: str | None, effect: Effect, said: str, render) -> Result:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if key:
                    found = self.db.execute("SELECT body, say, sms FROM responses WHERE key = ?", (key,)).fetchone()
                    if found:
                        self.db.execute("COMMIT")
                        return Result(found["body"], None, False, False, found["say"] or "", found["sms"] or "")
                state = effect.state
                incident_id = state.incident_id
                changed = effect.incident is not None
                if effect.incident is not None:
                    incident_id = self._upsert(effect.incident, state.incident_id)
                    state = replace(state, incident_id=incident_id, incident_status=effect.incident["status"])
                say = _fill(effect.say, incident_id)
                sms = _fill(effect.sms, incident_id)
                page = _fill(effect.responder_sms, incident_id)
                body = render(say, sms, page, effect.listen, effect.prompt)
                self._save(state)
                self.db.execute(
                    "INSERT INTO turns(phone, call_sid, said, action, incident_id, created_at) VALUES(?,?,?,?,?,?)",
                    (state.phone, state.call_sid, said[:500], effect.action, incident_id, now()),
                )
                if key:
                    self.db.execute(
                        "INSERT INTO responses(key, body, say, sms, created_at) VALUES(?,?,?,?,?)",
                        (key, body, say, sms, now()),
                    )
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    self.db.execute("DELETE FROM responses WHERE created_at < ?", (cutoff,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return Result(body, incident_id, True, changed, say, sms)

    def incidents(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM incidents ORDER BY priority ASC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def incident(self, incident_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    RESPONDER_STATES = ("claimed", "en_route", "on_scene", "resolved", "released")

    def respond(self, incident_id: int, state: str, who: str = "",
                note: str = "", supervised: bool | None = None) -> dict | None:
        """A responder acts on an incident. Returns the updated row, or None."""
        if state not in self.RESPONDER_STATES:
            return None
        with self.lock:
            row = self.db.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                return None
            fields = ["responder_state = ?", "responder = ?", "responder_note = ?",
                      "responder_at = ?", "caller_told = 0", "updated_at = ?"]
            values = [state, who or "dispatch", note or "", now(), now()]
            if supervised is not None:
                fields.append("supervised = ?")
                values.append(1 if supervised else 0)
            if state == "resolved":
                fields.append("status = ?")
                values.append("closed")
            self.db.execute(
                f"UPDATE incidents SET {', '.join(fields)} WHERE id = ?",
                (*values, incident_id),
            )
            out = self.db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(out) if out else None

    def open_for(self, phone: str) -> dict | None:
        """The caller's live incident, so their next turn can carry responder news."""
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM incidents WHERE phone = ? AND status != 'closed' "
                "ORDER BY updated_at DESC LIMIT 1", (phone,)).fetchone()
        return dict(row) if row else None

    def mark_told(self, incident_id: int) -> None:
        with self.lock:
            self.db.execute("UPDATE incidents SET caller_told = 1 WHERE id = ?", (incident_id,))

    def reports(self, exclude_phone: str | None = None, limit: int = 40) -> list:
        """Recent sightings that named both a position and a hazard direction.

        These are the sensor readings behind swarm inference. The caller's own
        incidents are excluded, so nobody ever corroborates themselves.
        """
        import swarm

        with self.lock:
            rows = self.db.execute(
                "SELECT lat, lon, hazard, need, updated_at FROM incidents "
                "WHERE hazard IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL "
                "AND COALESCE(hazard_inferred, 0) = 0 "
                "AND (? IS NULL OR phone != ?) "
                "ORDER BY updated_at DESC LIMIT ?",
                (exclude_phone, exclude_phone, limit),
            ).fetchall()
        out = []
        for row in rows:
            try:
                age = _age(row["updated_at"])
            except (ValueError, TypeError):
                continue
            out.append(swarm.Report(lat=row["lat"], lon=row["lon"],
                                    hazard=row["hazard"], age_s=age,
                                    kind=row["need"]))
        return out

    def _open_incident(self, state: CallState) -> CallState:
        if not state.incident_id:
            return state
        row = self.db.execute(
            "SELECT status, updated_at FROM incidents WHERE id = ?",
            (state.incident_id,),
        ).fetchone()
        if row is None or row["status"] == "closed" or _age(row["updated_at"]) > WINDOW_SECONDS:
            return CallState(phone=state.phone, caller_city=state.caller_city, caller_state=state.caller_state)
        return replace(state, incident_status=row["status"])

    def _save(self, state: CallState) -> None:
        self.db.execute(
            "INSERT INTO sessions(phone, state_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(phone) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at",
            (state.phone, json.dumps(state_to(state)), now()),
        )

    def _upsert(self, incident: dict, existing: int | None) -> int:
        stamp = now()
        values = (
            incident.get("phone") or "",
            incident.get("call_sid") or "",
            incident["status"],
            incident["priority"],
            incident.get("need"),
            incident.get("mobility"),
            incident.get("place_text"),
            incident.get("place_name"),
            incident.get("lat"),
            incident.get("lon"),
            incident.get("hazard"),
            incident.get("destination_name"),
            incident.get("destination_lat"),
            incident.get("destination_lon"),
            incident.get("instruction"),
            incident.get("summary"),
            1 if incident.get("hazard_inferred") else 0,
        )
        if existing:
            self.db.execute(
                """
                UPDATE incidents
                SET phone=?, call_sid=?, status=?, priority=?, need=?, mobility=?, place_text=?, place_name=?,
                    lat=?, lon=?, hazard=?, destination_name=?, destination_lat=?, destination_lon=?,
                    instruction=?, summary=?, hazard_inferred=?, updated_at=?
                WHERE id=?
                """,
                (*values, stamp, existing),
            )
            return existing
        cursor = self.db.execute(
            """
            INSERT INTO incidents(
              phone, call_sid, status, priority, need, mobility, place_text, place_name,
              lat, lon, hazard, destination_name, destination_lat, destination_lon,
              instruction, summary, hazard_inferred, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*values, stamp, stamp),
        )
        return int(cursor.lastrowid)
