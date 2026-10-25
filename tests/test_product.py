"""Call behavior without Twilio and without the network."""

import base64
import hashlib
import hmac
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge
import classify
import dialogue
import geo
import jev
import store
import voice

HOSPITAL = geo.Place("UC San Diego Health, La Jolla Campus", "H", 32.87809, -117.22520, 1200, 90)

GEISEL = [
    geo.Hit("Geisel Library", "Geisel Library", 32.88114, -117.23758, "poi", "San Diego", "California", "Torrey Pines"),
    geo.Hit("Geisel Library", "Geisel Library", 32.88209, -117.23562, "road", "San Diego", "California", "Torrey Pines"),
    geo.Hit("Geisel Library", "Geisel Library", 42.98635, -71.50701, "poi", "Goffstown", "New Hampshire", "Pinardville"),
    geo.Hit("Helen Palmer Geisel Library", "Helen Palmer Geisel Library", 32.84560, -117.27782, "poi", "San Diego", "CA", "La Jolla"),
]
PRICE = [geo.Hit("Price Center", "Price Center", 32.87980, -117.23600, "poi", "San Diego", "California", "University City")]


def origins(query):
    text = query.casefold()
    if "price" in text:
        return PRICE
    if "geisel" in text:
        return GEISEL
    return []


def route(lat, lon, hazard, need, origin_name=None):
    return HOSPITAL


def ask(state, said, finder=origins, router=route):
    return dialogue.step(state, said, classify=classify.fallback, origins=finder, route=router)


def test_jev_refuses_to_invent_a_place_and_gates_on_confidence():
    spans = ["Geisel Library"]
    sure = {
        "trapped": {"type": "noul", "noul": 0.05},
        "safe": {"type": "noul", "noul": 0.02},
        "repeat": {"type": "noul", "noul": 0.01},
        "reject": {"type": "noul", "noul": 0.04},
        "medical": {"type": "noul", "noul": 0.1},
        "fire": {"type": "noul", "noul": 0.96},
        "water": {"type": "noul", "noul": 0.02},
        "hazard": {"type": "choice", "choice": "S", "confidence": 0.9},
        "place": {"type": "choice", "choice": "Geisel Library", "confidence": 0.88},
    }
    guided = jev.decide(spans, sure)
    assert guided["action"] == "guide"
    assert guided["place"] == "Geisel Library"
    assert guided["need"] == "fire"
    assert guided["hazard"] == "S"
    unsure = dict(sure)
    unsure["place"] = {"type": "choice", "choice": "Geisel Library", "confidence": 0.4}
    assert jev.decide(spans, unsure)["action"] == "ask"
    invented = dict(sure)
    invented["place"] = {"type": "choice", "choice": "Helen Palmer Geisel Library", "confidence": 0.99}
    assert jev.decide(spans, invented)["place"] is None
    trapped = dict(sure)
    trapped["trapped"] = {"type": "noul", "noul": 0.91}
    held = jev.decide(spans, trapped)
    assert held["action"] == "escalate"
    assert held["mobility"] == "trapped"
    vague_hazard = dict(sure)
    vague_hazard["hazard"] = {"type": "choice", "choice": "W", "confidence": 0.2}
    assert jev.decide(spans, vague_hazard)["hazard"] is None
    assert jev.decide(spans, {**sure, "safe": {"type": "noul", "noul": 0.9}, "fire": {"type": "noul", "noul": 0.1}})["action"] == "close"
    asked = jev.questions(spans)
    assert "Helen Palmer" not in asked["place"]["criteria"]
    assert "Geisel Library" in asked["place"]["criteria"]
    assert classify.place_candidates("I am at Geisel Library and there is smoke to the south") == ["Geisel Library"]
    assert classify._ground("I am at Geisel Library", {"action": "guide", "place": "Helen Palmer Geisel Library"})["place"] is None
    assert classify._ground("We're beside SeaWorld", {"action": "guide", "place": "beside SeaWorld"})["place"] == "SeaWorld"


