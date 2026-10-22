# Opt-in, separately capped index pilot

Phase 4 adds a separate **one-shot process on the existing `.63` host**, not a
new machine or worker host. It does not require an SSD or RAM upgrade. It stays
disabled unless explicitly configured and invoked; normal mail synchronization
and code reads must never start an index build. No index/search network port is
exposed. No `code.blame` is added. Mail/code evidence links are a later, independent phase.

## Scope and configuration

The `code.index` object is independently disabled by default. Merely supplying
`index_refs` in the tree catalog does not enable any indexing. There is exactly
one configured pilot tree/ref per run, not an implicit loop over every catalog
entry. No CLI tree/ref override expands that selection.

Example fragment inside an existing `code` object, after configuring the
`u-boot` tree and adding `refs/heads/master` to that tree's `index_refs`:

```json
"index": {
  "enabled": true,
  "symbols": false,
  "pilot_tree": "u-boot",
  "pilot_ref": "refs/heads/master",
  "memory_bytes": 536870912,
  "timeout_seconds": 1800,
  "max_index_bytes": 2147483648,
  "max_source_bytes": 1073741824
}
```

`code.enabled` must also be true. The local mirror must already contain the
published requested ref and objects. Indexing is local-only and cannot repair
a missing mirror by fetching from upstream.

| Field | Default | Allowed range / meaning |
| --- | --- | --- |
| `enabled` | false | Boolean; true requires `code.enabled` and a pilot pair. |
| `symbols` | false | Boolean; opt-in Universal Ctags lexical symbol extraction. Changing it changes index policy and requires a rebuild. |
| `pilot_tree` | null | A configured tree ID, paired with `pilot_ref`. |
| `pilot_ref` | null | Exact full ref in that tree's `index_refs`; no wildcard, raw OID, abbreviated name, or revision expression. |
| `memory_bytes` | 512 MiB | Positive integer, maximum 768 MiB. This is an admission ceiling, not creation of a cgroup. |
| `timeout_seconds` | 1800 | Positive integer, maximum 7200. |
| `max_index_bytes` | 2 GiB | Positive integer, maximum 8 GiB. |
| `max_source_bytes` | 1 GiB | Positive integer, maximum 4 GiB. |

Unknown keys, booleans used as numbers, fractions, NaN/infinity, and invalid
selections fail validation even when the index is disabled. A disabled index
may retain a valid pilot pair for later review, or leave both values null.
Retained historical tags are not automatically index targets. The linux-next
catalog's current `master` target does not authorize indexing every `next-*` tag.

## Hard resource boundary

`compose.code-index.yaml` adds only the `code-index` service, behind the
`code-index` profile. Its defaults are:

- `mem_limit: 512m` and `memswap_limit: 512m`, so the container has no swap budget.
- `cpus: 0.5`, a non-root user, dropped capabilities, and no new privileges.
- `network_mode: none`; no upstream fetch, credential use, or download at run time.
- Read-only container filesystem, small bounded scratch tmpfs mounts, and the
  existing `mail` volume writable at `/data` for index outputs and locks.
- The same read-only configuration file as the other services, with no implicit
  creation of a missing host file. No restart loop and no API healthcheck.

The runtime coordinator must inspect its **actual cgroup hard memory limit**
before launching the builder and fail closed if the limit is unavailable,
unbounded, or larger than the configured `memory_bytes`. A configuration value,
sampled RSS, or the presence of a container environment variable is not proof
that a hard cap exists. A direct uncapped host invocation is not an alternative.
The configured cap must remain within the validated 768 MiB maximum; the
provided Compose pilot stays at 512 MiB.

