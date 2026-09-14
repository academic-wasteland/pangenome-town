#!/bin/sh
set -eu
pangenome-town doctor | python3 -c '
import json, sys
d = json.load(sys.stdin)
ok = True
for key in ("graph", "vcf"):
    print(key, d[key]["path"], "present" if d[key]["present"] else "MISSING")
    ok = ok and d[key]["present"]
sys.exit(0 if ok else 2)
'
