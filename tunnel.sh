#!/bin/bash
# Bring up a public tunnel and point ElevenLabs at it.
# Free tunnels die without warning, so `watch` keeps one alive for the demo.
cd "$(dirname "$0")"

start_tunnel () {
  pkill -f "serveo.net" 2>/dev/null
  pkill -f "localhost.run" 2>/dev/null
  sleep 1
  : > /tmp/serveo.log
  nohup ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=20 \
    -o ExitOnForwardFailure=yes -R 80:localhost:8810 serveo.net \
    > /tmp/serveo.log 2>&1 &
  for _ in $(seq 1 20); do
    sleep 2
    URL=$(grep -oE "https://[a-z0-9-]+\.serveousercontent\.com" /tmp/serveo.log | head -1)
    [ -n "$URL" ] && return 0
  done
  # serveo refused: fall back to the other free provider
  : > /tmp/tun.log
  nohup ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=20 \
    -o ExitOnForwardFailure=yes -R 80:127.0.0.1:8810 nokey@localhost.run \
    > /tmp/tun.log 2>&1 &
  for _ in $(seq 1 20); do
    sleep 2
    URL=$(grep -oE "https://[a-z0-9]+\.lhr\.life" /tmp/tun.log | head -1)
    [ -n "$URL" ] && return 0
  done
  return 1
}

repoint () {
  python3 - "$1" <<'PY'
import json, pathlib, re, sys, urllib.request, urllib.error
URL = sys.argv[1].rstrip("/")
ROOT = pathlib.Path("/Users/bijinjiby/Downloads/ares")
envp = ROOT / ".env"; s = envp.read_text()
envp.write_text(re.sub(r'PUBLIC_BASE_URL=.*', f'PUBLIC_BASE_URL="{URL}"', s))
env = {}
for l in envp.read_text().splitlines():
    if "=" in l and not l.strip().startswith("#"):
        k, v = l.split("=", 1); env[k.strip()] = v.strip().strip('"')
K = env["ELEVENLABS_API_KEY"]; AID = (ROOT/"data"/"agent_id").read_text().strip()
TOK = (ROOT/"data"/"api_token").read_text().strip()
def req(p, m="GET", b=None):
    r = urllib.request.Request("https://api.elevenlabs.io"+p, method=m,
        headers={"xi-api-key": K, "Content-Type": "application/json"},
        data=json.dumps(b).encode() if b else None)
    try:
        with urllib.request.urlopen(r, timeout=40) as x: return x.status, json.load(x)
    except urllib.error.HTTPError as e: return e.code, e.read().decode()[:200]
st, d = req(f"/v1/convai/agents/{AID}")
tools = d["conversation_config"]["agent"]["prompt"]["tools"]
for t in tools:
    t["api_schema"]["url"] = f"{URL}/v1/tools/{t['name']}?token={TOK}"
st, res = req(f"/v1/convai/agents/{AID}", "PATCH",
              {"conversation_config": {"agent": {"prompt": {"tools": tools}}}})
print("  agent repointed ->", st if st == 200 else res)
PY
}

# ---- watchdog mode: keep the tunnel alive until you stop it --------------
if [ "$1" = "watch" ]; then
  echo "watchdog running. Ctrl-C to stop."
  LAST=""
  while true; do
    URL=$(grep -oE "https://[a-z0-9-]+\.serveousercontent\.com" /tmp/serveo.log 2>/dev/null | head -1)
    CODE=000
    [ -n "$URL" ] && CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$URL/health")
    if [ "$CODE" != "200" ]; then
      echo "$(date +%H:%M:%S)  tunnel down ($CODE) — restarting"
      if start_tunnel; then
        echo "$(date +%H:%M:%S)  up: $URL"
        URL=$(grep -oE "https://[a-z0-9-]+\.serveousercontent\.com" /tmp/serveo.log | head -1)
        [ -z "$URL" ] && URL=$(grep -oE "https://[a-z0-9]+\.lhr\.life" /tmp/tun.log | head -1)
        repoint "$URL"
        LAST="$URL"
      else
        echo "$(date +%H:%M:%S)  could not raise a tunnel"
      fi
    elif [ "$URL" != "$LAST" ]; then
      repoint "$URL"; LAST="$URL"; echo "$(date +%H:%M:%S)  tunnel: $URL"
    fi
    sleep 15
  done
fi

# ---- one-shot setup ------------------------------------------------------
printf "raising a tunnel"
if ! start_tunnel; then echo; echo "no tunnel came up"; exit 1; fi
echo; echo "tunnel: $URL"
repoint "$URL"
echo "restarting ARES..."
lsof -ti:8810 | xargs kill 2>/dev/null
sleep 2
nohup python3 run.py > /tmp/ares.log 2>&1 &
sleep 9
TOK=$(cat data/api_token)
echo "  local  $(curl -s -m 8  -o /dev/null -w '%{http_code}' http://127.0.0.1:8810/health)"
echo "  tunnel $(curl -s -m 15 -o /dev/null -w '%{http_code}' "$URL/health")"
echo "  tools  $(curl -s -m 30 -o /dev/null -w '%{http_code}' -X POST "$URL/v1/tools/find_place?token=$TOK" -H 'Content-Type: application/json' -d '{"spoken":"I am at Price Center","phone":"+16190000000"}')"
echo
echo "READY. Call +1 743 330 4352"
echo "Keep it alive during the demo:   ./tunnel.sh watch"
