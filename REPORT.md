# Docker deployment report

Verified on 7 October 2026 using local Docker Desktop, Engine 29.5.3,
Compose v5.1.4 and linux/arm64. The demo is running at
`http://127.0.0.1:8765/mcp`. API, gateway and hourly sync are healthy.

## Changes and reasons

- Preserved the Debian base digest, signed package snapshot, public-inbox
  commit and Python versions/hashes. Pinned `pyjwt[crypto]` explicitly:
  Debian's pip otherwise tried resolving MCP's crypto extra outside the lock.
- Added `g++`: the real `public-inbox-clone` path loads XapHelperCxx and failed
  with `no C++ compiler`. The image build and regression suite now load that
  dependency without contacting the network, and the build runs `pip check`.
- Added a fixed TCP gateway using the same image and snapshot-pinned `socat`.
  The draft's internal-only API network produced an empty runtime port mapping
  despite configured HostPort 8765. The gateway publishes only localhost and
  forwards to API; API remains on its isolated network with no external route.
  This follows Docker's [separate ingress/internal network model](https://docs.docker.com/engine/network/).
- Compose has one writable mail mount, belonging to sync. Bootstrap and manual
  jobs invoke that same service, whose coordinator lock excludes another writer.
  API mounts mail read-only and separately owns the issue store. Gateway has
  neither mount. Scratch directories are disposable tmpfs mounts.
- Added daemon healthchecks, restart policies, resource limits, rotating Docker
  and native job logs, configurable host port/config file, and a host bind-mount override.
- Made setup build, test, preflight, reconcile selected mail, start services,
  wait for health and execute a native search. Its Docker path needs no host
  application dependencies. Native setup remains explicitly available.
- Derived the Docker default from `config/pilot.json`, selecting only
  devicetree-spec and u-boot-amlogic with one worker. The original four-list
  native pilot is preserved. An explicit full-manifest config generator is
  available but does not clone mail or enable itself by default.
- Fixed successful unchanged sync cycles failing to refresh `last_success`.
  Generation and last-index time now change only when indexing occurs, while
  successful reconciliation refreshes freshness and clears obsolete errors.
- Added deployment regression tests and a real HTTP MCP smoke client. Updated
  README with first run, sizing, configuration, upgrade, backup/restore,
  client configuration and troubleshooting instructions.

## Commit list

One deployment commit after baseline `face0b0`:

- `lore-mirror: docker: verify persistent compose deployment`

The commit containing this report contains the complete change. Resolve its
hash with `git log --oneline face0b0..HEAD`. Author and committer are Tyler Baker
`<forcedinductionz@gmail.com>`. The brief's specific Signed-off-by and
Assisted-by trailers are used. No push, remote change, post or CI run occurred.
The supplied, initially untracked `BRIEF.md` is preserved outside the commit.

## Build and test evidence

The sandbox could not write the normal buildx activity directory or execute
Docker Desktop's Windows credential helper. Public pulls used these empty,
local temporary client/buildx directories; no credentials were copied:

```sh
export DOCKER_CONFIG=/tmp/lore-mirror-docker
export BUILDX_CONFIG=/tmp/lore-mirror-buildx
docker compose build
scripts/verify.sh --docker
scripts/setup.sh
```

Final verification: **161 tests passed in 20.364 seconds, zero skips**.
All 150 existing tests ran, including native public-inbox fixture import,
indexing, search, identity, semantics and security tests. Eleven deployment
regressions cover defaults, clone dependencies, explicit manifest generation,
invalid selection, freshness, daemon health, log rotation/failure output and setup failure ordering.
The test service uses `network_mode: none`, native tests are mandatory, and
subprocess tests have an executable test-only `/tmp`. Production `/tmp` stays
`noexec`. Existing native setup tests now select native mode explicitly.

`evidence/verification-final.log` contains the build and complete final suite.
`evidence/setup-final.log` records successful setup, health checks and the
native search with `matched_count: 4869`. `pip check` reports:

```text
No broken requirements found.
Ran 161 tests in 20.364s
OK
```

Final image: `lore-mirror:local`, UID/GID `10001:10001`, arm64.

```text
sha256:8f2072ad14d7f76f90335fd2f1ac267602b0a75b04ca1e048b3589968fe67b16
```

`docker image inspect` reports **193,213,118 bytes** of content (184.26 MiB).
Docker Desktop's image listing reports **814 MB disk usage, 193 MB content**.
All three running containers were checked against that same final image ID.
Pinned inputs improve repeatability; bit-for-bit image reproducibility is not
claimed.

## Real archive and HTTP MCP evidence

Only erol's manifest/Git mirror path was used, with one Git worker and
serialized indexing. No lore web search or large-inbox mirror was run.

| Inbox | Indexed messages |
| --- | ---: |
| devicetree-spec | 1,748 |
| u-boot-amlogic | 3,121 |
| Sum of per-inbox memberships | 4,869 |

Real MCP calls used the installed official SDK and TCP HTTP, including a
separate client container connecting through the published localhost port:

```sh
curl --fail http://127.0.0.1:8765/api/health
docker run --rm --network host --entrypoint python lore-mirror:local \
  /app/scripts/smoke-mcp.py
```

