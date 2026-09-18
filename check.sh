#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
.venv/bin/python diagnostics.py
.venv/bin/python -m unittest discover -s tests -v
