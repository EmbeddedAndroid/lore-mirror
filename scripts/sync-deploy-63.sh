#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p artifacts/deploy-63
git diff --exit-code HEAD -- . ':!HANDOFF.md' ':!REPORT-DEPLOY-63.md'
git ls-files -z > artifacts/deploy-63/source-files
# Only committed source is transferred; generated host config and data survive.
git rev-parse HEAD > artifacts/deploy-63/SOURCE_COMMIT
docker build -f scripts/Dockerfile.transport -t lore-mirror:transport .
docker run --rm --mount "type=bind,src=$PWD,dst=/source,readonly" \
  --mount "type=bind,src=$HOME/.ssh/id_rsa,dst=/key,readonly" \
  --mount "type=bind,src=$HOME/.ssh/known_hosts,dst=/known_hosts,readonly" \
  lore-mirror:transport -az --from0 --files-from=/source/artifacts/deploy-63/source-files \
  -e 'ssh -i /key -o BatchMode=yes -o UserKnownHostsFile=/known_hosts -o StrictHostKeyChecking=yes' \
  /source/ root@<host>:/srv/lore-mirror/
ssh -F /dev/null -o BatchMode=yes root@<host> \
  'cat > /srv/lore-mirror/SOURCE_COMMIT' < artifacts/deploy-63/SOURCE_COMMIT