The host-network client worked on this Docker Desktop installation. For hosts
without that facility, the checked-in client also runs with
`docker compose exec -T api python /app/scripts/smoke-mcp.py` through real TCP.

Each smoke run passes **26 operations**, covering initialize/tool discovery,
all 11 tools and negative cases:

- Search returns the exact real patch; message retrieval reconstructs nine raw
  chunks and matches the committed fixture's SHA-256.
- Thread returns three messages; series returns nine records; in_flight returns
  two matching patches; was_posted returns exact-ID evidence.
- issue.report, idempotent replay, issue.get and issue.list preserve the same
  local issue across restarts and backup/restore.
- Unknown message, invalid limit, conflicting issue replay and nonexistent
  mail.fetch are rejected. Unknown-ID search returns an empty result.
- Discovery contains exactly the eight mail tools and three local issue tools.
  Only issue.report is writable; all tools declare a closed world.

`evidence/mcp-final.json` is the final successful run. Earlier HTTP evidence
covers initial indexing, published-port access, restart, restore on port 18765,
and the restored copy after a real incremental fetch. The latter port verifies
`LORE_PORT` configuration. No test relies only on in-process HTTP emulation.

## Persistence, incremental sync and recovery

```sh
docker compose down
docker compose up -d --wait
docker compose stop sync
docker compose run --rm --no-deps sync sync --config /config/pilot.json --once
docker compose up -d --wait sync
```

Snapshots of **88 archive/index files** have identical SHA-256 hashes across
image rebuild and Compose down/up. Both message counts, initial indexed times,
Git refs and the issue ID survive. There is no additional clone. A normal
manual cycle completes ready and skips Git/index work for unchanged upstream
fingerprints. Successful freshness advances while generation stays unchanged.

Controlled stale-checkpoint reconciliation was also exercised. Native
public-inbox independently checks actual refs and correctly reports current
repositories up-to-date. To exercise a real fetch, the backup was restored into
a temporary bind mount and its devicetree-spec master ref was set one commit
behind upstream. Only that disposable copy was changed. A normal coordinator
cycle then issued an actual fetch into the existing epoch:

```text
git --git-dir=/data/inboxes/devicetree-spec/git/0.git ... fetch --no-write-fetch-head ...
From https://erol.kernel.org/devicetree-spec/git/0
   4950256..63c32b0  master     -> master
{"status": "ready", "pending_lists": []}
```

The resulting ref matches `63c32b01d4e416e71d991f3ef7206695d96c9dd8`.
The MCP suite passed afterward. This proves incremental recovery/fetch, not
new mail arrival or an elapsed one-hour growth measurement. Exact commands,
setup scripts and output are retained in `evidence/restore-incremental.log`,
`evidence/lag-restored-ref.py` and `evidence/restore-compose.sh`.

Starting another coordinator while the daemon owns the volume fails with:

```text
another sync coordinator already owns this data directory
```

Backups used the README's stopped-service tar commands, preserving mail and
issues separately. Restore into the bind mount produced identical archive/index
hashes and counts; MCP also retrieved the same issue record. The restored stack,
its issue volume, networks and bind directory were removed afterward.
`artifacts/backup/` retains about 75 MiB of compressed backup artifacts, ignored
by Git. Capture/build/test logs stay in ignored `evidence/`.

## Read-only proof and final layout

An attempted API write to `/data/api-write-probe` fails with `EROFS`.
An API TCP connection to `1.1.1.1:443` fails with `Network is unreachable`.
No probe file was created. Container inspection verifies non-root identities,
read-only root filesystems, mount modes, CPU/memory/PID limits and log rotation.
Existing security tests and real tool discovery/calls verify that no tool sends
mail, performs arbitrary URL fetches or exposes sync commands.

| Persistent volume | Mounts | Final allocated size |
| --- | --- | ---: |
| `lore-mirror_mail` | sync `/data` RW; API `/data` RO | 173,432 KiB (169.37 MiB) |
| `lore-mirror_issues` | API `/issues` RW | 20 KiB |

The one retained **demo mail volume is `lore-mirror_mail`**. Its separate
20 KiB issue store remains because the running deployment requires persistent
issue reports. No temporary test/restore volumes remain. Including compressed
backups, retained task mail/issue data is under 0.3 GiB, well below 5 GB.
No unrelated pre-existing Docker resources were removed.

Services left running:

```text
api       healthy, non-root, archive read-only, no external route
gateway   healthy, non-root, 127.0.0.1:8765 -> API
sync      healthy, non-root, one writer, interval_seconds=3600
```

## Limits of verification

- Only linux/arm64 was built/run here; the base digest is multi-architecture.
- Full-manifest config generation is tested against validated offline inventory;
  a full-lore dataset and the large pilot inboxes were deliberately not mirrored.
- The daemon completed live startup cycles and manual cycles. A full elapsed
  hourly interval was not waited out during this task.
- LEI daemon runtime, remote authentication/proxy integration, and long-term
  storage growth remain unverified. The direct public-inbox backend is verified.
- Docker stdout/stderr logs rotate. Native logs rotate between jobs at 10 MiB
  with three previous files retained; a single running job can exceed that
  threshold until completion, subject to the existing free-space guard.
