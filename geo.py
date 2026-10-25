"""Places, and the rule for which building a caller is allowed to walk toward."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

UA = "OperatorRescue/1.0 (disaster voice dispatch)"
# Water and other barriers a person on foot cannot cross. Loaded from
# OPERATOR_BARRIERS, so the geography is operator data and never code.
_BARRIERS: list | None = None


def barriers() -> list:
    """[( (lon,lat), (lon,lat) ), ...] — cached after the first read."""
    global _BARRIERS
    if _BARRIERS is not None:
        return _BARRIERS
    path = os.environ.get("OPERATOR_BARRIERS", "").strip()
    out: list = []
    if path and os.path.isfile(path):
        try:
            for item in json.loads(Path(path).read_text(encoding="utf-8")):
                a, b = item["from"], item["to"]
                out.append(((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"barriers file unusable: {exc}", flush=True)
            out = []
    _BARRIERS = out
    return out


# An assembly point is somewhere you can walk to. 30 km was a lookup radius,
# not a walking distance; a downtown caller was being offered a campus lawn.
REFUGE_RANGE_M = float(os.environ.get("OPERATOR_REFUGE_RANGE_M", "4000"))
DRIFT_ARC = 45.0      # degrees either side of the smoke track to avoid
COMPASS = {
    "N": 0,
    "NE": 45,
    "E": 90,
    "SE": 135,
    "S": 180,
    "SW": 225,
    "W": 270,
    "NW": 315,
}
WORDS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
STOP = {"the", "a", "an", "of", "and", "at", "in", "near", "on", "by"}
GENERIC_TOKENS = {
    "library",
    "center",
    "centre",
    "building",
    "hall",
    "hospital",
    "clinic",
    "school",
    "university",
    "park",
    "north",
    "south",
    "east",
    "west",
}
GENERIC_NAMES = {"hospital", "shelter", "clinic"}
BAD_NAME = ("committal", "cemetery", "mausoleum", "cremator")
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri",
    "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio",
    "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia",
}
_AREA: tuple[float, float] | None = None


def bind_area(lat: float, lon: float) -> None:
    """Tell the geocoders where we answer calls. Set once, at boot."""
    global _AREA
    _AREA = (float(lat), float(lon))


def area_centre() -> tuple[float, float] | None:
    if _AREA is not None:
        return _AREA
    try:
        return float(os.environ["OPERATOR_AREA_LAT"]), float(os.environ["OPERATOR_AREA_LON"])
    except (KeyError, ValueError):
        return None


# How far out of the service area a result may sit before it is "somewhere else".
LOCAL_M = 80_000.0


def _prefer_local(hits: list[Hit]) -> list[Hit]:
    """Put results near the service area first, without discarding the rest.

    A caller who says "Student Services Center" means the one they can walk to.
    The geocoders rank globally, so the local building can fall past the cut.
    Sorting before the cut is what keeps it in the list.
    """
    centre = area_centre()
    if centre is None:
        return hits
    clat, clon = centre

    def rank(hit: Hit) -> tuple[int, float]:
        far = haversine_m(clat, clon, hit.lat, hit.lon)
        return (0 if far <= LOCAL_M else 1, far)

    return sorted(hits, key=rank)


_NOMINATIM = threading.Lock()
_LAST_NOMINATIM = 0.0


class LookupError(Exception):
    """The place directory could not be reached."""


@dataclass(frozen=True)
class Place:
    name: str
    kind: str
    lat: float
    lon: float
    meters: float
    bearing: float


@dataclass(frozen=True)
class Hit:
    name: str
    label: str
    lat: float
    lon: float
    kind: str
    city: str = ""
    state: str = ""
    district: str = ""


def hit_to_dict(hit: Hit) -> dict:
    return {
        "name": hit.name,
        "label": hit.label,
        "lat": hit.lat,
        "lon": hit.lon,
        "kind": hit.kind,
        "city": hit.city,
        "state": hit.state,
        "district": hit.district,
    }


def hit_from_dict(raw: dict) -> Hit:
    return Hit(
        name=raw["name"],
        label=raw.get("label") or raw["name"],
        lat=float(raw["lat"]),
        lon=float(raw["lon"]),
        kind=raw.get("kind") or "poi",
        city=raw.get("city") or "",
        state=raw.get("state") or "",
        district=raw.get("district") or "",
    )


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    y = math.sin(delta) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def angle_diff(a: float, b: float) -> float:
    turn = abs(a - b) % 360
    return min(turn, 360 - turn)


def bearing_word(degrees: float) -> str:
    return WORDS[int((degrees + 22.5) // 45) % 8]


def distance_phrase(meters: float) -> str:
    if meters < 950:
        rounded = max(50, int(round(meters / 50) * 50))
        return f"about {rounded} meters"
    kilometers = meters / 1000
    text = f"{kilometers:.1f}".rstrip("0").rstrip(".")
    unit = "kilometer" if text == "1" else "kilometers"
    return f"about {text} {unit}"


def instruction(place: Place, hazard_kind: str | None = None) -> str:
    clause = ""
    if hazard_kind == "smoke":
        clause = "away from the smoke, "
    elif hazard_kind == "water":
        clause = "away from the water, "
    return f"Head {bearing_word(place.bearing)}, {clause}{distance_phrase(place.meters)}, toward {place.name}."


def _usable(place: Place) -> bool:
    name = place.name.strip()
    low = name.casefold()
    if not name or low in GENERIC_NAMES or low.isdigit():
        return False
    return not any(word in low for word in BAD_NAME)


def _crosses(ax: float, ay: float, bx: float, by: float,
             cx: float, cy: float, dx: float, dy: float) -> bool:
    """Do segments AB and CD intersect?"""
    def side(px, py, qx, qy, rx, ry):
        v = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        return (v > 1e-12) - (v < -1e-12)
    d1 = side(ax, ay, bx, by, cx, cy)
    d2 = side(ax, ay, bx, by, dx, dy)
    d3 = side(cx, cy, dx, dy, ax, ay)
    d4 = side(cx, cy, dx, dy, bx, by)
    return d1 != d2 and d3 != d4


def walkable(origin: tuple[float, float], place: Place) -> bool:
    """False when the straight line to this place crosses water on foot."""
    olat, olon = origin
    for (x1, y1), (x2, y2) in barriers():
        if _crosses(olon, olat, place.lon, place.lat, x1, y1, x2, y2):
            return False
    return True


def inside_ring(lat: float, lon: float, ring: list) -> bool:
    """Point in polygon, by ray casting. Ring points are [lon, lat]."""
    hit = False
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[(index + 1) % count][0], ring[(index + 1) % count][1]
        if (y1 > lat) != (y2 > lat):
            cut = (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-15) + x1
            if lon < cut:
                hit = not hit
    return hit


def in_fire(lat: float, lon: float, perimeters: list | None) -> dict | None:
    """The fire whose outline contains this point, when there is one."""
    for fire in perimeters or []:
        for ring in fire.get("rings") or []:
            if inside_ring(lat, lon, ring):
                return fire
    return None


def clear_of_fire(origin: tuple[float, float], place: Place,
                  perimeters: list | None) -> bool:
    """False when the destination sits in a mapped fire, or the walk enters one.

    The caller's own bearing is one person's word. A published perimeter is a
    measured outline. We refuse to cross it even when the caller never named it.
    """
    if not perimeters:
        return True
    olat, olon = origin
    if in_fire(place.lat, place.lon, perimeters):
        return False
    for fire in perimeters:
        for ring in fire.get("rings") or []:
            count = len(ring)
            for index in range(count):
                x1, y1 = ring[index][0], ring[index][1]
                x2, y2 = ring[(index + 1) % count][0], ring[(index + 1) % count][1]
                if _crosses(olon, olat, place.lon, place.lat, x1, y1, x2, y2):
                    return False
    return True


_PLACE_NOISE = {"the", "of", "at", "assembly", "area", "point", "centre", "center"}


def _same_place(origin_name: str, dest_name: str) -> bool:
    """True when the destination is really the caller's own location.

    The spoken origin carries a city suffix ("Warren Mall in San Diego"), so the
    comparison uses the part before " in ".
    """
    head = re.split(r"\s+\bin\b\s+", origin_name or "", maxsplit=1)[0]
    a = set(_tokens(head)) - _PLACE_NOISE
    b = set(_tokens(dest_name or "")) - _PLACE_NOISE
    if not a or not b:
        return False
    return a.issubset(b) or b.issubset(a)


def choose(origin: tuple[float, float], places: list[Place], hazard: str | None,
           need: str | None = None, drift: float | None = None,
           origin_name: str | None = None, perimeters: list | None = None) -> Place | None:
    """Nearest staffed building that is not toward the hazard.

    Kind H is a hospital or clinic. Kind R is a refuge supplied by the operator,
    used for fire when one is open. Mapped picnic shelters are not eligible.

    drift is the bearing the smoke is travelling, from live wind. Buildings that
    sit downwind are refused at a wider angle, because the hazard is moving that
    way. Upwind is unchanged: the fire is not going there.
    """
    pool = [place for place in places if place.kind in ("H", "R")]
    if not pool:
        return None
    dry = [place for place in pool if walkable(origin, place)]
    if dry:
        pool = dry
    # A mapped fire is not a preference. Nothing inside one is a destination,
    # and no walk may cross one, so this filter has no "if any survive" escape.
    if perimeters:
        pool = [place for place in pool if clear_of_fire(origin, place, perimeters)]
        if not pool:
            return None
    # A destination that shares the caller's own place name is where they already
    # stand. "From Warren Mall ... toward Warren Mall assembly area" is not a route.
    if origin_name:
        away = [place for place in pool if not _same_place(origin_name, place.name)]
        if away:
            pool = away
    danger = COMPASS.get(hazard or "")

    # Downwind is a separate question from the hazard cone. A building can sit
    # well off the fire and still be in the path the smoke is travelling.
    if drift is not None:
        upwind = [place for place in pool if angle_diff(place.bearing, drift) >= DRIFT_ARC]
        if upwind:
            pool = upwind

    if danger is not None:
        safe = [place for place in pool if angle_diff(place.bearing, danger) >= 90]
        if safe:
            pool = safe
        else:
            best = max(pool, key=lambda place: angle_diff(place.bearing, danger))
            if angle_diff(best.bearing, danger) < 40:
                return None
            pool = [best]
    named = [place for place in pool if _usable(place)]
    if named:
        pool = named
    if need == "medical":
        hospitals = [place for place in pool if place.kind == "H"]
        if hospitals:
            return min(hospitals, key=lambda place: place.meters)
    if need == "fire":
        refuges = [place for place in pool if place.kind == "R"]
        if refuges:
            return min(refuges, key=lambda place: place.meters)
    return min(pool, key=lambda place: place.meters)


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.casefold()) if token not in STOP and len(token) > 1]


def _score(query: str, hit: Hit) -> float:
    tokens = _tokens(query)
    if not tokens:
        return 0.0
    name = hit.name.casefold()
    overlap = sum(1 for token in tokens if token in name) / len(tokens)
    folded = " ".join(tokens)
    score = overlap
    if folded == " ".join(_tokens(hit.name)):
        score += 3
    elif folded and folded in name:
        score += 1
    if hit.kind == "poi":
        score += 0.25
    return score


def _distinctive(query: str) -> list[str]:
    return [token for token in _tokens(query) if token not in GENERIC_TOKENS and len(token) > 3]


def _shares(query: str, hit: Hit) -> bool:
    tokens = _distinctive(query)
    if not tokens:
        return False
    name = hit.name.casefold()
    return any(token in name for token in tokens)


def _canon(value: str) -> str:
    text = " ".join(value.casefold().split())
    return US_STATES.get(text, text)


def _abbrev(state: str) -> str:
    full = _canon(state)
    for short, name in US_STATES.items():
        if name == full and len(short) == 2:
            return short
    return ""


def _city_in_name(hit: Hit) -> bool:
    """\"Warren Mall Cinemas\" sits in a town called Warren. The city is not a signal."""
    return bool(hit.city and hit.city.casefold() in (hit.name or "").casefold())


