#!/bin/sh
# Formula check for the answer step. Gas City runs it after the iteration bead
# closes, with GC_BEAD_ID set to that bead and cwd = city root. Passes only when
# an answer (or notice) replying to the workflow's message_id is in the exchange log.
set -eu
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
message_id="${1:-${PT_MESSAGE_ID:-}}"
if [ -z "$message_id" ] && [ -n "${GC_BEAD_ID:-}" ]; then
  message_id=$( { gc bd show "$GC_BEAD_ID" --json 2>/dev/null; root=$(gc bd show "$GC_BEAD_ID" --json 2>/dev/null | grep -oE '"gc\.root_bead_id": *"[^"]+"' | head -1 | sed 's/.*: *"//; s/"$//'); [ -n "$root" ] && gc bd show "$root" --json 2>/dev/null; } | grep -oE '"gc\.var\.message_id": *"[^"]+"' | head -1 | sed 's/.*: *"//; s/"$//')
fi
[ -n "$message_id" ] || { echo "no message id (arg, PT_MESSAGE_ID, or GC_BEAD_ID metadata)" >&2; exit 1; }
[ -f town.toml ] && export PT_TOWN_TOML="${PT_TOWN_TOML:-$PWD/town.toml}"
pangenome-town messages --id "$message_id" | python3 -c '
import json, sys
message = json.load(sys.stdin)
answers = message.get("answers") or []
if answers:
    print("answered by", answers[0]["id"]); sys.exit(0)
print("no answer recorded for", message["id"], "status", message["status"]); sys.exit(1)
'
