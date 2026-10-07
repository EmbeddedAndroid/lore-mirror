#!/usr/bin/env bash
# Offline-by-default functional and security checks. Never bootstraps mail.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
if [[ ${1:-} == --docker && $# == 1 ]]; then
  # A Docker-only host must not need the application's Python dependencies.
  command -v docker >/dev/null || { echo 'Docker/Compose build NOT RUN: Docker is not installed.' >&2; exit 1; }
  docker compose version >/dev/null
  docker info >/dev/null
  docker compose config --quiet
  docker compose build
  docker compose --profile test run --rm --no-deps --env LORE_NATIVE_E2E_REQUIRED=1 \
    --entrypoint python test -m unittest discover -s tests -v
  exit 0
elif [[ $# -gt 0 ]]; then
  echo 'Usage: scripts/verify.sh [--docker]' >&2
  exit 2
fi
PYTHON=${PYTHON:-python3}
if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON" -m compileall -q "$ROOT/src"
bash -n "$ROOT/scripts/setup.sh" "$ROOT/scripts/verify.sh"
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m lore_mirror --help >/dev/null
echo 'Python/CLI checks passed; inspect any skipped tests above. Run scripts/test-native-e2e.sh for the mandatory native inbox gate. Docker/Compose build NOT RUN (use --docker on a Docker host).'
