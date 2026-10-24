import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geo
import packet


def test_packet_roundtrip():
    raw = packet.build("H", 32.88012, -117.23451, 4)
    parsed = packet.parse(raw)
    assert parsed["kind"] == "H"
    assert abs(parsed["lat"] - 32.88012) < 0.00002
    assert abs(parsed["lon"] + 117.23451) < 0.00002
    assert parsed["seq"] == 4
    assert packet.parse(raw[:-1] + ("0" if raw[-1] != "0" else "1")) is None
    assert packet.parse("nope") is None


def test_hazard_rejects_the_near_side():
    origin = (0, 0)
    north = geo.Place("North", "H", 0.01, 0, 1000, 0)
    south = geo.Place("South", "H", -0.01, 0, 500, 180)
    chosen = geo.choose(origin, [north, south], "S")
    assert chosen.name == "North"


def test_no_hazard_picks_the_nearest_hospital():
    origin = (0, 0)
    far = geo.Place("Far", "H", 0.02, 0, 2000, 0)
    near = geo.Place("Near", "H", 0.001, 0, 100, 0)
    assert geo.choose(origin, [far, near], None).name == "Near"


def test_a_picnic_shelter_is_not_a_destination():
    shelter = geo.Place("Solar Chill", "S", 0, 0, 100, 0)
    hospital = geo.Place("Scripps", "H", 0, 0, 4000, 90)
    assert geo.choose((0, 0), [shelter], None) is None
    assert geo.choose((0, 0), [shelter, hospital], None).name == "Scripps"


def test_fire_uses_a_trusted_refuge_and_medical_uses_a_hospital():
    hospital = geo.Place("Scripps", "H", 0, 0, 200, 90)
    refuge = geo.Place("Price Center", "R", 0, 0, 800, 90)
    assert geo.choose((0, 0), [hospital, refuge], "S", "fire").name == "Price Center"
    assert geo.choose((0, 0), [hospital, refuge], "S", "medical").name == "Scripps"


def test_refuses_a_route_into_the_hazard():
    into = geo.Place("Into", "H", 0, 0, 100, 180)
    assert geo.choose((0, 0), [into], "S") is None


def test_a_closer_hospital_toward_the_hazard_loses():
    close = geo.Place("Close", "H", 0, 0, 100, 106)
    farther = geo.Place("Farther", "H", 0, 0, 200, 73)
    assert geo.choose((0, 0), [close, farther], "S").name == "Farther"


def test_the_only_offset_hospital_is_still_a_route():
    close = geo.Place("Close", "H", 0, 0, 100, 106)
    assert geo.choose((0, 0), [close], "S").name == "Close"


def test_a_mediocre_angle_is_still_a_route():
    side = geo.Place("Side", "H", 0, 0, 100, 50)
    assert geo.choose((0, 0), [side], "N").name == "Side"


def test_words():
    assert geo.bearing_word(0) == "north"
    assert geo.bearing_word(90) == "east"
    assert "meters" in geo.distance_phrase(380)
    assert geo.distance_phrase(1500).startswith("about 1.5")


if __name__ == "__main__":
    test_packet_roundtrip()
    test_hazard_rejects_the_near_side()
    test_no_hazard_picks_the_nearest_hospital()
    test_a_picnic_shelter_is_not_a_destination()
    test_fire_uses_a_trusted_refuge_and_medical_uses_a_hospital()
    test_refuses_a_route_into_the_hazard()
    test_a_closer_hospital_toward_the_hazard_loses()
    test_the_only_offset_hospital_is_still_a_route()
    test_a_mediocre_angle_is_still_a_route()
    test_words()
    print("ok")
