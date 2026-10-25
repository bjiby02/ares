# ARES

**A phone line that decides.**

A person in danger dials a number. They say what is wrong and where they are.
ARES either walks them to a specific named building away from the hazard, or
tells them to stay exactly where they are — and when every exit is unsafe, it
refuses to send them anywhere.

No app. No account. No screen. Voice only.

---

## Why this exists

In a disaster, emergency lines saturate. Everyone waits in one queue: the person
asking whether a road is open sits ahead of the person whose house is burning.
In the 2003 Cedar Fire, San Diego's dispatch centres took the highest call
volume they had ever seen. In Australia's Black Summer, one service went from
164 calls to over 14,000 in 24 hours and was taken offline.

Every existing product in this space — Axon 911, Prepared, Carbyne, RapidSOS —
is a **call-taker assist** tool. They make a human dispatcher faster. That does
not scale past the number of people wearing headsets.

ARES **absorbs**. It answers the calls a dispatcher will never reach, handles
them to completion, and hands a human only the ones that need one.

---

## The three properties that matter

### 1. It refuses

Buildings must be at least 90° away from the hazard. If none qualify, it takes
the single farthest-from-danger building — but only above 40°. Below that it
sends nobody anywhere and says so:

> *"I will not send you toward the danger. Stay in a solid building, or move
> away from it on foot."*

A system that always produces an answer is a system that will confidently walk
somebody into a fire.

### 2. The model cannot name a building

The language model may only select a span of text **the caller actually said**.
It never writes the destination. Destinations come from OpenStreetMap, filtered
by policy. An invented hospital is structurally impossible, not discouraged by a
prompt.

In the GPT-Live path this is enforced by the tool contract: the model calls a
tool and reads back the `say` string verbatim.

### 3. Callers are sensors for each other

Three people each say *"smoke to my south / west / east"*. Their bearings are
back-projected and the rays cross — fixing the fire to within 130 m. A fourth
caller who names no hazard at all inherits the direction:

> *"3 other callers nearby report smoke to the south. From Price Center in San
> Diego. Head north, away from the smoke, about 1.2 kilometers, toward Scripps
> Memorial Hospital La Jolla."*

She never told us where the fire was. The first three did.

**This is the thing no competitor can do**, because it requires cross-call state
and they are all built around a single dispatcher on a single call.

---

## Quick start

```bash
cd ~/Projects/ares
cp .env.example .env        # then fill in the keys you have
python3 run.py
```

Wait for:

```
voice           gpt-live-1 / webrtc
tools           4 exposed at /v1/tools
area centre     32.8797, -117.2362  (env)
feeds prewarmed in 4.4s
geocache warmed 12/12 landmarks
```

Then, in another terminal:

```bash
# the dispatcher console
open "http://127.0.0.1:8810/console?token=$(cat data/api_token)&as=Engine%2035"

# rehearse a turn without a handset
curl -s --get http://127.0.0.1:8810/preview \
  --data-urlencode "token=$(cat data/api_token)" \
  --data-urlencode "phone=+16190000001" \
  --data-urlencode "q=I am at Geisel Library and there is smoke to the south"
```

Run the tests:

```bash
python3 tests/test_flow.py && python3 tests/test_product.py && python3 tests/test_swarm.py
# each prints: ok
```

---

## Architecture

Two paths run at once. The **live path** never stops talking; the **async path**
thinks underneath it. A hospital lookup can take four seconds and the caller
hears none of them.

```
  caller ──voice──┐
                  ▼
        ┌──────────────────┐     SDP      ┌──────────────┐
        │  transport       │◄────────────►│  GPT-Live-1  │  live path
        │  Twilio PSTN     │   WebRTC     │  full duplex │  0.798 s turns
        │  or WebRTC       │              └──────┬───────┘
        └──────────────────┘                     │ tool calls
                  │                              ▼
                  │                     ┌──────────────────┐
                  └────TwiML────────────│  decision core   │  async path
                                        │  classify        │
                                        │  ground          │
                                        │  narrow          │
                                        │  swarm           │
                                        │  cone + wind     │
                                        │  choose          │
                                        └────────┬─────────┘
                                                 ▼
                                        ┌──────────────────┐
                                        │  store (SQLite)  │
                                        └────────┬─────────┘
                                                 ▼
                                        ┌──────────────────┐
                                        │  console         │  human supervisor
                                        │  claim / roll /  │  can seize the call
                                        │  arrive / close  │
                                        └────────┬─────────┘
                                                 │
                       caller hears it ◄─────────┘  on their next turn
```

