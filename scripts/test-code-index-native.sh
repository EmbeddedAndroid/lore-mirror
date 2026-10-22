#!/usr/bin/env bash
# Tiny offline pinned-engine checks; never a production capacity qualification.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
INDEXER=${LORE_ZOEKT_INDEXER:-/usr/local/bin/zoekt-git-index}
BRIDGE=${LORE_ZOEKT_BRIDGE:-/usr/local/bin/lore-zoekt}
CTAGS=${LORE_CTAGS:-/usr/bin/ctags-universal}
for binary in "$INDEXER" "$BRIDGE" "$CTAGS"; do
  [[ -x "$binary" ]] || { echo "Native code-index check NOT RUN: missing $binary" >&2; exit 1; }
done
export LORE_ZOEKT_INDEXER="$INDEXER" LORE_ZOEKT_BRIDGE="$BRIDGE" LORE_NATIVE_E2E_REQUIRED=1
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
${PYTHON:-python3} -m unittest tests.test_code_search.NativeZoektReaderTests -v
${PYTHON:-python3} "$ROOT/tools/zoekt/integration_test.py" "$INDEXER" "$BRIDGE" "$CTAGS"
