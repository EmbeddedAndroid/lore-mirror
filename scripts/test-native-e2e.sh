#!/usr/bin/env bash
# Offline fixture replay only. Does not bootstrap dependencies or fetch mail.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
if [[ $# -gt 0 ]]; then
  echo 'Usage: LORE_NATIVE_ENV=/absolute/path/to/env.sh scripts/test-native-e2e.sh' >&2
  exit 2
fi
if [[ -n ${LORE_NATIVE_ENV:-} ]]; then
  [[ -r "$LORE_NATIVE_ENV" ]] || { echo "Native environment file missing: $LORE_NATIVE_ENV" >&2; exit 2; }
  source "$LORE_NATIVE_ENV"
elif [[ -r "$ROOT/.runtime/env.sh" ]]; then
  source "$ROOT/.runtime/env.sh"
fi
PYTHON=${PYTHON:-python3}
if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# Ordinary unittest discovery visibly skips when native dependencies are absent;
# this explicit native gate must fail instead of printing a misleading pass.
export LORE_NATIVE_E2E_REQUIRED=1
"$PYTHON" -m unittest discover -s tests -p 'test_native_*.py' -v
echo 'Native E2E passed: committed raw fixtures -> fresh public-inbox -> full extindex -> backend/MCP.'