def test_classifier_keeps_a_building_out_of_the_hazard():
    heard = classify.fallback("I am at Geisel Library and there is smoke to the south")
    assert heard["action"] == "guide"
    assert heard["need"] == "fire"
    assert heard["hazard"] == "S"
    assert heard["place"] == "Geisel Library"
    hall = classify.fallback("I am at South Hall")
    assert hall["hazard"] is None
    assert hall["place"] == "South Hall"
    trapped = classify.fallback("I am in the house and I cannot get out")
    assert trapped["action"] == "escalate"
    assert trapped["place"] is None
    hurt = classify.fallback("I am hurt at Price Center")
    assert hurt["action"] == "guide"
    assert hurt["need"] == "medical"
    assert hurt["place"] == "Price Center"
    breath = classify.fallback("I cannot breathe and I am at Price Center")
    assert breath["action"] == "escalate"
    assert breath["need"] == "medical"
    flame = classify.fallback("the fire is inside and I am at Birch Aquarium")
    assert flame["action"] == "escalate"
    assert flame["need"] == "fire"
    assert flame["place"] == "Birch Aquarium"


def test_a_local_namesake_is_the_question_not_a_library_across_the_country():
    chosen = geo.narrow("Geisel Library", GEISEL)
    assert len(chosen) == 2
    labels = " ".join(hit.label for hit in chosen)
    assert "Goffstown" not in labels
    assert "La Jolla" in labels
    assert "Torrey Pines" in labels
    biased = geo.narrow("Geisel Library", GEISEL[:3], None, "CA")
    assert len(biased) == 1
    assert "San Diego" in biased[0].label
    named = geo.narrow("Geisel Library, La Jolla", GEISEL)
    assert len(named) == 2


def test_the_call_asks_before_it_sends_anyone_walking():
    routed = {"n": 0}

    def counting(*args):
        routed["n"] += 1
        return HOSPITAL

    first = ask(dialogue.CallState(phone="+15551212"), "I am at Geisel Library and there is smoke to the south", router=counting)
    assert first.action == "ask"
    assert first.say.startswith("I found ")
    assert "La Jolla" in first.say
    assert routed["n"] == 0
    assert first.incident is None
    missed = ask(first.state, "San Diego", router=counting)
    assert missed.action == "ask"
    assert routed["n"] == 0
    second = ask(first.state, "La Jolla", router=counting)
    assert second.action == "guide"
    assert routed["n"] == 1
    assert "Helen Palmer" in second.say
    assert "Head east, away from the smoke, about 1.2 kilometers, toward UC San Diego Health, La Jolla Campus." in second.say
    assert "fire engine is on the way" in second.say.lower()
    assert second.incident["auto_help"] == "Fire engine"
    # Voice only: the instruction is spoken, never texted, and no map is sent.
    assert second.sms == ""
    assert "texting" not in second.say
    assert "maps.google.com" not in second.say
    assert "smoke to the south" in second.incident["summary"]
    assert second.responder_sms == ""
    assert "{incident}" not in (second.state.last_say or "")
    rejected = ask(second.state, "no that's the wrong library", router=counting)
    assert rejected.say == "Where are you?"
    assert rejected.state.origin_lat is None
    assert rejected.state.hazard == "S"


def test_a_trapped_caller_is_not_given_a_map():
    def explode(*args):
        raise AssertionError("no walking route")

    effect = ask(dialogue.CallState(phone="+15551212"), "I am trapped inside Geisel Library", router=explode)
    assert effect.action == "escalate"
    assert effect.listen is True
    # Voice only: nothing is texted, and no map exists to follow.
    assert effect.sms == ""
    assert "map" not in effect.say.lower()
    assert "Stay low" in effect.say
    assert "rescue team is on the way" in effect.say.lower()
    assert effect.incident["auto_help"] == "Rescue team"
    assert effect.responder_sms.startswith("OPERATOR P0 incident")
    assert effect.state.origin_name == "Geisel Library"
    later = ask(effect.state, "I am inside the lobby", router=explode)
    assert later.action == "escalate"
    assert "update" in later.responder_sms
    assert "on the way" not in later.say
    breath = ask(
        dialogue.CallState(
            phone="m", place="Price Center", origin_label="Price Center in San Diego",
            origin_lat=32.88, origin_lon=-117.24,
        ),
        "I cannot breathe",
        router=explode,
    )
    assert "Stay still" in breath.say
    assert "ambulance is on the way" in breath.say.lower()
    assert breath.incident["auto_help"] == "Ambulance"
    lost = ask(dialogue.CallState(phone="z"), "I cannot get out", router=explode)
    assert "What building are you in?" in lost.say
    assert "on the way" not in lost.say
    assert "auto_help" not in lost.incident


