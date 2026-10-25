"""Print the located fire, for the demo. Reads /v1/swarm."""
import json, pathlib, sys, urllib.parse, urllib.request
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8810"
T = (pathlib.Path(__file__).with_name("data") / "api_token").read_text().strip()
with urllib.request.urlopen(B + "/v1/swarm?" + urllib.parse.urlencode({"token": T}),
                            timeout=25) as r:
    d = json.load(r)
s, sat = d.get("source"), d.get("satellite") or {}
R, Y, N = "\033[1;31m", "\033[0;33m", "\033[0m"
if not s:
    print("  (no source yet)")
else:
    print(f"  {R}FIRE LOCATED  {s['lat']:.5f}, {s['lon']:.5f}{N}")
    print(f"  by {s['support']} callers, to within {s['spread_m']} m")
    if sat.get("confirms"):
        print(f"  {Y}confirmed by VIIRS {sat['sat']} at {sat['km']} km, "
              f"acquired {sat['at']}{N}")
    print(f"  {len(d.get('hotspots') or [])} satellite detections on the board")
