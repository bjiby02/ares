"""Compact map pin carried as sound.

H|3288012|-11723451|4|3a
kind, latitude * 1e5, longitude * 1e5, sequence, checksum.
"""

from __future__ import annotations

import re

_PACKET = re.compile(r"([HS])\|(-?\d+)\|(-?\d+)\|(\d+)\|([0-9a-fA-F]{2})")


def checksum(body: str) -> str:
    return f"{sum(body.encode()) & 0xFF:02x}"


def build(kind: str, lat: float, lon: float, seq: int) -> str:
    if kind not in ("H", "S"):
        raise ValueError(f"unknown kind {kind}")
    body = f"{kind}|{int(round(lat * 100000))}|{int(round(lon * 100000))}|{seq & 255}"
    return f"{body}|{checksum(body)}"


def parse(packet: str) -> dict | None:
    raw = (packet or "").replace("\0", "").strip()
    match = _PACKET.fullmatch(raw)
    if not match:
        return None
    body = raw.rsplit("|", 1)[0]
    if checksum(body) != match.group(5).lower():
        return None
    return {
        "kind": match.group(1),
        "lat": int(match.group(2)) / 100000,
        "lon": int(match.group(3)) / 100000,
        "seq": int(match.group(4)),
    }
