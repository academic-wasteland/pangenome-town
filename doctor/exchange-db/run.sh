#!/bin/sh
set -eu
pangenome-town messages --limit 1 >/dev/null && echo "exchange log ok"
pangenome-town town-info | python3 -c 'import json,sys; d=json.load(sys.stdin); print("peers:", d["peers"]); sys.exit(0 if d["peers"] else 2)'
