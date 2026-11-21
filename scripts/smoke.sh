#!/bin/bash -p
set -eu
python=/opt/petar-smoke/venv/bin/python3
if [[ ! -x "$python" ]]; then python=/usr/bin/python3; fi
exec /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 "$python" -IB \
  "${BASH_SOURCE[0]%/*}/petar_smoke.py" "$@"
