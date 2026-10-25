#!/bin/bash
# ARES stage demo. ENTER advances each beat, so you control the pace.
B="${ARES_BASE:-http://127.0.0.1:8810}"
T="$(cat "$(dirname "$0")/data/api_token")"
STEP="${ARES_STEP:-1}"
beat () { printf "\n\033[1;36m%s\033[0m\n" "$1"; [ "$STEP" = "1" ] && read -r -s; }
line () { printf "\n\033[1;37m%s\033[0m\n" "$1"; }
call () {
  printf "\033[0;33m  caller:  %s\033[0m\n" "$2"
  curl -s -m 90 --get "$B/preview" \
    --data-urlencode "token=$T" --data-urlencode "phone=$1" --data-urlencode "q=$2" \
  | python3 -c "import json,sys;print('  \033[1;32mARES  :  '+json.load(sys.stdin)['say']+'\033[0m')"
}
# The server clears its own tables, so the console and the swarm agree with
# what the room just watched. Safe to run as many times as you like.
curl -s -m 15 "$B/v1/demo/reset?token=$T" > /dev/null
R="$(date +%H%M%S)"
clear
printf "\033[1;31m\n   A R E S\033[0m   a phone line that decides\n"
printf "\n   \033[0;37mSan Diego, 2003. The Cedar Fire burns 273,246 acres and takes 15 lives.\n"
printf "   County dispatch takes the heaviest call volume it has ever seen.\n"
printf "   2025, Los Angeles: the Palisades and Eaton fires destroy more than\n"
printf "   16,000 structures. Evacuation orders reach nearly 200,000 people.\n"
printf "   Every product in this space makes one dispatcher faster.\n"
printf "   That does not scale past the number of people wearing headsets.\033[0m\n"

if [ "$1" = "seed" ]; then
  # Three bearing reports and nothing else, so the next voice on the line is
  # a real caller who never mentions fire.
  printf "\n\033[1;36mSeeding three callers, each naming only a direction.\033[0m\n"
  call "+1619${R}2" "I am at Torrey Pines Gliderport and there is smoke to the south"
  call "+1619${R}3" "I am at Birch Aquarium and there is smoke to the east"
  call "+1619${R}4" "I am at Westfield UTC and there is smoke to the northwest"
  printf "\n"
  python3 "$(dirname "$0")/swarmline.py" "$B"
  printf "\n\033[1;37m  Now call +1 743 330 4352 and say nothing about fire.\033[0m\n\n"
  exit 0
fi

beat "[1]  One person. One sentence."
call "+1619${R}1" "I am at Student Services Center and there is smoke to the south"
line "     One turn. A real building. A real assembly point."

beat "[2]  Three more callers. Each one names only a direction."
call "+1619${R}2" "I am at Torrey Pines Gliderport and there is smoke to the south"
call "+1619${R}3" "I am at Birch Aquarium and there is smoke to the east"
call "+1619${R}4" "I am at Westfield UTC and there is smoke to the northwest"

beat "     >>>  LOOK AT THE MAP  <<<"
curl -s -m 20 "$B/v1/swarm?token=$T" | python3 -c "
import json,sys
s=json.load(sys.stdin)['source']
print(f\"\n  \033[1;31mFIRE LOCATED  {s['lat']:.5f}, {s['lon']:.5f}\033[0m\") if s else print('  (no source)')
print(f\"  by {s['support']} callers, to within {s['spread_m']} metres\n\") if s else None"
line "     Not one of them reported a fire location. The map worked it out."

beat "[3]  A fourth caller. She never mentions fire."
call "+1619${R}5" "I am at Price Center and I need to get out"
line "     She never told us where the fire was.  They did."

beat "[4]  And when every way out is unsafe..."
call "+1619${R}6" "I am trapped at Geisel Library and I cannot breathe"
line "     No route. Because routing her would be a lie."
printf "\n"
