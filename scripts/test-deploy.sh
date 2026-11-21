#!/bin/bash -p
set -eu
case "${1-}:$#" in
  contracts:1) exec /usr/bin/python3 -IB "${BASH_SOURCE[0]%/*}/test-deploy.py" ;;
  isolated:1) exec /usr/bin/python3 -IB "${BASH_SOURCE[0]%/*}/test-deploy-isolated.py" ;;
  *) echo 'usage: test-deploy.sh contracts|isolated' >&2; exit 2 ;;
esac
