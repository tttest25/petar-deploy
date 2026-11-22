#!/bin/bash -p
set -eu

if [[ "$#:${1-}" != 1:--confirm-restart ]]; then
  cat >&2 <<'EOF'
Usage: bash scripts/retest-rehearsal.sh --confirm-restart
Confirm a dedicated rehearsal window: no other submissions or runner ticks.
This sends two jobs and restarts all three rehearsal services. It does not
reinstall, remove containers, clear old jobs, or mark a release as accepted.
EOF
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec /bin/bash "$script_dir/smoke.sh" rehearsal deploy-001 --confirm-restart