### The pure-turn rule

`dialogue.py` must **never** import network or database code. `classify`,
`origins`, `route` and `nearby` are injected. That is why the policy is testable
without a phone, and why the same core serves both Twilio and GPT-Live without
modification.

---

## Repo map

| File | Lines | Role |
|---|---:|---|
| `geo.py` | 537 | Search, namesake narrowing, hazard cone, wind drift, water barriers, `choose()` |
| `server.py` | 477 | HTTP routes, Twilio TwiML, tool dispatch, SDP relay, responder actions |
| `dialogue.py` | 440 | One pure turn. The six actions live here |
| `classify.py` | 348 | Harness ladder: Jev → OpenAI → Anthropic → rules. Grounding |
| `store.py` | 282 | SQLite WAL. Incidents, sessions, turns, idempotent replies, geocache |
| `tools.py` | 210 | The four tools GPT-Live may call. Schemas + system prompt |
| `feeds.py` | 206 | NWS wind, ALERTCalifornia cameras, NIFC fires. Cached, prewarmed |
| `swarm.py` | 144 | Back-projection, coherence, cascade guard |
| `gptlive.py` | 78 | WebRTC SDP negotiation against `/v1/live/sessions` |
| `directory.py` | 48 | Cache in front of `geo`. Injects live wind into routing |
| `voice.py` | 82 | TwiML render, E.164, Twilio signature |
| `run.py` | 14 | Entry point. Parses `.env` safely (values with spaces survive) |
| `web/console.*` | — | Dispatcher console: map, layers, alert feed, cameras, lanes, actions |
| `tests/` | 446 | `test_flow`, `test_product`, `test_swarm` |

---

## The six actions

| Action | When | What the caller hears |
|---|---|---|
| `guide` | they can move and a place resolved | *From {origin}. Head {dir}, away from the smoke, about {dist}, toward {building}.* |
| `escalate` | trapped, cannot breathe, unconscious | *Stay low. Do not open the door.* — priority 0 |
| `ask` | place missing or ambiguous | *I found {A}, and {B}. Which one?* — no incident opened |
| `repeat` | they ask again | the stored instruction, 0 ms, no network |
| `correct` | they reject the place | *Where are you?* — hazard is kept |
| `close` | they are safe | *Incident {id} is closed.* |
| **refuse** | **every building is inside the cone** | ***I will not send you toward the danger.*** |

---

## API

All token-guarded except `/health` and the Twilio webhooks.

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | liveness |
| `/voice`, `/gather` | POST | Twilio webhook, signature-checked |
| `/preview` | GET | Rehearse a turn without a handset |
| `/console` | GET | Dispatcher console |
| `/v1/live/offer` | POST | Browser SDP offer in, GPT-Live SDP answer out |
| `/v1/tools/{name}` | POST | `find_place`, `plan_route`, `escalate`, `close_call` |
| `/v1/tools/schema` | GET | Tool schemas, prompt, `session.update` body |
| `/v1/voice` | GET | Transport status and what is blocking it |
| `/v1/situation` | GET | Cached wind, cameras, fires |
| `/v1/trace` | GET | Live/async tool telemetry |
| `/v1/incidents` | GET | Priority queue |
| `/v1/incidents/{id}/respond` | POST | `claimed` → `en_route` → `on_scene` → `resolved` |

---

## Live data

Every endpoint below was called and verified against its real service.

| Feed | What it changes | Key | Cache |
|---|---|---|---|
| **Our own callers** | **the hazard location itself** | none | live |
| Photon / Nominatim | origin and candidate buildings | none | 12 h / 1 h |
| NWS | wind bearing; widens the forbidden arc downwind | none | 15 min |
| ALERTCalifornia | 28 cameras in 40 km, nearest UCSD TDLLN 1 at 0.96 km | none | 6 h |
| NIFC | active fire perimeters within range | none | 30 min |

Cold fetch 2.3 s, warm 1 ms. `prewarm()` runs at boot and also warms demo
landmarks in the geocache. A feed failure returns last-good and marks it stale —
it can never raise into the call path.

**Satellites are not real time.** VIIRS near-real-time runs ~3 hours behind the
overpass. Our callers beat the satellites by hours. That is the argument for the
swarm, not against it.

