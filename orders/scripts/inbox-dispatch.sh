#!/bin/sh
# For every pending peer question, start an answer-peer workflow routed to the
# town's agent and record the dispatch in the exchange log.
set -eu
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
if [ -z "${PT_TOWN_TOML:-}" ]; then
  for candidate in "${GC_CITY_PATH:-}" "${GC_CITY_ROOT:-}" "$PWD"; do
    if [ -n "$candidate" ] && [ -f "$candidate/town.toml" ]; then PT_TOWN_TOML="$candidate/town.toml"; break; fi
  done
fi
: "${PT_TOWN_TOML:?PT_TOWN_TOML must point at this city's town.toml (GC_CITY_PATH unset?)}"
export PT_TOWN_TOML
city_root=$(dirname "$PT_TOWN_TOML")
town=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["town"]["name"])' "$PT_TOWN_TOML")
rig=$(python3 -c 'import tomllib,sys; t=tomllib.load(open(sys.argv[1],"rb"))["town"]; print(t.get("rig") or t["name"]+"-rig")' "$PT_TOWN_TOML")
agent=$(python3 -c 'import tomllib,sys; t=tomllib.load(open(sys.argv[1],"rb"))["town"]; print(t.get("agent") or "pangenome-town.townsfolk")' "$PT_TOWN_TOML")
pangenome-town --town "$PT_TOWN_TOML" inbox | python3 -c '
import json, sys
for message in json.load(sys.stdin)["pending"]:
    body = message["envelope"]["body"]
    print("\t".join([message["id"], message["from"], (body.get("text") or "").replace("\t", " ").replace("\n", " ")[:400]]))
' | while IFS="$(printf '\t')" read -r message_id peer question; do
  [ -n "$message_id" ] || continue
  echo "dispatching $message_id from $peer to $rig/$agent"
  if gc sling --city "$city_root" "$rig/$agent" answer-peer --formula \
       --var "message_id=$message_id" --var "peer=$peer" --var "question=$question" --nudge; then
    pangenome-town --town "$PT_TOWN_TOML" inbox --mark-dispatched "$message_id" --detail "answer-peer via $rig/$agent" >/dev/null
  else
    echo "sling failed for $message_id" >&2
  fi
done
