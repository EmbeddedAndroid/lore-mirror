# Local mailing-list mirror, lean v1

A local-only MCP/HTTP adapter over public-inbox's Git, SQLite and Xapian data. A separate sync process fetches a selected pilot and builds the shared external index. The query service does not fetch remote mail, send email, apply patches or expose synchronization commands. Local issue reporting is the only writable API.

The selected pilot comprises linux-arm-msm, u-boot, devicetree-spec and u-boot-amlogic, with full available history per selected inbox. This is a bounded pilot implementation, not a completed full-lore mirror. Read aggregate freshness in `mail.status` and actual per-list coverage in `mail.list_lists`; an empty result never proves that something was never posted. Authentication is intentionally deferred to an existing trusted private proxy. Keep the default localhost binding unless that proxy is in place.

## Verified pilot outcome, 7 October 2026

The four inboxes are indexed, with 951,304 summed per-inbox Message-ID records after reconciliation (cross-post memberships, not unique messages). Initial Git/basic/shared-index components occupy 17.23 GiB. All 150 native-enabled tests pass, and real HTTP/MCP read and local-issue tests pass. See [measurements and sizing](docs/pilot-measurements.md) and [validation limits](docs/validation.md).

The hourly daemon is implemented, but its first live cycle failed with a managed-proxy CONNECT 403. Its process handle is now unavailable and no running coordinator or scheduled future run is verified. The last successful four-list reconciliation, at 16:00:20 UTC, remains read-safe. Start hourly sync on the chosen host using the commands below; do not assume this workspace is an active persistent service. Docker/Compose execution and LEI runtime are also unverified here.

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
  ca-certificates git curl make gcc libc6-dev perl python3 python3-venv \
  liburi-perl libdbi-perl libdbd-sqlite3-perl libsearch-xapian-perl \
  libemail-address-xs-perl libsocket-msghdr-perl liblinux-inotify2-perl \
  libnet-server-perl libtimedate-perl libinline-c-perl xapian-tools time

mkdir -p .deps
git init .deps/public-inbox
git -C .deps/public-inbox remote add origin \
  https://kernel.googlesource.com/pub/scm/infra/public-inbox
git -C .deps/public-inbox fetch --depth=1 origin \
  94b6bbb95cb88a6de59458b47869c74fc0c901ad
git -C .deps/public-inbox checkout --detach FETCH_HEAD
test "$(git -C .deps/public-inbox rev-parse HEAD)" = \
  94b6bbb95cb88a6de59458b47869c74fc0c901ad
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

## Docker/Compose installation

Install Docker Engine and Compose from the [official Docker instructions](https://docs.docker.com/engine/install/) for the host first. The checked-in image uses non-root UID/GID 10001. Query and sync are separate services with separate scratch directories. The query service has a read-only archive mount, an internal-only Docker network, and writable issue/runtime volumes. The published port binds to `127.0.0.1`, even though the process listens on all interfaces inside its container.

```sh
cd /path/to/lore-mirror
scripts/setup.sh --mode docker
scripts/verify.sh --docker

# This is the only setup command that fetches the configured pilot mail.
scripts/setup.sh --mode docker --skip-install --bootstrap-pilot
docker compose up -d api
curl --fail http://127.0.0.1:8765/api/health

# Optional ongoing hourly updates; sync is an opt-in Compose profile.
docker compose --profile sync up -d sync
docker compose logs --tail=100 api sync
```

Use `config/container.json` for Compose. Mount paths in that file are container paths (`/data`, `/issues`, `/runtime`), not host paths. Named volumes preserve mail and reports across container replacement. `docker compose down` stops services without deleting named volumes; do not add `--volumes` unless you intend to delete those datasets.

Build inputs are pinned to public-inbox `94b6bbb95cb88a6de59458b47869c74fc0c901ad`, Debian's official `bookworm-slim` image digest, a timestamped signed Debian package snapshot, and the Python lock. This improves repeatability; it is not a claim of independently verified bit-for-bit reproducible image output. Refresh the base digest, Debian snapshot and Python lock together when applying reviewed security updates. No SBOM or generation rollback system is included in this lean scope.

Docker is not installed in the current build executor. Native execution and tests are the local validation path; the Docker image build, Compose startup and container-level network/mount enforcement must be verified on a Docker host. Do not treat a checked-in Compose file or YAML parse as a successful container run.

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

This version does not include public deployment/authentication, full-manifest bootstrap, a five-year derived archive, embeddings, an SBOM, immutable-generation rollback, private mailing-list access or administrator outreach. It does not infer a confirmed upstream merge from matching subject/author or review trailers. Series grouping and evidence searches report ambiguity and mirrored-scope limits. Full-corpus capacity and completion time remain unestablished until broader authorized measurements exist.

Upstream components retain their own licenses. public-inbox source and notices are retained in the container at `/opt/public-inbox`; inspect applicable upstream licensing before redistributing or publicly deploying this bundle.

## Upstream references

- [public-inbox pinned source](https://kernel.googlesource.com/pub/scm/infra/public-inbox/+/94b6bbb95cb88a6de59458b47869c74fc0c901ad/)
- [public-inbox installation instructions](https://kernel.googlesource.com/pub/scm/infra/public-inbox/+/94b6bbb95cb88a6de59458b47869c74fc0c901ad/INSTALL)
- [LEI query manual](https://public-inbox.org/lei-q.html)
- [public-inbox external index manual](https://public-inbox.org/public-inbox-extindex.html)
- [MCP Python SDK 1.29.0](https://pypi.org/project/mcp/1.29.0/)
- [liblore 0.10.1](https://pypi.org/project/liblore/0.10.1/)
- [Debian snapshot service](https://snapshot.debian.org/)

Pinned Linux and U-Boot root MAINTAINERS catalogs are included under `config/maintainers`, with commit/hash provenance and upstream notices. The inbox class selects the project catalog. They are inert snapshots, never refreshed on a query; U-Boot board-local catalogs and complex N/K regex classification are not exhaustive.
