# Local mailing-list mirror, lean v1

A local-only MCP/HTTP adapter over public-inbox's Git, SQLite and Xapian data. A separate sync process fetches a selected pilot and builds the shared external index. The query service does not fetch remote mail, send email, apply patches or expose synchronization commands. Local issue reporting is the only writable API.

The selected pilot comprises linux-arm-msm, u-boot, devicetree-spec and u-boot-amlogic, with full available history per selected inbox. This is a bounded pilot implementation, not a completed full-lore mirror. Read aggregate freshness in `mail.status` and actual per-list coverage in `mail.list_lists`; an empty result never proves that something was never posted. Authentication is intentionally deferred to an existing trusted private proxy. Keep the default localhost binding unless that proxy is in place.

## Verified pilot outcome, 7 October 2026

The four inboxes are indexed, with 951,304 summed per-inbox Message-ID records after reconciliation (cross-post memberships, not unique messages). Initial Git/basic/shared-index components occupy 17.23 GiB. All 150 native-enabled tests pass, and real HTTP/MCP read and local-issue tests pass. See [measurements and sizing](docs/pilot-measurements.md) and [validation limits](docs/validation.md).

The original executor could not keep its hourly daemon running because of a managed-proxy CONNECT 403. Its last successful four-list reconciliation was at 16:00:20 UTC. The laptop deployment is a separate two-small-inbox Compose stack; see [REPORT.md](REPORT.md) for current build, daemon and HTTP MCP verification. LEI runtime remains unverified; both deployments use the direct public-inbox backend.

## What is included

- Read tools: `mail.search`, `mail.get_message`, `mail.get_thread`, `mail.get_series`, `mail.in_flight`, `mail.was_posted`, `mail.list_lists`, `mail.status`.
- Local issue tools: `issue.report`, `issue.get`, `issue.list`; HTTP `GET`/`POST /api/issues.json`.
- MCP stdio and Streamable HTTP at `/mcp`; direct REST calls at `/api/tools/{tool_name}`; health at `/api/health`.
- Limits on page size, bytes, subprocess output, execution time and retained pagination state. Cursor invalidation makes changed index generations explicit.
- Explicit pilot configuration, hourly sync, at most two workers, 30% free-space reserve and no implicit all-manifest bootstrap.
- Offline fixtures and security tests; pinned public-inbox source, Debian image/snapshot, and fully pinned/hash-checked Python runtime dependencies.

The ordinary query backend is the thin direct public-inbox helper. It uses the same public-inbox/Xapian implementation and query syntax rather than building another search engine. An optional LEI backend is retained for compatible hosts. The build executor cannot create LEI's required `SOCK_SEQPACKET` socket, so LEI operation on that executor is not claimed as verified. The deterministic `fixture` backend supports a deliberately smaller query subset and must not be used to claim native-search compatibility.

## Fresh Linux host: native installation

Start from the delivered source directory. No public repository publication is required. Commands below assume Debian 12 or a compatible Debian/Ubuntu host, Python 3.11+, a normal non-root shell, and `sudo` for system packages. The Docker path below pins the distro package snapshot; these native package commands intentionally use the host's configured distribution repositories.

```sh
cd /path/to/lore-mirror
sudo apt-get update
sudo apt-get install --no-install-recommends \
  ca-certificates git curl make gcc g++ libc6-dev perl python3 python3-venv \
  liburi-perl libdbi-perl libdbd-sqlite3-perl libsearch-xapian-perl \
  libemail-address-xs-perl libsocket-msghdr-perl liblinux-inotify2-perl \
  libnet-server-perl libtimedate-perl libinline-c-perl xapian-tools time

mkdir -p .deps
git init .deps/public-inbox
git -C .deps/public-inbox remote add origin \
  https://kernel.googlesource.com/pub/scm/infra/public-inbox
git -C .deps/public-inbox fetch --depth=1 origin \
  d6b9850df271f3fed85f6d222ec39249d8bc061f
git -C .deps/public-inbox checkout --detach FETCH_HEAD
test "$(git -C .deps/public-inbox rev-parse HEAD)" = \
  d6b9850df271f3fed85f6d222ec39249d8bc061f
(cd .deps/public-inbox && perl Makefile.PL && make -j2 && make test && sudo make install)

scripts/setup.sh --mode native
scripts/verify.sh
```

