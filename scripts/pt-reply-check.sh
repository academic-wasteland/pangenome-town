#!/bin/sh
# Formula check for the answer step: passes when an answer or notice replying
# to $PT_MESSAGE_ID (or the message_id var passed as $1) exists in the exchange log.
set -eu
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
message_id="${1:-${PT_MESSAGE_ID:-}}"
[ -n "$message_id" ] || { echo "no message id given" >&2; exit 1; }
pangenome-town messages --id "$message_id" | python3 -c '
import json, sys
message = json.load(sys.stdin)
answers = message.get("answers") or []
if answers:
    print("answered by", answers[0]["id"]); sys.exit(0)
print("no answer recorded for", message["id"], "status", message["status"]); sys.exit(1)
'
