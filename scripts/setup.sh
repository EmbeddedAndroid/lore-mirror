#!/usr/bin/env bash
# Docker mode builds, tests, reconciles the configured mail and starts services.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODE=docker
CONFIG="$ROOT/config/pilot.json"
CONFIG_SET=0
BOOTSTRAP=0
INSTALL=1
usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [--mode native|docker] [--config PATH]
                        [--skip-install] [--bootstrap-pilot]
Default: Docker build, offline tests, configured mail bootstrap, hourly sync and API.
Native mode expects Git, Perl, Xapian and public-inbox already installed.
Docker mode uses LORE_CONFIG (default config/container.json) or --config PATH.
Native mode only installs unless --bootstrap-pilot explicitly requests one sync.
--skip-install skips building/installing, but Docker mode still tests and starts.
EOF
}
while (($#)); do
  case "$1" in
    --mode) MODE=${2:?Missing mode}; shift 2 ;;
    --config) CONFIG=${2:?Missing config path}; CONFIG_SET=1; shift 2 ;;
    --bootstrap-pilot) BOOTSTRAP=1; shift ;;
    --skip-install) INSTALL=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
cd "$ROOT"
PREFLIGHT=$(cat <<'PY'
import json, pathlib, shutil, sys
p=pathlib.Path(sys.argv[1]).resolve()
with p.open() as f: c=json.load(f)
lists=c.get('lists', [])
maximum=int(sys.argv[2])
if not 1 <= len(lists) <= maximum:
    raise SystemExit(f'Refusing bootstrap: explicitly select 1 to {maximum} inboxes.')
ids=[x.get('id') for x in lists if isinstance(x,dict)]
if len(ids)!=len(lists) or len(set(ids))!=len(ids) or any(not x or '/' in x or x in ('.','..') for x in ids):
    raise SystemExit('Refusing bootstrap: invalid or duplicate selected list IDs.')
if c.get('backend') == 'fixture':
    raise SystemExit('Use fixture-import for fixtures; bootstrap requires a public-inbox backend.')
target=pathlib.Path(c['data_dir']).expanduser().resolve()
target.mkdir(parents=True,exist_ok=True)
d=shutil.disk_usage(target)
reserve=max(0.30,float(c.get('min_free_fraction',0.30)))
if not 0.30 <= reserve < 1:
    raise SystemExit('Invalid min_free_fraction: must be at least 0.30 and below 1.')
print('Selected lists: '+', '.join(ids))
print(f'Data filesystem: {target}; free {d.free/2**30:.2f} GiB / {d.total/2**30:.2f} GiB ({d.free/d.total:.1%})')
if d.free/d.total < reserve:
    raise SystemExit(f'Refusing bootstrap: less than {reserve:.0%} free disk headroom.')
print('Disk gate passed. This is a reserve check, not a full-history capacity estimate.')
PY
)
case "$MODE" in
  native)
    PYTHON=${PYTHON:-python3}
    "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
    if ((BOOTSTRAP)); then "$PYTHON" -c "$PREFLIGHT" "$CONFIG" 6; fi
    if ((INSTALL)); then
      "$PYTHON" -m venv "$ROOT/.venv"
      "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check \
        --only-binary=:all: --require-hashes -r "$ROOT/requirements.lock"
    fi
    if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; fi
    export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PYTHON" -c 'import importlib.metadata as m; import mcp, lore_mirror; assert m.version("liblore") == "0.10.1"'
    if ((BOOTSTRAP)); then
      "$PYTHON" -m lore_mirror sync --config "$CONFIG" --once
    fi
    echo "Runtime ready. Run: PYTHONPATH=src $PYTHON -m lore_mirror serve --config $CONFIG"
    ;;
  docker)
    command -v docker >/dev/null || { echo 'Docker is not installed.' >&2; exit 1; }
    docker compose version >/dev/null
    docker info >/dev/null
    if ((CONFIG_SET)); then export LORE_CONFIG="$CONFIG"; fi
    docker compose config --quiet
    if ((INSTALL)); then docker compose build; fi
    docker compose --profile test run --rm --no-deps test
    docker compose stop sync
    docker compose run --rm --no-deps --entrypoint python sync -c "$PREFLIGHT" /config/pilot.json 1000
    docker compose run --rm --no-deps sync sync --config /config/pilot.json --once
    docker compose up -d --wait --wait-timeout 180 api sync gateway
    docker compose exec -T api python -m lore_mirror call --config /config/pilot.json \
      mail.search '{"query":"","limit":1}'
    echo 'Stack ready. Streamable HTTP uses /mcp at this published address:'
    docker compose port gateway 8765
    ;;
  *) echo '--mode must be native or docker' >&2; exit 2 ;;
esac