The setup script installs Python dependencies into `.venv`; it does not clone mail or start a service. `requirements.lock` contains the full transitive version lock and official PyPI wheel hashes. The application is run directly from `src` to avoid an additional, floating build-dependency install.

Create a native configuration with absolute paths before bootstrapping. This copies the approved four-inbox scope (linux-arm-msm, u-boot, devicetree-spec and u-boot-amlogic) and uses the tested direct public-inbox adapter. It stores mail, issue reports and runtime scratch data separately under the checkout; choose a larger mounted data directory instead if the disk preflight requires it.

```sh
python3 scripts/configure-local.py --data-dir "$PWD/data"
```

The helper refuses to overwrite an existing configuration and does not create the data directory, fetch mail or start a service. Use `--output`, `--runtime-dir` and `--issue-dir` to select different local paths.

A normal system installation uses `perl`/`git`/`public-inbox-*` from `PATH`. Both checked-in templates select the tested direct helper. To use LEI on a compatible host, explicitly set `backend` to `lei` and `lei_bin` to its installed executable. Remove executor-specific `query_env`/`sync_env` overrides if copying a measured pilot configuration from another machine.

For a Debian 13 cloud host without administrator access, `scripts/bootstrap-public-inbox.sh "$PWD/.runtime"` provides a separate rootless dependency/source installation. Source `.runtime/env.sh` in the shell running queries or sync. This helper validates Debian package signatures and pins public-inbox source, but uses the host-era Debian package repository rather than the Docker build's frozen package snapshot. Its XS modules must match the host Perl ABI; do not copy that directory between different distro/Perl versions.

```sh
# Explicitly opt in to one bounded pilot bootstrap. This checks 30% headroom.
scripts/setup.sh --mode native --skip-install \
  --config config/local.json --bootstrap-pilot

# Start the read service on localhost. This command never starts a sync.
PYTHONPATH=src .venv/bin/python -m lore_mirror serve \
  --config config/local.json --listen 127.0.0.1 --port 8765

# In a separate terminal/session, enable hourly updates when wanted.
PYTHONPATH=src .venv/bin/python -m lore_mirror sync \
  --config config/local.json --daemon
```

The worker enforces the reserve during synchronization too. Passing the reserve check is not a prediction that the complete mirror fits; configured byte/time budgets and actual pilot measurements still matter. Do not turn a list count or epoch count into a storage estimate.

## Running with Docker Compose

Install Docker Engine or Docker Desktop with Compose v2.20 or newer and Bash.
No host Python, Perl or compiler is needed. The image pins the Debian base by
multi-architecture digest, a signed Debian package snapshot, public-inbox commit
`d6b9850df271f3fed85f6d222ec39249d8bc061f`, and hash-locked Python wheels.
Builds check those inputs; byte-for-byte identical image output is not claimed.
The services run as UID/GID 10001 with read-only root filesystems.

The default mounted `config/container.json` is derived from `config/pilot.json`
and selects only **devicetree-spec and u-boot-amlogic**, with one Git worker.
The original four-list pilot remains in `config/pilot.json` for larger hosts.
The prior [pilot measurements](docs/pilot-measurements.md) measured about
169 MiB for the two small lists' Git/basic/shared indexes and about 393 MiB
peak sampled indexing RSS. Allow additional room for the image, build cache,
temporary work and growth. Keep at least 30% of the data filesystem free.
The full four-list pilot used 17.23 GiB; the documented starting allocation is
2 to 4 CPUs, 8 GiB RAM and 64 GiB storage including illustrative reindex room.
These measurements do not establish full-lore capacity.

### First run

```sh
cd /path/to/lore-mirror
scripts/setup.sh
# Equivalent: scripts/setup.sh --mode docker

docker compose ps
curl --fail http://127.0.0.1:8765/api/health
docker compose logs --tail=100 api sync gateway
```

Setup builds the image, runs the complete offline suite inside it, stops an
existing sync daemon, runs preflight and one sync/index job, starts API, TCP gateway and
hourly sync, waits for their healthchecks, and verifies a native search. Rerunning
setup reconciles the existing archive; it does not discard or re-clone it.
A failed test, preflight or initial sync stops setup before startup. If updating
an existing installation, fix the error and rerun setup to resume the daemon.
Ordinary `docker compose up -d` also starts hourly sync, so inspect the mounted
selection before running it. Only the sync service mounts `/data` writable;
bootstrap is a one-shot invocation of that same service. A filesystem lock
rejects overlapping coordinators. There is no separate index writer.

