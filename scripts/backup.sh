#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA=${MIRAGE_DATA_DIR:-$ROOT/data}
. "$ROOT/scripts/common.sh"
ENGINE=$(mirage_engine)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=$DATA/backups/$STAMP
mkdir -p "$DEST"
"$ENGINE" exec mirage python -c 'from mirage.database import Database; from pathlib import Path; db=Database(Path("/data/mirage.sqlite3")); db.backup(Path("'"$DEST"'/mirage.sqlite3")); db.close()'
for item in config inputs outputs; do
    [ ! -e "$DATA/$item" ] || cp -a "$DATA/$item" "$DEST/"
done
printf 'Backup created: %s\nModels and runtimes are intentionally excluded; reinstall them from manifests.\n' "$DEST"
