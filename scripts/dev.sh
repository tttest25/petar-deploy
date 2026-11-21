#!/bin/bash -p
exec /usr/bin/python3 -IB "${BASH_SOURCE[0]%/*}/petar_operator.py" dev "$@"
