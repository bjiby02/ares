"""Swarm inference. Callers corroborate each other, or they do not count."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import swarm
from swarm import Report

# A fire at 32.8900,-117.2367. Three callers ringed around it, each naming
# the direction they see it from where they stand.
NORTH = Report(32.8990, -117.2367, "S", kind="fire")
EAST = Report(32.8900, -117.2240, "W", kind="fire")
WEST = Report(32.8900, -117.2494, "E", kind="fire")
RING = [NORTH, EAST, WEST]


def test_rays_cross_on_the_fire():
    found = swarm.source(RING)
    assert found is not None
    assert abs(found.lat - 32.8900) < 0.003, found.lat
    assert abs(found.lon - -117.2367) < 0.003, found.lon
    assert found.support == 3
    assert found.spread_m < swarm.MAX_SPREAD_M


def test_caller_south_of_it_is_told_north():
    heard = swarm.infer(RING, 32.8798, -117.2367)
    assert heard is not None
    assert heard.hazard == "N", heard.hazard
    assert "smoke to the north" in heard.phrase
    assert "3 other callers" in heard.phrase


def test_one_report_is_not_a_cluster():
    assert swarm.infer([NORTH], 32.8798, -117.2367) is None


def test_contradictory_reports_do_not_fuse():
    noise = [Report(32.8990, -117.2367, "N"), Report(32.8900, -117.2240, "E"),
             Report(32.8900, -117.2494, "W")]
    assert swarm.source(noise) is None


def test_far_caller_does_not_inherit():
    assert swarm.infer(RING, 32.7076, -117.1570) is None  # Petco Park, ~20 km


def test_stale_reports_do_not_count():
    old = [Report(r.lat, r.lon, r.hazard, age_s=swarm.FRESH_S + 60) for r in RING]
    assert swarm.infer(old, 32.8798, -117.2367) is None


def test_water_changes_the_noun():
    flood = [Report(r.lat, r.lon, r.hazard, kind="shelter") for r in RING]
    heard = swarm.infer(flood, 32.8798, -117.2367)
    assert heard is not None and heard.kind == "shelter"
    assert "rising water" in heard.phrase


def test_single_report_still_speaks_in_the_singular():
    pair = [NORTH, EAST]
    heard = swarm.infer(pair, 32.8798, -117.2367)
    assert heard is not None and heard.support == 2
    assert heard.phrase.startswith("Another caller"), heard.phrase


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