The API listens at `http://127.0.0.1:8765`, with Streamable HTTP at `/mcp`.
The API's internal Docker network has no external route. It reads local mail
and writes only local issue reports and transient scratch files. A TCP gateway on a separate ingress network publishes the localhost port and
forwards only to the API, without mail or issue mounts. Docker does not publish
ports from an internal-only container, so the gateway provides host access while
the API remains isolated. Sync uses its own upstream network. Services have restart policies, CPU,
memory and PID limits, and three rotating 10 MB Docker logs. Runtime directories
are disposable tmpfs mounts; `/data` and `/issues` use separate named volumes.
Native logs under `/data/logs` rotate before the next job once they reach
10 MiB, retaining three previous files. A single running job can exceed that
threshold; the existing storage guard still preserves disk headroom. These
logs are part of the archive backup.

Put persistent settings in `.env` so every Compose command uses them:

```dotenv
LORE_PORT=8765
LORE_CONFIG=./config/container.json
```

`LORE_CONFIG` must name an existing JSON file using container paths (`/data`,
`/issues`, `/runtime`, `/app`). Missing config files fail instead of becoming
empty directories. `scripts/setup.sh --config PATH` overrides the config for
that invocation; set `.env` for subsequent commands. The localhost port binding
is fixed, while `LORE_PORT` is configurable. Do not publish unauthenticated
access beyond the host; remote access needs your trusted private proxy.

A Streamable HTTP client configuration example:

```json
{
  "mcpServers": {
    "lore-mirror": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

The exact config container key varies by MCP client; use its Streamable HTTP
transport, not SSE or stdio. Run the checked-in real SDK smoke client through
the API container's TCP listener:

```sh
docker compose exec -T api python /app/scripts/smoke-mcp.py
```

### Adding inboxes and full-manifest selection

Edit the JSON selected by `LORE_CONFIG`, adding entries like those in
`config/pilot.json`. Each ID must be an erol manifest namespace, with
`inbox_path` set to `/data/inboxes/ID`. Keep the existing catalog/class metadata
where known. Then run `scripts/setup.sh` to test, reconcile and recreate the
services. Removed selections do not delete old Git data. To reduce a dataset,
create a separate empty volume and keep the old one until recovery is verified.

Full-manifest selection is available as an explicit config-generation command.
It fetches only the erol manifest and materializes every namespace into a JSON
selection shared by API and sync. It never clones mail by itself. It is off by
default and has not been mirrored on this laptop. Run this on a suitably sized
host, inspect the output and storage requirements, then select it with
`LORE_CONFIG=./config/full-manifest.json` in `.env` before setup:

```sh
# Refuse to overwrite an existing selection.
(set -o noclobber; docker compose run --rm --no-deps -T --entrypoint python sync \
  /app/scripts/configure-manifest.py --full-manifest > config/full-manifest.json)
```

Unknown projects are classified `unclassified`; edit their class/address
metadata if needed. New upstream namespaces require regenerating the selection.
Only configured inboxes are admitted. No tool discovers or fetches remote mail.

### Use a large host disk

For a bind mount, create a directory owned by 10001:10001. Do not change ownership
of a disk root or an existing unrelated dataset. Add these settings to `.env`:

```dotenv
COMPOSE_FILE=compose.yaml:compose.bind.yaml
LORE_DATA_DIR=/srv/lore-mirror/mail
```

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /srv/lore-mirror/mail
scripts/setup.sh
```

The override replaces `/data` for both services and keeps the API mount
read-only. Issue reports remain in their separate named volume. Switching this
setting does not migrate old data; restore a backup into the new directory
first if preserving the existing archive. Use a Linux filesystem supporting
file locks, atomic rename and Unix permissions. On Docker Desktop, ensure the
host path is shared and the underlying host disk has capacity too.

### Upgrade and manual sync

Keep the selected config and `.env` alongside your backup. Review source and
pin changes, then run `scripts/setup.sh`; it rebuilds and tests before touching
the running writer. To run verification independently:

```sh
scripts/verify.sh --docker
```

A manual cycle uses the same single writer:

```sh
docker compose stop sync
docker compose run --rm --no-deps sync sync --config /config/pilot.json --once
docker compose up -d --wait sync
```

