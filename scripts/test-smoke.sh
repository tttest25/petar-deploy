#!/bin/bash -p
set -eu
case "${1-}:$#" in
  contracts:1) exec /usr/bin/python3 -IB "${BASH_SOURCE[0]%/*}/test-smoke.py" ;;
  isolated:1) exec /usr/bin/python3 -IB "${BASH_SOURCE[0]%/*}/test-smoke-isolated.py" ;;
  *) echo 'usage: test-smoke.sh contracts|isolated' >&2; exit 2 ;;
esac
