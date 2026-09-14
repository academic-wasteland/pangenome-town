#!/bin/sh
# Pull the JaSaPaGe pangenome files from the DDBJ gateway into the shared data dir.
# Usage: scripts/fetch-data.sh [dest-dir]   (needs `ssh ddbj` configured)
set -eu
dest="${1:-$(dirname "$0")/../../data/jasapage}"
mkdir -p "$dest"
cd "$dest"
printf '%s\n' JaSaPaGe.GRCh38.vcf.gz JaSaPaGe.GRCh38.vcf.gz.tbi JaSaPaGe.snarls JaSaPaGe.gbz JaSaPaGe.hapl > files.txt
rsync -a --partial --info=progress2 --files-from=files.txt ddbj:/home/asianhla/data/JaSaPaGe/ .
sha256sum JaSaPaGe.* > MANIFEST.sha256
echo "done: $(pwd)"