An unchanged manifest completes without Git/index work. Changed fingerprints
update existing epochs incrementally. Mail reads continue during index updates
and see the last committed index batch: a list being indexed returns what has
been committed so far. A read that meets a commit in progress is retried; if it
still fails, clients receive `index_busy` and should retry. `docker compose down` preserves
named volumes, as do image rebuilds. `docker compose down --volumes` deletes them.

### Backup and restore

With the default project name, persistent volumes are `lore-mirror_mail` and
`lore-mirror_issues`. Compose prefixes them with a different project name when
using `-p` or `COMPOSE_PROJECT_NAME`. Stop both services to take consistent mail
and SQLite issue snapshots. Bootstrap/backup/restore all use the sync service
for any writable mail mount; the backup command itself reads mail only.

```sh
mkdir -p artifacts/backup
docker compose stop api sync
docker compose run --rm --no-deps -T --entrypoint tar sync -C /data -czf - . \
  > artifacts/backup/mail.tar.gz
docker compose run --rm --no-deps -T --entrypoint tar api -C /issues -czf - . \
  > artifacts/backup/issues.tar.gz
docker compose up -d --wait api sync gateway
```

Check command exit statuses and keep copies off this disk. Save the config,
`.env` and image revision with the archives. To restore into **empty** volumes
(or an empty prepared bind directory), use the matching Compose settings:

```sh
docker compose stop api sync
docker compose run --rm --no-deps -T --entrypoint tar sync \
  -C /data -xzf - < artifacts/backup/mail.tar.gz
docker compose run --rm --no-deps -T --entrypoint tar api \
  -C /issues -xzf - < artifacts/backup/issues.tar.gz
scripts/setup.sh --skip-install
```

The non-root image creates named volumes with the correct ownership. Do not
restore on top of a running writer or mix unrelated index generations. Retain
the old image and volume until the restored search and issue calls pass.

### Troubleshooting

- `docker compose ps` and `docker compose logs --tail=100 api sync gateway` show service
  health and cycle summaries. Read `/data/sync-state.json` through
  `docker compose exec -T sync cat /data/sync-state.json` for stage errors, and
  `/data/logs/` for native command logs.
- An unhealthy API can mean no initial index. Run setup and check sync errors.
  HTTP health reports index presence and freshness; use `mail.list_lists` to
  confirm every selected inbox is indexed. A failed sync healthcheck reports a
  dead/unlocked coordinator, failed cycle or overdue schedule. Docker does not
  restart a process solely because its healthcheck fails; the daemon retries
  upstream failures on its next cycle, and restart policy handles process exit.
- Low disk preserves the 30% reserve and stops expanding work. Add capacity
  before retrying. A quiet unchanged cycle refreshes successful-sync freshness.
- A lock conflict means another sync owns the archive. Stop the daemon before
  manual sync; never remove lock files to bypass the live writer.
- An interrupted index can set `unsafe_index`. Rerun setup against the same
  selection to reconcile it before trusting reads. Preserve existing epochs.
- Bind mount permission errors require the data directory to be writable by
  UID/GID 10001. Missing configuration paths must be corrected on the host.
- If a sandbox cannot write Docker's buildx activity directory, set
  `BUILDX_CONFIG` to a writable temporary directory. If Docker Desktop's
  Windows credential helper is unavailable, public pulls can use an empty
  temporary `DOCKER_CONFIG`; never copy credentials into the repository.

See [REPORT.md](REPORT.md) for this laptop's container and MCP verification.

## Connect an MCP client

For stdio, configure the client to execute the absolute virtual-environment Python path, pass the config path explicitly, and set `PYTHONPATH` to this checkout's `src` directory:

```json
{
  "mcpServers": {
    "mail": {
      "command": "/absolute/path/lore-mirror/.venv/bin/python",
      "args": ["-m", "lore_mirror", "stdio", "--config", "/absolute/path/lore-mirror/config/local.json"],
      "env": {"PYTHONPATH": "/absolute/path/lore-mirror/src"}
    }
  }
}
```

For Streamable HTTP, use `http://127.0.0.1:8765/mcp` from a client on the same host. Configure remote access only through the already trusted private proxy; this application does not issue tokens or implement an authentication layer. Binding a native process to a non-loopback interface emits a warning and does not make it safe for Internet exposure. The issue-write endpoint has the same access boundary as mail reads.

