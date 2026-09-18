#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then python3 setup.py; fi
exec .venv/bin/python launch.py "$@"