def test_outage_and_unsafe_routes_do_not_invent_a_building():
    def down(_query):
        raise geo.LookupError("down")

    outage = ask(dialogue.CallState(phone="a"), "I am at Price Center and there is smoke to the south", finder=down)
    assert "cannot look up" in outage.say
    assert outage.incident["status"] == "holding"
    assert outage.sms == ""

    def none(*args):
        return None

    blocked = ask(dialogue.CallState(phone="b"), "I am at Price Center and there is smoke to the south", router=none)
    assert "will not send you toward the danger" in blocked.say
    assert "fire engine is on the way" in blocked.say.lower()
    assert blocked.sms == ""


def test_hurt_callers_get_a_hospital_and_can_close():
    effect = ask(dialogue.CallState(phone="+15550001"), "I am hurt at Price Center")
    assert effect.action == "guide"
    assert effect.incident["priority"] == 1
    assert effect.incident["need"] == "medical"
    assert "away from the smoke" not in effect.say
    assert "ambulance is on the way" in effect.say.lower()
    assert effect.incident["auto_help"] == "Ambulance"
    assert effect.responder_sms == ""
    effect.state.incident_id = 7
    effect.state.incident_status = "guided"
    closed = ask(effect.state, "I am safe now")
    assert closed.action == "close"
    assert closed.listen is False
    assert closed.say == "Call again if you need to."
    assert closed.responder_sms == ""


def test_repeat_and_twiml():
    state = dialogue.CallState(phone="c", last_say="From Price Center. Head east.")
    assert ask(state, "say that again").say == "From Price Center. Head east."
    body = voice.twiml(
        "https://example.com",
        "A & B",
        sms="go\nhttps://maps.google.com/maps?daddr=1,2&dirflg=w",
        responder_sms="P0",
        responder_to="+15551212",
        listen=False,
    )
    assert "<Gather" not in body
    # Voice only: no <Message> is ever emitted, to the caller or a responder.
    assert "<Message" not in body
    assert 'to="+15551212"' not in body
    assert "maps.google.com" not in body
    assert "&amp;" in body
    assert "<Hangup/>" in body
    asking = voice.twiml("https://example.com", "Where are you?")
    assert "<Gather" in asking
    assert "<Message" not in asking


def test_a_phone_call_opens_gpt_live():
    body = voice.live_stream(
        "wss://example.com/v1/live/stream",
        {"token": "abc", "From": "+16195550000", "FromCity": "San Diego", "CallSid": "CA1"},
    )
    assert "<Connect>" in body
    assert "<Stream " in body
    assert 'url="wss://example.com/v1/live/stream"' in body
    assert 'name="token" value="abc"' in body
    assert 'name="from" value="+16195550000"' in body
    assert "stream?token" not in body
    assert "<Gather" not in body
    assert "<Say" not in body
    escaped = voice.live_stream("wss://example.com/v1/live/stream", {"FromCity": "A & B", "token": "a&b"})
    assert "A &amp; B" in escaped
    assert 'value="a&amp;b"' in escaped
    assert "<Gather" not in voice.unavailable()
    frame = bridge.encode_frame(1, b"hello", mask=True)
    opcode, fin, payload, rest = bridge.take_frame(frame)
    assert opcode == 1 and fin and payload == b"hello" and rest == b""
    assert bridge.take_frame(frame[:3]) is None


def test_signature():
    previous_token = os.environ.get("TWILIO_AUTH_TOKEN")
    previous_base = os.environ.get("PUBLIC_BASE_URL")
    os.environ["TWILIO_AUTH_TOKEN"] = "secret"
    os.environ["PUBLIC_BASE_URL"] = "https://example.com"
    try:
        params = {"CallSid": "CA1", "SpeechResult": "hi"}
        signed = "https://example.com/gather" + "CallSidCA1SpeechResulthi"
        digest = base64.b64encode(hmac.new(b"secret", signed.encode(), hashlib.sha1).digest()).decode()
        assert voice.twilio_ok("http://127.0.0.1:8810/gather", params, digest)
        assert not voice.twilio_ok("http://127.0.0.1:8810/gather", params, "nope")
    finally:
        if previous_token is None:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)
        else:
            os.environ["TWILIO_AUTH_TOKEN"] = previous_token
        if previous_base is None:
            os.environ.pop("PUBLIC_BASE_URL", None)
        else:
            os.environ["PUBLIC_BASE_URL"] = previous_base


