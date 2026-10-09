#!/usr/bin/env bash
# Create a virtual environment if needed, install requirements, and run the painter.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (3.9 or newer)." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -r requirements.txt
exec python animator.py "$@"
