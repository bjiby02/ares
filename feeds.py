"""Live situation feeds. Cached hard, because a demo must never wait on a network.

Every call returns immediately from cache when it can. A cold miss fetches with a
short timeout; a failure returns the last good value and marks it stale. Nothing
in here is allowed to raise into the call path.
"""
from __future__ import annotations

import json
import math
import threading
import time
import pathlib
import urllib.parse
import urllib.request

UA = "AresRescue/1.0 (disaster voice dispatch)"

WIND_TTL = 900.0        # NWS observations update roughly every 20 min
CAMERA_TTL = 21600.0    # camera positions are static; the images are not
FIRE_TTL = 1800.0
TIMEOUT = 6.0

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


def _get(url: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def cached(key: str, ttl: float, producer):
    """Return a cached value, refreshing on miss. Never raises."""
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit["at"] < ttl:
            return hit["value"]
    try:
        value = producer()
        with _LOCK:
            _CACHE[key] = {"at": now, "value": value, "stale": False}
        return value
    except Exception as exc:                      # noqa: BLE001 - feeds never break a call
        with _LOCK:
            hit = _CACHE.get(key)
            if hit:
                hit["stale"] = True
                return hit["value"]
        print(f"feed {key} unavailable: {exc}", flush=True)
        return None


def age(key: str) -> float | None:
    with _LOCK:
        hit = _CACHE.get(key)
        return None if not hit else time.time() - hit["at"]


# ---------------------------------------------------------------- wind
def wind(lat: float, lon: float) -> dict | None:
    """Bearing the wind comes FROM, and the bearing smoke therefore drifts TOWARD."""
    key = f"wind:{lat:.2f},{lon:.2f}"

    def fetch():
        points = _get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        stations_url = points["properties"]["observationStations"]
        stations = _get(stations_url)
        station = stations["features"][0]["id"]
        obs = _get(f"{station}/observations/latest")["properties"]
        deg = obs.get("windDirection", {}).get("value")
        spd = obs.get("windSpeed", {}).get("value")
        if deg is None:
            return None
        return {
            "from_deg": round(deg),
            "to_deg": round((deg + 180) % 360),
            "kph": round(spd, 1) if spd is not None else None,
            "station": station.rsplit("/", 1)[-1],
        }

    return cached(key, WIND_TTL, fetch)


# ---------------------------------------------------------------- cameras
def _webmerc_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x / 20037508.34 * 180.0
    lat = y / 20037508.34 * 180.0
    lat = 180.0 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2)
    return lon, lat


def cameras(lat: float, lon: float, radius_m: int = 40000) -> list[dict]:
    """ALERTCalifornia camera sites near a point. Operated by UC San Diego."""
    key = f"cams:{lat:.2f},{lon:.2f},{radius_m}"

    def fetch():
        base = ("https://services8.arcgis.com/X84q166Srnyl4JMV/ArcGIS/rest/services/"
                "ALERTCalifornia_Camera_Feed/FeatureServer/0/query")
        qs = urllib.parse.urlencode({
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
            "distance": radius_m, "units": "esriSRUnit_Meter",
            "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
            "returnGeometry": "true", "f": "json",
        })
        data = _get(f"{base}?{qs}", timeout=10)
        out = []
        for f in data.get("features", []):
            a = f.get("attributes", {}) or {}
            g = f.get("geometry", {}) or {}
            if g.get("x") is None:
                continue
            clon, clat = _webmerc_to_lonlat(g["x"], g["y"])
            out.append({
                "name": a.get("cameraName") or a.get("siteId") or "camera",
                "site": a.get("siteId"),
                "url": a.get("cameraURL"),
                "lat": round(clat, 5), "lon": round(clon, 5),
                "km": round(_haversine_km(lat, lon, clat, clon), 2),
            })
        out.sort(key=lambda c: c["km"])
        return out

    return cached(key, CAMERA_TTL, fetch) or []


def _haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- fires
def fires(lat: float, lon: float, radius_m: int = 120000) -> list[dict]:
    """Active NIFC incident perimeters near the area of responsibility."""
    key = f"fires:{lat:.1f},{lon:.1f}"

    def fetch():
        base = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
                "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")
        qs = urllib.parse.urlencode({
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
            "distance": radius_m, "units": "esriSRUnit_Meter",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "poly_IncidentName,poly_GISAcres",
            "returnGeometry": "false", "f": "json",
        })
        data = _get(f"{base}?{qs}", timeout=10)
        return [{"name": (f["attributes"].get("poly_IncidentName") or "unnamed").title(),
                 "acres": round(f["attributes"].get("poly_GISAcres") or 0)}
                for f in data.get("features", [])]

    return cached(key, FIRE_TTL, fetch) or []


# ---------------------------------------------------------------- bundle
def situation(lat: float, lon: float) -> dict:
    """Everything the console needs, in one cached call."""
    w = wind(lat, lon)
    cams = cameras(lat, lon)
    fs = fires(lat, lon)
    return {
        "wind": w,
        "wind_age_s": round(age(f"wind:{lat:.2f},{lon:.2f}") or 0),
        "cameras": cams[:12],
        "camera_count": len(cams),
        "fires": fs,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


AREA_TTL = 86400.0


def area() -> dict | None:
    """Centre of the configured service area, geocoded. No coordinates in code.

    OPERATOR_AREA_CITY / _REGION are the only inputs. OPERATOR_AREA_LAT / _LON
    override for an operator who wants an exact centre.
    """
    import os
    lat = os.environ.get("OPERATOR_AREA_LAT", "").strip()
    lon = os.environ.get("OPERATOR_AREA_LON", "").strip()
    if lat and lon:
        return {"lat": float(lat), "lon": float(lon), "source": "env"}
    city = os.environ.get("OPERATOR_AREA_CITY", "").strip()
    region = os.environ.get("OPERATOR_AREA_REGION", "").strip()
    if not city:
        return None
    query = ", ".join(x for x in (city, region) if x)

    def fetch():
        import geo
        hits = geo.search(query)
        local = [h for h in hits if not region or
                 region.casefold() in (h.state or "").casefold() or
                 (h.state or "").casefold() in region.casefold()]
        pick = (local or hits or [None])[0]
        if pick is None:
            return None
        return {"lat": pick.lat, "lon": pick.lon, "source": "geocoded:" + query}

    return cached("area:" + query, AREA_TTL, fetch)


def warm_places() -> list[str]:
    """Landmarks to pre-resolve. Operator supplied, never baked into the code."""
    import os
    raw = os.environ.get("OPERATOR_WARM_PLACES", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    path = os.environ.get("OPERATOR_WARM_FILE", "").strip()
    if path and os.path.isfile(path):
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data]
        except (OSError, json.JSONDecodeError):
            pass
    return []


def prewarm(lat: float, lon: float, places=None) -> None:
    """Fill every cache before anyone dials. Runs off the request path."""
    def go():
        t0 = time.time()
        situation(lat, lon)
        print(f"feeds prewarmed in {time.time() - t0:.1f}s", flush=True)
        try:
            import directory
            t1 = time.time()
            warmed = 0
            for q in (places if places is not None else warm_places()):
                try:
                    hits = directory.search(q)
                    if hits:
                        directory.route(hits[0].lat, hits[0].lon, None, None)
                        warmed += 1
                except Exception:
                    continue
            total = len(places if places is not None else warm_places())
            if total:
                print(f"geocache warmed {warmed}/{total} landmarks "
                      f"in {time.time() - t1:.1f}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"geocache prewarm skipped: {exc}", flush=True)
    threading.Thread(target=go, daemon=True).start()
