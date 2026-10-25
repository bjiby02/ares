# ARES — stellar demo

Total: about 3 minutes. The phone is the demo. Everything else is support.

---

## Setup — 60 seconds before you walk on

```bash
cd ~/Downloads/ares && ./tunnel.sh
```

Wait for `READY. Call +1 743 330 4352`. If it doesn't say that, run it again — nothing
else works until it does.

Console on the projector:

```bash
open "http://127.0.0.1:8810/console?token=$(cat ~/Downloads/ares/data/api_token)"
```

Phone on speaker, volume up. Terminal where you can reach it, not on screen.

---

## [0:00] Open cold. No slides.

> "In 2003 the Cedar Fire burned 273,000 acres and took fifteen lives, and San Diego
> County dispatch took the heaviest call volume it had ever seen.
>
> *(beat)*
>
> When a fire moves, the line saturates. The person asking whether a road is open
> sits ahead of the person whose house is burning.
>
> Every product in this space — Axon, Prepared, Carbyne — makes one dispatcher
> faster. That does not scale past the number of people wearing headsets."

---

## [0:25] Seed the swarm. Say this while it runs.

```bash
./demo.sh seed
```

> "Three people are calling from different parts of campus. None of them knows where
> the fire is. Each one only says which way the smoke is blowing. South. East.
> Northwest."

**Point at the map. Stop talking.** Let the rays cross and the marker pulse.

> "Nobody reported a location. Three bearings crossed, and that is the fire —
> to within 1.3 kilometres.
>
> *(beat)*
>
> The amber squares are VIIRS satellite detections. The satellite agrees with our
> callers. But look at the timestamp — it was acquired hours ago. Satellites run
> about three hours behind the overpass. **Our callers beat them.**"

---

## [1:10] The live call. This is the demo.

**Dial +1 743 330 4352 on speaker.**

> "Now I'm a fourth caller. Watch — I will not mention fire."

| You say | What to point at |
|---|---|
| *"I'm at Price Center."* | the dashboard — you appear in real time |
| — | the line tells you smoke is to the south |

> "I never said there was a fire. The other three told it. That needs cross-call
> state, which is why a product built around one dispatcher on one call cannot
> do this."

**Then, still on the call:**

> *"I can't move."*

> "It stops routing. It escalates. A map is no use to someone who can't walk."

Point at the right-hand panel: **unit assigned, ETA, and the nearest CCTV camera**
to the caller — so a dispatcher can confirm the story without asking someone to
describe a fire while they're running from it.

---

## [2:20] The refusal. Close on this.

> "And when every way out is inside the fire, it does not guess.
>
> A destination has to sit ninety degrees clear of the hazard. Below forty,
> ARES sends nobody anywhere and says so:
>
> *'I will not send you toward the danger. Stay in a solid building, or move away
> from it on foot.'*
>
> *(beat)*
>
> A system that always has an answer is a system that will confidently walk
> somebody into a fire.
>
> The model is never allowed to name a building. It can only choose words the
> caller actually said — that's enforced by the tool contract, not by a prompt.
> An invented hospital is structurally impossible."

---

## [2:50] Land it.

> "Live wind from the National Weather Service. Live fire perimeters from NIFC —
> no route may cross one. Live satellite. Live cameras. Buildings from
> OpenStreetMap at call time.
>
> We're ARES. We answer the calls a dispatcher will never reach."

---

# If something breaks

| What you see | What to do |
|---|---|
| Call connects, silence after the greeting | tunnel died — `./tunnel.sh`, then redial |
| Map empty / "list did not load" | reload the console tab |
| Swarm count looks wrong | `./demo.sh seed` resets it |
| Anything else | keep talking, run `./demo.sh` — it needs no phone |

**`./demo.sh` with no arguments runs the whole thing without a phone.** That is your
fallback if the call fails. Rehearse it once so you can switch without pausing.

---

# Questions you will get

**"What if the callers are wrong?"**
> Three have to agree within three kilometres or we infer nothing. An inferred hazard
> is never allowed to corroborate the next caller — otherwise one report snowballs
> into a crowd of fake ones. That guard is in the code.

**"Why not just use satellites?"**
> We do — you saw the detections. But that one was acquired hours before the call.
> Satellites confirm; callers are the fast signal.

**"How do you stop it hallucinating a hospital?"**
> The model can't name one. It calls a tool and reads back the string. Destinations
> come from OpenStreetMap filtered by policy. It's structural.

**"How do you handle model uncertainty?"**
> It can't close a call unless the caller actually said they were safe — enforced in
> code, with a test. Next is a harness returning calibrated confidence per decision,
> so the line can abstain and ask. Refusal is already a first-class output.

**"Is this a real phone number?"**
> You just heard it. Twilio to an ElevenLabs agent; every decision is made by our
> own core, which the model cannot overrule.