def _mentioned(query: str, hit: Hit) -> bool:
    text = query.casefold()
    if hit.city and len(hit.city) > 3 and hit.city.casefold() in text and not _city_in_name(hit):
        return True
    if hit.state and len(_canon(hit.state)) > 3 and _canon(hit.state) in text:
        return True
    short = _abbrev(hit.state)
    return bool(short and re.search(rf"\b{short}\b", text))


def _caller_match(hit: Hit, city: str | None, region: str | None) -> bool:
    if city and hit.city and city.casefold() in hit.city.casefold():
        return True
    return bool(region and hit.state and _canon(region) == _canon(hit.state))


def _speech(hit: Hit, other: Hit | None) -> str:
    if other is not None:
        if hit.city and other.city and _canon(hit.city) != _canon(other.city):
            return f"{hit.name} in {hit.city}"
        if hit.district and hit.district.casefold() != (other.district or "").casefold():
            return f"{hit.name} in {hit.district}"
    if hit.city and hit.city.casefold() not in hit.name.casefold():
        return f"{hit.name} in {hit.city}"
    return hit.name


def narrow(query: str, hits: list[Hit], city: str | None = None, region: str | None = None) -> list[Hit]:
    """One hit to route on, or two hits the caller must choose between."""
    # Service area first. A campus path is kind "road"; preferring POIs before
    # locality threw away Warren Mall, San Diego in favour of a cinema in Warren, PA.
    pool = list(hits)
    # A caller in San Diego is not at a namesake in another state.
    if city or region:
        local = [hit for hit in pool if _caller_match(hit, city, region)]
        if not local:
            return []
        pool = local
    pois = [hit for hit in pool if hit.kind == "poi"]
    if pois:
        pool = pois
    pool = _dedup(query, pool)
    mentioned = [hit for hit in pool if _mentioned(query, hit)]
    if mentioned:
        pool = mentioned
    if not pool:
        return []
    ranked = sorted(enumerate(pool), key=lambda item: (_score(query, item[1]), -item[0]), reverse=True)
    ordered = [hit for _, hit in ranked]
    best = ordered[0]
    other = _alternative(query, best, ordered[1:])
    if other is None:
        return [replace(best, label=_speech(best, None))]
    return [replace(best, label=_speech(best, other)), replace(other, label=_speech(other, best))]


