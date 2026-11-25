#!/usr/bin/env bash
# Shared VM setup: uv + CPython 3.11 virtualenv with packages resolved as of the original campaign date.
set -euo pipefail
VENV=$1; shift
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh >/dev/null
export PATH=$HOME/.local/bin:$PATH
sudo mkdir -p $VENV && sudo chown $USER $VENV
uv venv -q --python 3.11.16 $VENV 2>/dev/null || uv venv -q --python 3.11 $VENV
uv pip install -q --python $VENV/bin/python --exclude-newer 2026-09-12T00:00:00Z "$@"
$VENV/bin/python -V; uv pip freeze --python $VENV/bin/python > $VENV/freeze.txt
