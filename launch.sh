#!/usr/bin/env bash
# Launch local-brain desktop app

cd ~/local-brain || exit 1
source .venv/bin/activate
exec python desktop.py "$@"