---

## Nothing is hardcoded

| Concern | Where it lives |
|---|---|
| Service area centre | geocoded from `OPERATOR_AREA_CITY`; `_LAT`/`_LON` override |
| Assembly points | `data/refuges.json` via `OPERATOR_REFUGES` |
| Unwalkable water | `data/barriers.json` via `OPERATOR_BARRIERS` |
| Refuge walking range | `OPERATOR_REFUGE_RANGE_M` (default 4 km) |
| Landmarks to pre-warm | `OPERATOR_WARM_PLACES` or `OPERATOR_WARM_FILE` |

No coordinates, place lists, or fabricated feed rows exist in the source. The
console renders only what `/v1/situation` and `/v1/incidents` actually return;
when a feed is absent it says so rather than inventing a row.

---

## Deploying to a new city

1. Set `OPERATOR_AREA_CITY` and `OPERATOR_AREA_REGION`.
2. Write `data/refuges.json` with that city's real assembly points.
3. Write `data/barriers.json` with any water or freeway a person cannot cross on
   foot. Each entry is a segment; a route that crosses it is rejected.
4. Set `OPERATOR_WARM_PLACES` to the landmarks you expect callers to name.
5. Restart. The boot line prints the resolved area centre.

Everything else — hospitals, roads, place names — comes from OpenStreetMap at
call time.

---

## Development plan

### Done

- [x] Six-action contract, pure turn, injected dependencies
- [x] Verbatim grounding — the model may not name a destination
- [x] Hazard cone, 90° / 40° fallback, explicit refusal
- [x] Namesake disambiguation — asks instead of guessing
- [x] Swarm inference with coherence check and cascade guard
- [x] Wind-aware routing (downwind arc from live NWS)
- [x] Water barrier rejection
- [x] Refuge preference for fire
- [x] Idempotent replay under Twilio retry
- [x] Four GPT-Live tools + WebRTC SDP negotiation
- [x] Dispatcher console with live map, cameras, alert feed, lanes
- [x] Responder loop: claim → en route → on scene → resolved
- [x] Supervisor seize — the model stands down for a human
- [x] Voice only: no SMS anywhere in the call path

### Next

| Pri | Task | Blocked by | Notes |
|---|---|---|---|
| 1 | Buy a Twilio number | payment method | Account is Trial, $0.00, 0 numbers. **Nobody can dial until this exists.** Then set `TWILIO_PHONE_NUMBER` and point VoiceUrl at `{PUBLIC_BASE_URL}/voice` |
| 2 | `OPERATOR_RESPONDER_NUMBER` | a phone number | Not currently used — notification is voice-only by design |
| 3 | Spanish | — | Large local need. Needs a model harness; the rules fallback is English-only |
| 4 | Proactive callback | a number | When a new report puts hazard in the direction someone was sent, call them back |
| 5 | Real CAD integration | an agency | Unit rows in the console are notional. This is the integration point |

### Invariants — do not weaken these

1. **Rule 1 of the system prompt.** The model may never name a building.
2. **`store.reports()` cascade guard.** An inferred hazard must never corroborate
   the next caller, or one real report snowballs into a crowd of fake ones.
3. **`dialogue.py` stays pure.** No network, no database.
4. **Refusal is a valid output.** Do not add a fallback that always answers.

---

## Testing

```bash
python3 tests/test_flow.py      # routing policy
python3 tests/test_product.py   # call behaviour, TwiML, signatures
python3 tests/test_swarm.py     # back-projection, coherence, cascade guard
```

End-to-end smoke test, including the responder loop:

```bash
# reset, then three bearing reports, then a caller who names no hazard
sqlite3 data/operator.db "DELETE FROM incidents; DELETE FROM sessions;"
# → the fourth caller must hear "3 other callers nearby report smoke to the..."
```

See `RUNBOOK.md` for the full demo sequence and the exact phrases that resolve
cleanly on stage.

---

## Known limitations

- **No phone number yet.** The dial path is code-complete and signature-verified
  but has never carried a real call.
- **GPT-Live is WebRTC only.** There is no SIP dial-in and no server-side session
  mint on this API surface. Earlier claims of SIP were wrong.
- **Console unit rows are notional.** There is no CAD feed. Say so if asked.
- **`/v1/live/sessions` accepts only `model` and `instructions`.** Tools, voice
  and turn detection are applied with `session.update` over the data channel.
