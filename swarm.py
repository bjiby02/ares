"""Cross-call hazard inference. Callers are sensors for each other.

One caller who says "smoke to my south" tells you a direction. Three callers who
each say a direction tell you a place. Back-project every report along the
bearing it named, and the rays cross where the fire is.

Pure. No network, no database. The server hands in the reports; tests hand in
fakes, the same way dialogue takes classify, origins, and route.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import geo

# A report has to agree with the others or it is noise, not a sensor.
MIN_REPORTS = 2          # below this a "cluster" is one frightened person
PROJECT_M = 2000.0       # how far along their bearing we assume the hazard sits
MAX_SPREAD_M = 3000.0    # back-projections must land near each other
NEAR_M = 5000.0          # only reports this close to the caller count
FRESH_S = 1800.0         # and only reports from the last half hour

CODES = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0,
         "S": 180.0, "SW": 225.0, "W": 270.0, "NW": 315.0}


@dataclass(frozen=True)
class Report:
    """One earlier caller's hazard sighting.

    kind is that caller's need, which is what makes the sighting smoke or water.
    The hazard column only ever holds a bearing.
    """
    lat: float
    lon: float
    hazard: str
    age_s: float = 0.0
    kind: str | None = None


@dataclass(frozen=True)
class Source:
    """Where the rays crossed. No observer, so no direction yet."""
    lat: float
    lon: float
    support: int         # how many callers agreed
    spread_m: float      # how tightly their rays crossed
    kind: str = "fire"   # what the reporters were seeing: fire or shelter


@dataclass(frozen=True)
class Inference:
    """A Source seen from one caller, so it has a direction they can act on."""
    hazard: str          # compass code to feed the cone rule
    lat: float           # estimated source
    lon: float
    support: int
    spread_m: float
    distance_m: float    # from the caller to the estimate
    kind: str = "fire"   # drives both the spoken noun and the routing need

    @property
    def phrase(self) -> str:
        """What the caller hears before the instruction."""
        word = geo.bearing_word(CODES[self.hazard])
        noun = "rising water" if self.kind == "shelter" else "smoke"
        if self.support < 3:
            return f"Another caller nearby reports {noun} to the {word}."
        return f"{self.support} other callers nearby report {noun} to the {word}."


def project(lat: float, lon: float, bearing: float, meters: float) -> tuple[float, float]:
    """Move a point along a bearing. Flat earth is fine over a few kilometres."""
    rad = math.radians(bearing)
    north = meters * math.cos(rad)
    east = meters * math.sin(rad)
    dlat = north / 111_320.0
    dlon = east / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat + dlat, lon + dlon


def usable(reports: list[Report], lat: float, lon: float) -> list[Report]:
    """Keep only reports that are near enough, recent enough, and name a direction."""
    kept = []
    for r in reports:
        if r.hazard not in CODES:
            continue
        if r.age_s > FRESH_S:
            continue
        if geo.haversine_m(lat, lon, r.lat, r.lon) > NEAR_M:
            continue
        kept.append(r)
    return kept


def source(reports: list[Report], *, min_reports: int = MIN_REPORTS) -> Source | None:
    """Back-project every report along its bearing and see where the rays cross."""
    pool = [r for r in reports if r.hazard in CODES]
    if len(pool) < min_reports:
        return None

    points = [project(r.lat, r.lon, CODES[r.hazard], PROJECT_M) for r in pool]
    clat = sum(p[0] for p in points) / len(points)
    clon = sum(p[1] for p in points) / len(points)

    # If the rays do not cross near one spot, these are separate fires, or noise.
    spread = max(geo.haversine_m(clat, clon, p[0], p[1]) for p in points)
    if spread > MAX_SPREAD_M:
        return None
    water = sum(1 for r in pool if r.kind == "shelter")
    kind = "shelter" if water * 2 > len(pool) else "fire"
    return Source(lat=clat, lon=clon, support=len(pool), spread_m=spread, kind=kind)


def infer(reports: list[Report], lat: float, lon: float,
          *, min_reports: int = MIN_REPORTS) -> Inference | None:
    """Caller side: the hazard source from other callers, as a direction this one can use."""
    found = source(usable(reports, lat, lon), min_reports=min_reports)
    if found is None:
        return None
    distance = geo.haversine_m(lat, lon, found.lat, found.lon)
    if distance < 100.0:
        return None  # the estimate sits on the caller; no useful direction
    code = _code(geo.bearing_deg(lat, lon, found.lat, found.lon))
    return Inference(hazard=code, lat=found.lat, lon=found.lon,
                     support=found.support, spread_m=found.spread_m,
                     distance_m=distance, kind=found.kind)


def ignition(reports: list[Report], *, min_reports: int = 3) -> Source | None:
    """Responder side: a tight cluster of fresh reports is a fire nobody has named yet."""
    fresh = [r for r in reports if r.hazard in CODES and r.age_s <= FRESH_S]
    return source(fresh, min_reports=min_reports)


def _code(degrees: float) -> str:
    """Snap a bearing to the eight names the cone rule understands."""
    best, gap = "N", 999.0
    for name, deg in CODES.items():
        d = geo.angle_diff(degrees, deg)
        if d < gap:
            best, gap = name, d
    return best
