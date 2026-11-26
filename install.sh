#!/bin/sh
set -eu
if [ -f "$0" ]; then
    case "$0" in
        */*) directory=${0%/*} ;;
        *) directory=. ;;
    esac
    if [ -f "$directory/install.py" ]; then
        exec python3 "$directory/install.py" "$@"
    fi
fi

exec python3 -c '
import os
from urllib.request import urlopen

url = os.environ.get("MIRAGE_INSTALLER_URL", "https://raw.githubusercontent.com/dshein-alt/mirage/master/install.py")
with urlopen(url, timeout=60) as response:
    source = response.read()
exec(compile(source, url, "exec"))
' --standalone "$@"