def test_incidents_survive_and_retries_do_not_duplicate():
    with tempfile.TemporaryDirectory() as folder:
        db = store.Store(str(Path(folder) / "operator.db"))
        effect = ask(dialogue.CallState(phone="+15551212"), "I am trapped inside Geisel Library")

        def render(say, sms, page, listen, prompt):
            return say

        first = db.commit("same", effect, "I am trapped inside Geisel Library", render)
        second = db.commit("same", effect, "I am trapped inside Geisel Library", render)
        assert first.fresh and first.changed
        assert not second.fresh
        assert first.twiml == second.twiml
        assert "Incident" not in first.say
        assert first.incident_id == 1
        assert len(db.incidents()) == 1
        state = db.load("+15551212")
        assert state.incident_id == 1
        assert state.incident_status == "escalated"
        follow = ask(state, "there is smoke to the south")
        assert follow.action == "escalate"
        assert follow.state.hazard == "S"
        db.commit("next", follow, "there is smoke to the south", render)
        rows = db.incidents()
        assert len(rows) == 1
        assert rows[0]["hazard"] == "S"
        assert rows[0]["status"] == "escalated"
        assert rows[0]["responder_state"] == "en_route"
        assert rows[0]["responder_note"] == "Rescue team"
        assert rows[0]["caller_told"] == 1
        assert "Geisel" in (rows[0]["place_name"] or "")
        db.db.execute("UPDATE incidents SET updated_at = ? WHERE id = 1", ("2000-01-01T00:00:00Z",))
        assert db.load("+15551212").incident_id is None


def test_a_caller_who_dials_back_is_not_asked_everything_again():
    """Hanging up must not cost the caller their place in the conversation."""
    state = dialogue.CallState(phone="+15557001", call_sid="CA-1")
    effect = dialogue.step(
        state,
        "I am at Price Center and there is smoke to the south",
        classify=lambda text: {"action": "guide", "place": "Price Center", "hazard": "S",
                               "need": "fire", "mobility": "mobile"},
        origins=lambda query: PRICE,
        route=lambda lat, lon, hazard, need, origin: HOSPITAL,
    )
    assert effect.action == "guide"
    carried = effect.state
    assert carried.destination_name == HOSPITAL.name

    # The call drops. Twilio sends a new CallSid, so the live line is cleared.
    redial = dialogue.replace(carried, call_sid="CA-2", last_say=None)
    back = dialogue.greet(redial)
    assert back.action == "resume"
    assert "Price Center" in back.say
    assert HOSPITAL.name in back.say
    assert back.say.endswith("Are you on your way?")

    # A trapped caller is asked about the room, never about a route.
    stuck = dialogue.replace(redial, incident_status="escalated", help_sent="Ambulance")
    stuck_say = dialogue.greet(stuck).say
    assert "Are you still inside?" in stuck_say
    assert "An ambulance is on the way to you." in stuck_say

    # A stranger, and a caller who already closed, both hear the plain opening.
    assert dialogue.greet(dialogue.CallState(phone="+15557002")).say == dialogue.OPENING
    done = dialogue.replace(redial, incident_status="closed")
    assert dialogue.greet(done).say == dialogue.OPENING

if __name__ == "__main__":
    test_jev_refuses_to_invent_a_place_and_gates_on_confidence()
    test_classifier_keeps_a_building_out_of_the_hazard()
    test_a_local_namesake_is_the_question_not_a_library_across_the_country()
    test_the_call_asks_before_it_sends_anyone_walking()
    test_a_trapped_caller_is_not_given_a_map()
    test_outage_and_unsafe_routes_do_not_invent_a_building()
    test_hurt_callers_get_a_hospital_and_can_close()
    test_repeat_and_twiml()
    test_a_phone_call_opens_gpt_live()
    test_signature()
    test_incidents_survive_and_retries_do_not_duplicate()
    test_a_caller_who_dials_back_is_not_asked_everything_again()
    print("ok")
