# ARES — 60-second pitch script

Read it straight. Don't perform it. Pauses are marked.

---

**[0:00 – 0:10]  Camera. You, talking.**

> In a wildfire, the emergency line saturates.
> The person asking whether a road is open sits ahead of the person whose house is burning.
> *(beat)*
> Every product in this space makes one dispatcher faster. That doesn't scale past the number of people wearing headsets.

---

**[0:10 – 0:18]  Camera.**

> We built ARES. It's a phone line that decides.
> You call, you say what's wrong and where you are, and it either walks you to a specific named building away from the fire — or it refuses to send you anywhere.

---

**[0:18 – 0:33]  Screen. The demo. You narrate over it.**

> Here's one caller. *(run beat 1)*
> One sentence, one turn, two seconds — a real building, a real assembly point, pulled from OpenStreetMap at call time.
> *(beat)*
> Now watch three more callers. Each one only names a direction. South. East. Northwest.
> *(let the rays cross — SAY NOTHING for two seconds)*
> Nobody reported where the fire was. Their bearings crossed, and that's the fire.

---

**[0:33 – 0:43]  Screen. Beat 3.**

> This is a fourth caller. She never mentions fire at all.
> *(her line appears)*
> "Four other callers nearby report smoke to the south."
> *(beat)*
> She never told us where the fire was. They did. That needs cross-call state — which is why a product built around one dispatcher on one call can't do it.

---

**[0:43 – 0:52]  Screen. The refusal.**

> And when every way out is inside the fire, it doesn't guess. It says so.
> A system that always has an answer is a system that will confidently walk somebody into a fire.
> The model is never allowed to name a building. It can only pick words the caller actually said.

---

**[0:52 – 1:00]  Camera. Close.**

> Live wind from the National Weather Service. Live fire perimeters from NIFC. Satellites run three hours behind — our callers beat them by hours.
> *(beat)*
> We're ARES. We answer the calls a dispatcher will never reach.

---

## What to record

**1 — Camera (you, ~30s of talking):**

```bash
ffmpeg -y -f avfoundation -framerate 30 -video_size 1280x720 -i "0:0" -t 40 -c:v libx264 -preset ultrafast -crf 18 -c:a aac ~/Downloads/ares/take-camera.mp4
```

**2 — Screen (console + `./demo.sh` side by side, ~40s):**

```bash
ffmpeg -y -f avfoundation -framerate 30 -i "2:0" -t 45 -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p -c:a aac ~/Downloads/ares/take-screen.mp4
```

Press `q` in the terminal to stop early.

Then tell me, and I'll cut them together with the animated sequences as b-roll.
