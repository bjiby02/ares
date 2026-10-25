# ARES — demo runbook

## Start
```
python3 run.py
```
Loads `.env` safely (values with spaces survive), prewarms the feed cache, binds :8810.

## Console
http://127.0.0.1:8810/console?token=$(cat data/api_token)

Polls `/v1/incidents` every 2s and `/v1/situation` every 30s. Camera tiles are
clickable and open the real ALERTCalifornia feed.

## The demo, in order

**1. Hand a judge a phone.** Any phrasing works. Good first line:
> "I am at Geisel Library and there is smoke to the south"

It asks which Geisel — that is the point. Then: "Torrey Pines".
Watch the console: candidate table shows UCSD Health **dropped**, Scripps **kept**.

**2. The swarm.** Three judges call and each names a direction.
These three resolve cleanly — no namesake question:
- "I am at Torrey Pines Gliderport and there is smoke to the south"
- "I am at Birch Aquarium and there is smoke to the east"
- "I am at Westfield UTC and there is smoke to the northwest"

**3. The fourth judge** says nothing about fire:
> "I am at Price Center and I need to get out"

ARES answers: **"3 other callers nearby report smoke to the south.** From Price
Center in San Diego. Head northeast, away from the smoke, about 1.2 kilometers,
toward Scripps Memorial Hospital La Jolla."

Line to say: *"She never told us where the fire was. You three did."*

## Avoid on stage
- "Scripps Pier" — two of them, triggers a namesake question mid-flow.
- Any landmark outside San Diego; the service area filter is doing real work.

## Voice only
No SMS anywhere. `<Message>` is never emitted, there is no walking link, and the
line never says "I am texting you this". Everything the caller gets, they hear.
Responder news reaches them on their next spoken turn.

## The responder loop
Open the console as a unit:
```
http://127.0.0.1:8810/console?token=$(cat data/api_token)&as=Engine%2035
```
Each open incident has one button: CLAIM -> ROLL -> ARRIVE -> CLOSE.
The caller hears the change on their next turn:
- claimed  -> "A dispatcher has your report and is looking at it now."
- en_route -> "Help is on the way to you."
- on_scene with supervised -> the model stands down: "A dispatcher is on this
  call with you now. Stay on the line."
- resolved -> incident closes.

Drive it from the API instead:
```
curl -s -X POST "localhost:8810/v1/incidents/<ID>/respond?token=$(cat data/api_token)" \
  -H 'Content-Type: application/json' \
  -d '{"state":"en_route","who":"Engine 35"}'
```

## Reset between runs — DO THIS RIGHT BEFORE YOU PRESENT
Rehearsal reports accumulate. Nine runs makes the line say "9 other callers",
which is honest but will not match what the room just watched.
```
sqlite3 data/operator.db "DELETE FROM incidents; DELETE FROM sessions; DELETE FROM turns; DELETE FROM responses;"
```
Wipe everything, not a phone prefix. Rehearsal numbers vary, and any leftover
report with a hazard stays in the swarm pool and inflates the count.
The geocache is deliberately left alone — that is what keeps the demo fast.

## Rehearse without a handset
```
curl -s --get http://127.0.0.1:8810/preview \
  --data-urlencode "token=$(cat data/api_token)" \
  --data-urlencode "phone=+16195559999" \
  --data-urlencode "q=I am at Price Center and I need to get out"
```

## Live feeds (all keyless, all cached)
| Feed | Gives | TTL |
|---|---|---|
| NWS | wind 137° from, drift toward 317°, 12.9 kph | 15 min |
| ALERTCalifornia | 28 cameras in 40 km, UCSD TDLLN 1 at 1.12 km | 6 h |
| NIFC | Henderson 79 ac, Norte 570 ac within 120 km | 30 min |
| Photon / Nominatim | origin + candidate buildings | 1–12 h |

Cold fetch 2.3 s, warm 1 ms. Prewarmed at boot, so the first call is never cold.

## Still open
- `TWILIO_PHONE_NUMBER`, `TYPESAFE_API_KEY`, `OPERATOR_RESPONDER_NUMBER` are empty.
  No number means no judge can dial; no classifier key means the rule fallback runs
  (good enough for every phrase above, but "beside SeaWorld" needs a model).