def _dedup(query: str, hits: list[Hit]) -> list[Hit]:
    ordered = sorted(hits, key=lambda hit: _score(query, hit), reverse=True)
    kept: list[Hit] = []
    for hit in ordered:
        if any(haversine_m(hit.lat, hit.lon, prev.lat, prev.lon) < 500 for prev in kept):
            continue
        kept.append(hit)
    return kept


def _same_name(query: str, hit: Hit) -> bool:
    """The caller said this place's whole name, with nothing left over."""
    return _tokens(query) == _tokens(hit.name)


def _alternative(query: str, best: Hit, rest: list[Hit]) -> Hit | None:
    local: list[Hit] = []
    distant: list[Hit] = []
    # Both guards below need to know that the best hit is one of ours. Without a
    # configured service area we cannot say that, so the old behaviour stands.
    centre = area_centre()
    best_ours = centre is not None and haversine_m(centre[0], centre[1], best.lat, best.lon) <= LOCAL_M
    # The caller named one building exactly. A longer name is a different
    # building, not a namesake: "Student Services Center" is not "Student
    # Services Center and Classrooms", and the question costs them a turn.
    exact = _same_name(query, best)
    for hit in rest:
        if hit.kind != "poi" or not _shares(query, hit):
            continue
        if best_ours and exact and not _same_name(query, hit):
            continue
        meters = haversine_m(best.lat, best.lon, hit.lat, hit.lon)
        if 1500 < meters <= 80000:
            local.append(hit)
        elif meters > 80000 and _score(query, best) - _score(query, hit) <= 1:
            distant.append(hit)
    if local:
        return max(local, key=lambda hit: _score(query, hit))
    # A namesake in another state is never the answer for a caller we already
    # placed inside our own area. Offering it is how you lose a minute.
    if distant and not best_ours:
        return max(distant, key=lambda hit: _score(query, hit))
    return None


