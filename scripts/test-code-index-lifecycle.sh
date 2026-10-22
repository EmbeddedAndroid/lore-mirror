#!/usr/bin/env bash
# Docker-only, isolated fixtures. Never attaches the deployment's data/config.
set -euo pipefail
if [[ $# -gt 1 ]]; then echo 'Usage: scripts/test-code-index-lifecycle.sh [verified-image]' >&2; exit 2; fi
command -v docker >/dev/null || { echo 'Index Docker lifecycle gate NOT RUN: Docker is not installed.' >&2; exit 1; }
IMAGE=${1:-lore-mirror:local}
VOLUME="lore-index-lifecycle-$$-$(date +%s)"
CONTAINER="$VOLUME-fixture"
cleanup() {
  docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Create only this disposable fixture volume; no bind mounts or production data.
docker volume create "$VOLUME" >/dev/null
common=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges
        --user 10001:10001 --pids-limit 64 --memory 128m --memory-swap 128m --cpus 0.5
        --tmpfs /tmp:rw,nosuid,noexec,size=32m,uid=10001,gid=10001,mode=1770
        --volume "$VOLUME:/data:rw")
for mode in supervisor-kill child-kill supervisor-term; do
  case "$mode" in supervisor-kill) expected=137;; child-kill) expected=42;; supervisor-term) expected=143;; esac
  status=0
  docker run "${common[@]}" --name "$CONTAINER" --entrypoint /usr/bin/tini "$IMAGE" \
    -- python /app/scripts/index-lifecycle-fixture.py supervise "/data/$mode" "$mode" || status=$?
  if [[ $status != "$expected" ]]; then echo "$mode: expected exit $expected, got $status" >&2; exit 1; fi
  docker run "${common[@]}" --entrypoint python "$IMAGE" \
    /app/scripts/index-lifecycle-fixture.py verify "/data/$mode" "$mode"
done
echo 'Docker hard-cap/PID-init lifecycle gate passed for isolated fixtures, not production capacity.'
