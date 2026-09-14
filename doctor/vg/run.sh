#!/bin/sh
set -eu
status=0
for tool in vg bcftools pangenome-town; do
  if command -v "$tool" >/dev/null 2>&1; then echo "$tool: $(command -v "$tool")"; else echo "$tool not found on PATH"; status=2; fi
done
exit $status