def _get(url: str, timeout: float, pace: bool) -> dict | list:
    global _LAST_NOMINATIM
    if pace:
        with _NOMINATIM:
            wait = 1.05 - (time.monotonic() - _LAST_NOMINATIM)
            if wait > 0:
                time.sleep(wait)
            _LAST_NOMINATIM = time.monotonic()
            return _read(url, timeout)
    return _read(url, timeout)


def _read(url: str, timeout: float) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def search(query: str) -> list[Hit]:
    clean = " ".join(query.split())
    if not clean:
        return []
    errors: list[Exception] = []
    for fetcher in (_photon_search, _nominatim_search):
        try:
            hits = fetcher(clean)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(exc)
            continue
        if hits:
            return _prefer_local(hits)[:5]
    if errors:
        raise LookupError(str(errors[-1]))
    return []


def destinations(lat: float, lon: float) -> list[Place]:
    found = trusted_refuges(lat, lon)
    try:
        found.extend(_nominatim_nearby(lat, lon))
    except (OSError, ValueError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
        if found:
            return found
        raise LookupError(str(exc)) from exc
    return found


def trusted_refuges(lat: float, lon: float) -> list[Place]:
    """Open shelters published by the operator. Empty until a city plugs a file in."""
    path = os.environ.get("OPERATOR_REFUGES", "").strip()
    if not path or not os.path.isfile(path):
        return []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    places: list[Place] = []
    for item in payload if isinstance(payload, list) else []:
        try:
            point_lat = float(item["lat"])
            point_lon = float(item["lon"])
            name = str(item["name"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not name:
            continue
        meters = haversine_m(lat, lon, point_lat, point_lon)
        if meters > REFUGE_RANGE_M:
            continue
        places.append(Place(name, "R", point_lat, point_lon, meters, bearing_deg(lat, lon, point_lat, point_lon)))
    return places


def _photon_search(query: str) -> list[Hit]:
    params = {"q": query, "limit": 15}
    centre = area_centre()
    if centre is not None:
        params["lat"], params["lon"] = centre[0], centre[1]
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
    payload = _get(url, timeout=6, pace=False)
    features = payload.get("features", []) if isinstance(payload, dict) else []
    hits: list[Hit] = []
    for feature in features:
        hit = _photon_hit(feature)
        if hit is not None:
            hits.append(hit)
    return hits


def _photon_hit(feature: dict) -> Hit | None:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    name = (props.get("name") or "").strip()
    if not name or len(coords) < 2:
        return None
    kind = "road" if props.get("osm_key") == "highway" else "poi"
    city = props.get("city") or props.get("town") or ""
    state = props.get("state") or ""
    district = props.get("district") or props.get("locality") or ""
    return Hit(name, name, float(coords[1]), float(coords[0]), kind, city, state, district)


def _nominatim_search(query: str) -> list[Hit]:
    params = {"q": query, "format": "jsonv2", "limit": 15, "addressdetails": 1}
    centre = area_centre()
    if centre is not None:
        clat, clon = centre
        # Prefer this box, do not restrict to it: a caller may name a place outside.
        params["viewbox"] = f"{clon - 0.7},{clat + 0.6},{clon + 0.7},{clat - 0.6}"
        params["bounded"] = 0
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    payload = _get(url, timeout=8, pace=True)
    if not isinstance(payload, list):
        return []
    hits: list[Hit] = []
    for item in payload:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        address = item.get("address") or {}
        category = item.get("category") or ""
        kind = "road" if category == "highway" else "poi"
        city = address.get("city") or address.get("town") or address.get("village") or ""
        state = address.get("state") or ""
        district = address.get("suburb") or address.get("neighbourhood") or ""
        hits.append(Hit(name, name, float(item["lat"]), float(item["lon"]), kind, city, state, district))
    return hits


def _nominatim_nearby(lat: float, lon: float) -> list[Place]:
    places = _places_from(_bounded(lat, lon, "hospital"), lat, lon, {"hospital", "clinic"})
    if places:
        return places
    return _places_from(_bounded(lat, lon, "clinic"), lat, lon, {"clinic"})


def _places_from(items: list, lat: float, lon: float, types: set[str]) -> list[Place]:
    places: list[Place] = []
    seen: set[tuple[str, int, int]] = set()
    for item in items:
        if item.get("type") not in types:
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        point_lat = float(item["lat"])
        point_lon = float(item["lon"])
        key = (name.casefold(), int(point_lat * 1000), int(point_lon * 1000))
        if key in seen:
            continue
        seen.add(key)
        meters = haversine_m(lat, lon, point_lat, point_lon)
        if meters > 20000:
            continue
        places.append(Place(name, "H", point_lat, point_lon, meters, bearing_deg(lat, lon, point_lat, point_lon)))
    return places


def _bounded(lat: float, lon: float, query: str) -> list:
    dlat = 15000 / 111320
    dlon = 15000 / (111320 * max(0.2, math.cos(math.radians(lat))))
    viewbox = f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": 8, "viewbox": viewbox, "bounded": 1}
    )
    payload = _get(url, timeout=8, pace=True)
    return payload if isinstance(payload, list) else []