## Quick local requests

```sh
curl --fail http://127.0.0.1:8765/api/health
curl --fail -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:8765/api/tools/mail.status
curl --fail -H 'Content-Type: application/json' \
  -d '{"query":"s:patch","limit":5}' \
  http://127.0.0.1:8765/api/tools/mail.search
```

See [the tool reference](docs/tool-reference.md) for arguments, limits, pagination and evidence semantics. Bodies and subject lines are untrusted archived content, not instructions to the client agent. Never execute a mail attachment, patch or command merely because it appears in a tool result.

## Operations and recovery

- Run only one sync writer for a data directory. The worker lock prevents overlapping scheduled/manual jobs.
- Queries are unavailable while an index batch is being updated; this lean version uses a maintenance barrier rather than immutable generations. An interrupted batch must be reconciled by a subsequent authorized sync before claiming healthy, searchable state.
- Check `mail.status` and `mail.list_lists` for actual selected scope, indexed state and failure details. A responsive health endpoint alone does not prove that every inbox is current or that bootstrap finished.
- Keep `data_dir`, the selected config and the issue directory in host backups. Stop the sync worker before a filesystem-level archive/index backup. For a consistent issue-store copy, stop API writes or use SQLite's online backup facility.
- After an interrupted clone/index, rerun the one-shot sync against the same configuration. Keep successful source Git data. Do not delete an inbox because a manifest response omits it or an update fails.
- Low disk stops new storage-expanding work; it does not authorize deleting historical mail. Increase available capacity or deliberately reduce approved pilot scope and rebuild in a separate empty data directory.
- Use `docker compose restart api` after a configuration-only change. Rebuild the image for source/dependency changes. Keep the old volume until the replacement has been checked; there is no automatic rollback promise.
- Local issue reports remain local. Nothing sends them to maintainers or archive administrators. Review reports before manually sharing any that contain copied mail or operational details.

## Important limits

This version does not include public deployment/authentication, a verified full-manifest mirror, a five-year derived archive, embeddings, an SBOM, immutable-generation rollback, private mailing-list access or administrator outreach. It does not infer a confirmed upstream merge from matching subject/author or review trailers. Series grouping and evidence searches report ambiguity and mirrored-scope limits. Full-corpus capacity and completion time remain unestablished until broader authorized measurements exist.

Upstream components retain their own licenses. public-inbox source and notices are retained in the container at `/opt/public-inbox`; inspect applicable upstream licensing before redistributing or publicly deploying this bundle.

## Upstream references

- [public-inbox pinned source](https://kernel.googlesource.com/pub/scm/infra/public-inbox/+/d6b9850df271f3fed85f6d222ec39249d8bc061f/)
- [public-inbox installation instructions](https://kernel.googlesource.com/pub/scm/infra/public-inbox/+/d6b9850df271f3fed85f6d222ec39249d8bc061f/INSTALL)
- [LEI query manual](https://public-inbox.org/lei-q.html)
- [public-inbox external index manual](https://public-inbox.org/public-inbox-extindex.html)
- [MCP Python SDK 1.29.0](https://pypi.org/project/mcp/1.29.0/)
- [liblore 0.10.1](https://pypi.org/project/liblore/0.10.1/)
- [Debian snapshot service](https://snapshot.debian.org/)

Pinned Linux and U-Boot root MAINTAINERS catalogs are included under `config/maintainers`, with commit/hash provenance and upstream notices. The inbox class selects the project catalog. They are inert snapshots, never refreshed on a query; U-Boot board-local catalogs and complex N/K regex classification are not exhaustive.

## Optional local code mirrors

An opt-in code catalog adds bounded, exact-revision `code.search`,
`code.get_file`, `code.log`, and `code.show`, plus catalog/status tools. Mail-only
configurations keep their existing MCP discovery unchanged. These tools never
fetch or execute repository content; there is no `code.blame`. Mirrors use the mail index's shared HDD I/O lock and preserve last-good
published snapshots on failures.

Start with a U-Boot-only catalog on the existing host and measure before adding
one Linux ref. The example is disabled and is not a claim that all requested
upstream refs currently exist. Read [catalog and upstream caveats](docs/code-catalog.md),
[mirror coordination and resource limits](docs/code-sync.md), and
[exact-Git tools and operator workflow](docs/code-tools.md)
before enabling it.
