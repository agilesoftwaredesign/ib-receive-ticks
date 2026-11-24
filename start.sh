#!/usr/bin/env bash
# Start ib-receive-ticks. Creates ./venv and installs requirements on first run.
# All arguments are passed through to main.py, e.g. ./start.sh --only-new-instrument --days 7
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "venv not found, creating it..."
    PYTHON="$(command -v python3 || command -v python || true)"
    if [ -z "$PYTHON" ]; then
        echo "Error: python3 not found on PATH." >&2
        exit 1
    fi
    rm -rf venv
    "$PYTHON" -m venv venv
    if ! venv/bin/python -m pip install -r requirements.txt; then
        echo "Error: dependency installation failed, removing venv so the next run retries." >&2
        rm -rf venv
        exit 1
    fi
fi

# shellcheck disable=SC1091
source venv/bin/activate
exec python main.py "$@"
