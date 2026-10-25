"""Cached place directory. Dialogue calls these; tests pass fakes instead."""

from __future__ import annotations

from dataclasses import asdict

import feeds
import geo

STORE = None
SEARCH_AGE = 12 * 3600
NEAR_AGE = 3600


def bind(store) -> None:
    global STORE
    STORE = store


def search(query: str) -> list[geo.Hit]:
    key = "q:" + " ".join(query.casefold().split())
    if STORE is not None:
        cached = STORE.cache_get(key, SEARCH_AGE)
        if cached is not None:
            return [geo.hit_from_dict(item) for item in cached]
    hits = geo.search(query)
    if STORE is not None and hits:
        STORE.cache_put(key, [geo.hit_to_dict(hit) for hit in hits])
    return hits


def route(lat: float, lon: float, hazard: str | None, need: str | None,
          origin_name: str | None = None) -> geo.Place | None:
    """Pick a destination. Live wind widens the forbidden arc downwind."""
    key = f"near:{lat:.3f}:{lon:.3f}"
    places = None
    if STORE is not None:
        cached = STORE.cache_get(key, NEAR_AGE)
        if cached is not None:
            places = [geo.Place(**item) for item in cached]
    if places is None:
        places = geo.destinations(lat, lon)
        if STORE is not None:
            STORE.cache_put(key, [asdict(place) for place in places])
    drift = None
    wind = feeds.wind(lat, lon)          # cached; never blocks on a cold network
    if wind:
        drift = float(wind["to_deg"])
    perimeters = feeds.fire_rings(lat, lon)   # cached the same way; last-good on failure
    return geo.choose((lat, lon), places, hazard, need, drift, origin_name, perimeters)