Docker documents [hard memory limits and swap behavior](https://docs.docker.com/engine/containers/resource_constraints/)
and the [Compose resource/security fields](https://docs.docker.com/reference/compose-file/services/).
The host must actually support cgroup v2 enforcement. A finite `memory.max`
within the configured budget and `memory.swap.max=0` are mandatory in both
supported whole-workload teardown modes:

- An already enforced `memory.oom.group=1`, read without changing it; or
- A dedicated PID namespace whose PID 1 is the image's Debian-snapshot
  `/usr/bin/tini`, with the index coordinator as its direct child. This works
  with Docker's ordinary `memory.oom.group=0` and read-only cgroup mount.

The index overlay uses an explicit exec-form Tini entrypoint and `init: false`
so Docker does not insert another init wrapper. Admission verifies the live PID 1
executable against the image Tini inode, exact parent/child argv, direct parent
PID, namespace identity and identical cgroup membership. Merely naming a process
`tini`, setting an environment variable, launching through a shell, `docker exec`,
or sharing the host/another container's PID namespace is not accepted. Do not
add `pid: host`, `pid: service:...`, `--init`, or a shell wrapper to this service.

If the coordinator dies, Tini exits; Linux then SIGKILLs every process remaining
in that PID namespace, including descendants that left the indexer's process
group. If the indexer dies first, the bounded runner promptly kills its group,
drains only bounded buffered output, reports the failure and lets the coordinator
exit. A surviving pipe holder cannot keep the coordinator alive until the full
build timeout. Inherited writer locks remain held until their holders die.
[Linux PID namespace init semantics](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)
provide the final cleanup boundary, not a sampled RSS estimate.

A v1 controller, unavailable/unbounded cap, swap allowance, or unverified teardown
mode fails closed. No code writes cgroup files, changes host security settings,
disables the OOM killer, or relies on swap to make a failed pilot fit. The Docker
hard-cap/lifecycle gate below still must run on the target runtime before a pilot.

## Capacity on the current host

Use the existing `.63` deployment overlay, **not `compose.yaml` alone**. The base
file's sync limit is 4 GiB; that is not evidence a sync process fits a 3891 MiB
host, and it is not the reviewed `.63` limit.

Layered `.63` caps are sync 2304 MiB, API 512 MiB, gateway 64 MiB, and this pilot
512 MiB: **3392 MiB in aggregate**. Caps are neither reservations nor measured
consumption. Subtracting them from 3891 MiB leaves only 499 MiB on paper, before
host services and other demands; it does not establish safe headroom. The shared
I/O lock does not eliminate memory already held by another process.

Before the first run, measure actual host/service memory and cgroup peaks during
representative mail traffic. Check free storage on the filesystem holding
`/data`, including source staging, new index output, retained last-good output,
and fetch/recovery slack. Preserve the existing minimum **30% free-space** rule.
Start with one U-Boot ref. A Linux pilot is a later explicitly reviewed run, not
a reason to silently raise caps or run all trees. A budget failure is a useful
result: preserve mail service and investigate rather than retrying without bounds.

## Image and one-shot invocation

The overlay selects `${LORE_CODE_INDEX_IMAGE:-lore-mirror:deploy-63}`. It has no
build directive and does not install an engine at startup. **Build and verify the
selected runtime image before invoking it.** An old phase-1-3 image does not gain
the phase-4 CLI or pinned index tooling merely because this overlay exists.
The updated multi-stage Dockerfile builds the bridge and indexer from Zoekt
commit `e54cd91d1a64864fda7e681c9d31171eaab09ca7` using checksum-verified Go 1.26.5
and the checked-in module checksums. It embeds their SHA-256 values in
`/usr/local/share/lore-mirror/zoekt-build.json`. Build-time dependency downloads
need network access; the running index service does not. Optional symbol
extraction uses Universal Ctags from the existing pinned Debian package snapshot,
not a runtime install. Missing or mismatched
tooling must fail closed. `LORE_CODE_INDEX_IMAGE`, if set, must identify the
verified image, not an arbitrary unpinned binary bundle.

Use the same reviewed config for all services, with `data_dir: "/data"` and
`code.root_dir` under `/data`. Set `LORE_CONFIG` to its absolute host path and
`LORE_BUILD_FINGERPRINT` for the existing deployment overlay. Then validate all
three Compose files together on a Docker-capable host for the **named-volume**
deployment (see the bind-backed variant below):

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml \
  -f compose.code-index.yaml --profile code-index config --quiet
```

Rebuild the default `lore-mirror:deploy-63` image with the updated Dockerfile:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml \
  -f compose.code-index.yaml build api
```

The build itself is not constrained by the runtime index service's 512 MiB cap;
review build-resource headroom separately. Building does not deploy or start the
services. If using a different `LORE_CODE_INDEX_IMAGE`, build and verify that
image through its approved procedure instead of assuming the default build
updates it.

Before a build, inspect the actual runtime enforcement without creating index
artifacts. Use the complete bind-backed layering below instead if applicable:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml \
  -f compose.code-index.yaml --profile code-index run --rm --no-deps \
  code-index code-index --config /config/pilot.json --preflight
```

A failure is an operator setup prerequisite, not permission to remove the gate.
A successful preflight proves the inspected hard controls, not available host
headroom or pilot capacity.

After building/verifying the image and establishing actual runtime headroom,
use an approved maintenance window. **Gracefully stop the existing sync daemon
before the index run**, including its mail and code-sync work. Wait for its
container to be stopped and outstanding writer children/`index.lock` to clear.
Do not use `docker pause`: freezing the process retains its memory and may retain
the shared lock. Do not kill an in-flight sync merely to make the pilot start.

For a named-volume deployment, the explicit operator sequence is:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml stop sync
docker compose -f compose.yaml -f compose.deploy-63.yaml ps --all sync
```

Check that sync is stopped, not restarting or still stopping. Keep the matching
bind overlay on both commands for a bind-backed deployment. Never run the pilot
alongside a manually launched sync or links backfill. The shared lock is a final
exclusion guard, not a mail-priority scheduler or permission to leave the daemon
running. Mail freshness intentionally pauses for this maintenance window; no
five-minute freshness guarantee is implied.

Then run one selected snapshot without starting dependencies:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml \
  -f compose.code-index.yaml --profile code-index run --rm --no-deps code-index
```

After inspecting the result, **resume the existing sync service on every outcome,
including failed or interrupted index runs**. Use the same deployment layering:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml start sync
docker compose -f compose.yaml -f compose.deploy-63.yaml ps sync
```

Verify sync is running/healthy and mail freshness advances again; if it does not,
stop further pilot attempts and follow the deployment's recovery procedure.
These are a manual operator sequence, not an unattended shell script: if a run
fails, do not let shell `set -e` skip the resume step.

This is an operator procedure, not authorization to deploy or run it in
production. Inspect the result before any second run. The overlay does not
change mail service caps, start an index daemon, or schedule recurring work.

## Bind-backed deployment: use the matching index overlay

If the existing deployment uses `compose.bind.yaml`, its API and sync services
mount the host directory named by `LORE_DATA_DIR` rather than the named `mail`
volume. **Do not combine that deployment with the named-volume index overlay
alone.** Doing so puts the builder's `/data` and `index.lock` on a different
filesystem, breaking shared-lock coordination and publishing to the wrong store.

Set `LORE_DATA_DIR` to the same existing absolute host directory used by API and
sync. Add `compose.code-index.bind.yaml` **last**, after both
`compose.bind.yaml` and `compose.code-index.yaml`. The override replaces only
the index service's `/data` mount, preserves its read-only config mount, and
sets `create_host_path: false`. [Compose merges volume entries by container
target](https://docs.docker.com/reference/compose-file/merge/#unique-resources),
so this replaces `/data` without removing the separate config mount. All three
services then share the exact bind source; API remains read-only and sync/index
remain writable.

Validate and run the bind-backed variant with all five files. The same approved
sync stop/idle-check/resume/health-check procedure above is mandatory, retaining
`-f compose.bind.yaml` when operating the existing sync service:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml \
  -f compose.bind.yaml -f compose.code-index.yaml \
  -f compose.code-index.bind.yaml --profile code-index config --quiet

docker compose -f compose.yaml -f compose.deploy-63.yaml \
  -f compose.bind.yaml -f compose.code-index.yaml \
  -f compose.code-index.bind.yaml --profile code-index run --rm --no-deps code-index
```

Use the same five-file order for `build api` when building for that deployment.
Inspect the merged configuration before the first pilot: the `api`, `sync`, and
`code-index` volume whose target is `/data` must all resolve to exactly the same
source and mount type. Mixing named-volume and bind-backed storage is invalid.
The small bind override is not a standalone service definition; do not use it
without the code-index overlay. Existing `compose.bind.yaml` is unchanged and
does not create an index service when indexing is not requested.

## Contention, exactness and failure

The builder uses the existing `/data/index.lock` shared with mail indexing and
other code writers. New code readers probe it nonblocking and immediately
release it; Git and Zoekt reads never hold or inherit that lock. A fresh code
query reports busy if it starts during a build. Already active readers do not
delay the builder or mail indexing; they use immutable published generations
and reject a publication change before returning. Cached cursor pages do not
probe the lock and remain usable while their captured generation is current.
Retaining a last-good index does not bypass fresh-query admission.

New output is built separately from the published generation, validated against
the exact requested Git snapshot, and published only when complete. Failures
must not overwrite the last-good publication. A new commit on the upstream ref
does not silently relabel an old index. An unavailable or nonmatching index
must be reported or use the bounded exact-Git fallback, never search a different
revision under the requested name. Indexing a current tip does not mean searching
all historical versions.

The initial indexed path is literal, case-sensitive search. Regex and
case-insensitive requests retain the explicit exact-Git fallback and existing
Git regex dialect; no unsupported cross-engine regex equivalence is claimed.
When an indexed query has more matching lines than `limit`, the page is the
engine's highest-ranked subset sorted by path, marked
`truncated_selection: "engine_ranked"`; it is not the path-order prefix the
Git fallback returns, so the first page of a truncated query can differ
between the two engines.

`symbols: true` opts into **lexical symbol extraction with Universal Ctags**.
It is not compiler-backed semantic lookup, type analysis, call-graph navigation,
or proof of a symbol's meaning. Changing this setting changes index policy and
requires rebuilding the selected snapshot before the new capability is advertised.
A symbol query against an index without verified symbol coverage, with missing
tooling, or without a compatible indexed snapshot must return an explicit
unavailable/unsupported error. It must **never silently fall back to full-text
search** and label those text matches as symbol results. The default remains
false, so ordinary pilot builds do not incur symbol extraction unless requested.

## Verification and pilot record

Offline catalog tests cover default-off behavior, strict limits, paired scope,
no unconfigured refs, and the overlay's security/resource contract. Run:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_code_config.py' -v
```

Static YAML checks are not Docker/Compose validation or a real cgroup/OOM test.
The development environment has no Docker CLI, so image builds, merged Compose
validation, live Docker cgroup enforcement, and production capacity remain
separate operator gates. No production pilot was run.

Real native PID-namespace lifecycle tests do run without Docker. With an official
Tini executable available, the following creates isolated user/PID namespaces
without host cgroup or security modifications:

```sh
LORE_TINI=/usr/bin/tini LORE_INDEX_LIFECYCLE_REQUIRED=1 PYTHONPATH=src \
  python -m unittest tests.test_code_index_lifecycle -v
```

They use real Tini and subprocesses, inherited flock, and a pipe-holding grandchild
that deliberately escapes its process group. Coordinator SIGKILL, PID-1 SIGTERM forwarded by Tini to the coordinator, and
indexer SIGKILL each must stop that descendant and release the lock;
indexer death must return promptly as a failure. Only cgroup admission is replaced
in this native fixture, because unshare creates no hard memory cap. This is proof
of the tested lifecycle behavior, **not** a Docker memory/OOM test.

On a Docker host, run the actual image hard-cap/lifecycle gate:

```sh
scripts/test-code-index-lifecycle.sh lore-mirror:local
```

This uses only a disposable fixture volume, no production config/data, 128 MiB
hard memory/no-swap caps, network disabled, dropped capabilities and the image's
Tini. Admission must select the PID-init path with the actual controller values.
Each failure case is followed by a separate verification container checking the
shared lock and stopped heartbeat. The script cleans up only its own fixture
container and volume, and never writes cgroup files. It is also part of
`scripts/verify.sh --docker`; it must not be reported as passed when Docker is
unavailable. These are simulated SIGKILL/OOM-victim lifecycle events, not an
allocation-stress test or a production capacity qualification.

For the first authorized pilot, record tree/ref/commit, engine identity, selected
source bytes, index bytes, elapsed time, cgroup peak, outcome, disk headroom, and
mail freshness/latency before and during the run. Distinguish a hard-cap or
timeout failure from a completed usable index; neither fixture success nor the
aggregate cap calculation is a capacity measurement.

## Pinned-engine regression gate

`scripts/test-code-index-native.sh` requires the built indexer, query bridge,
and Universal Ctags. It fails (rather than silently skips) if any is absent.
Override `LORE_ZOEKT_INDEXER`, `LORE_ZOEKT_BRIDGE`, and `LORE_CTAGS` only for
local test binaries. These overrides are test-script conveniences, not production
coordinator configuration. The two `NativeZoektReaderTests` resolve the baked
binary paths by default and run when present; `LORE_NATIVE_E2E_REQUIRED=1` makes
missing tools an error, never a skip. The native index script explicitly runs
both reader tests before the engine fixtures. The runtime image includes every
Compose YAML file read by its offline deployment-contract tests.
`scripts/verify.sh --docker` also runs this gate.
The tiny fixtures exercise actual pinned-engine lookup and lexical symbols;
they do not measure .63 memory, disk throughput, or real repository capacity.

Only one pilot publication is active at a time. Selecting a different tree/ref
replaces the active index pointer after successful validation; it does not
silently expand the indexed fleet. Older and failed generation directories are
retained for operator inspection. There is no automatic garbage collection or
cleanup authorization in this feature. They count against filesystem headroom,
so repeated failed builds eventually stop at the free-space guard.

The admission policy rejects CR/LF and non-UTF-8 filenames and a tracked
`.sourcegraph/ignore` file because the pinned engine cannot preserve the ordinary
exact-Git scope for those cases. The Git reader remains available. Zoekt's
binary, large-file and trigram exclusions are explicitly reported as partial
coverage; empty indexed results are never proof of complete source absence.
Symlink target text can exist inside a Zoekt shard, but the public adapter drops
those hits after checking the file mode at the exact Git commit.

Resource-budget or storage failures persist `blocked` with the attempted resource
and workload fingerprint. A later invocation does not repeat the build until
those limits or the selected pilot/symbol/catalog policy change. Read/session
settings do not reopen a blocked build. The last-good immutable index remains
intact. A supervisor interrupted after recording `building` is also gated, so an
OOM-killed container cannot repeatedly restart the same attempt. Inspect retained
failed directories before changing policy; this feature does not delete them.

Mirror, reader, index and evidence subprocesses share one bounded pipe/deadline
runner. Index admission still requires the existing hard cgroup, while readers
and evidence use sampled process-group RSS. Writer lock FDs are inherited only
by writer children. Policy wrappers retain their own bounded diagnostics.
