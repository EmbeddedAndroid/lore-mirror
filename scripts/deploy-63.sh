#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$PWD" == /srv/lore-mirror && "$(uname -m)" == aarch64 ]] || {
  echo 'Run only from /srv/lore-mirror on the arm64 deployment host.' >&2; exit 2;
}
mkdir -p evidence/deploy-63
[[ -d /srv/lore-mirror-data ]] || install -d -o 10001 -g 10001 -m 0750 /srv/lore-mirror-data
if docker ps --format '{{.Names}}' | grep -q '^lore-mirror-sync-'; then
  echo 'Stop this stack before rerunning the build and test deployment.' >&2; exit 2
fi
fingerprint="$(cat SOURCE_COMMIT)-$(date -u +%Y%m%dT%H%M%S)-$(cat /proc/sys/kernel/random/uuid)"
cat > .env <<ENV
COMPOSE_FILE=compose.yaml:compose.bind.yaml:compose.deploy-63.yaml
LORE_DATA_DIR=/srv/lore-mirror-data
LORE_CONFIG=./config/full-manifest.json
LORE_PORT=8765
LORE_BUILD_FINGERPRINT=$fingerprint
ENV
export LORE_BUILD_FINGERPRINT="$fingerprint"
docker compose config --quiet
docker compose build api
docker compose --profile test run --rm --no-deps test
if [[ ! -f config/full-manifest.json ]]; then
  docker run --rm --memory=256m --memory-swap=256m --entrypoint python lore-mirror:deploy-63 \
    /app/scripts/configure-manifest.py --full-manifest --template /app/config/deploy-63.json \
    > config/full-manifest.json.tmp
  mv config/full-manifest.json.tmp config/full-manifest.json
fi
docker compose run --rm --no-deps -T --entrypoint python sync -c '
import json, shutil
from lore_mirror.sync import prepare_config
c=prepare_config("/config/pilot.json")
u=shutil.disk_usage(c["data_dir"])
assert u.free/u.total >= c["min_free_fraction"] and u.free > 100*1024**3
print(json.dumps({"inboxes":len(c["lists"]),"free_bytes":u.free,"total_bytes":u.total}))
'
docker compose up -d api sync gateway
for service in api sync gateway; do
  id=$(docker compose ps -q "$service")
  docker inspect --format '{{.Image}} {{range .Config.Env}}{{println .}}{{end}}' "$id" \
    | grep -E '^sha256:|^LORE_BUILD_FINGERPRINT='
done
