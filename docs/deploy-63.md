# Full-manifest deployment on <host>

The laptop checkout is authoritative. `scripts/sync-deploy-63.sh` transfers
committed source with rsync over SSH and records SOURCE_COMMIT. Its transport
container provides rsync and SSH without installing laptop packages. The host
requires Docker, Compose 2.24.4 or newer, Bash and rsync. No CIL resources are
used. Mail is a bind mount at /srv/lore-mirror-data, owned by 10001:10001.

Run on the laptop after committing the deployment change:

```sh
LORE_DEPLOY_HOST=<host> scripts/sync-deploy-63.sh
ssh -F /dev/null root@<host> 'cd /srv/lore-mirror && mkdir -p evidence/deploy-63 && LORE_BIND_ADDR=<host> nohup scripts/deploy-63.sh > evidence/deploy-63/deploy.log 2>&1 < /dev/null &'
```

Poll the log during the native arm64 build. The script runs the entire offline
suite before starting services, generates config/full-manifest.json using
configure-manifest.py and config/deploy-63.json, and starts initial import in
the sync daemon. It refuses to rebuild while this stack's sync is running.
Every invocation assigns a unique source/UTC/UUID image fingerprint; inspect
all running containers against it. Generated .env selects three Compose files.
The default laptop Compose configuration remains localhost-only and small.

## Resource settings

| Setting | Value | Reason |
| --- | --- | --- |
| Sync memory / memory+swap | 2304 / 2304 MiB | Hard cgroup limit, no swap |
| API memory / memory+swap | 512 / 512 MiB | Bound queries and session cache |
| Gateway memory / memory+swap | 64 / 64 MiB | Fixed TCP forwarding only |
| Combined service memory | 2880 MiB | Leaves 1011 MiB of 3891 MiB for host |
| Sync / API / gateway CPUs | 2 / 1 / 0.5 | Limit contention on slow cores |
| Native job sampled RSS | 1792 MiB | Terminate job before sync cgroup ceiling |
| Native job deadline | 7 days | Allow large historical epochs on slow disk |
| Git pull workers | 2 | Conservative upstream concurrency |
| Index jobs | 1 | Serialized basic and shared index writes |
| Basic/shared batch size | 4 MiB | Reduce Xapian memory versus previous 16 MiB |
| Multi-pack-index generation | Disabled | Avoid multi-GB memory spikes |
| Per-inbox index | basic | SQLite only; full search lives in shared extindex |
| Shared Xapian shards | 1 worker/shard via -j1 | Avoid parallel HDD seeking |
| XAPIAN_FLUSH_THRESHOLD | Unset | Use byte batch limit recommended by pinned docs |
| API retained sessions / bytes | 16 / 8 MiB | Bound memory for pagination |
| API query timeout | 60 seconds | Allow slow native queries |
| Sync interval | 3600 seconds after cycle | Full initial import precedes hourly updates |
| Disk reserve | 30% of filesystem | Stronger than 100 GB minimum |

Public-inbox retains default fsync durability. Initial selection contains all
manifest epochs with no date/shallow filters. The two existing small inboxes
are indexed first as end-to-end probes; every remaining inbox stays selected.
Mail reads do not wait for index writes: they see the last committed index
batch, so completed inboxes stay searchable while later ones download and
index, and an inbox being indexed returns what has been committed so far. Each
completed inbox advances the pagination generation. Interrupted basic/shared
writes retain the unsafe flag until recovery, even if a later inbox succeeds;
status reports it.

The host-only override publishes `$LORE_BIND_ADDR:8765` for the explicitly
requested LAN HTTP MCP endpoint. It is unauthenticated and confined to this
binding. The host template allows exactly <host>:8765 in Host and
http://<host>:8765 in Origin while retaining DNS rebinding protection.
No host firewall or SSH configuration is changed. API mail remains
read-only on an internal Docker network. Gateway starts alongside API so
health is accessible before the first index exists.

Upstream policy: use [the kernel.org administrator's mirror recipe](https://people.kernel.org/monsieuricon/mirroring-lore-kernel-org)
with erol and two pull workers. public-inbox-clone handles Git replication.
No lore web searches, message downloads or web scraping are used.

## Scheduling and restart recovery

The sync daemon starts a full pass over every selected inbox once per
`interval_seconds` (3600, start to start). Between full passes it refreshes
`priority_lists` every `priority_interval_seconds` (300). During a full pass,
the daemon also checks that interval after each inbox and, when due, checks a
fresh manifest and refreshes priority inboxes before resuming the pass. Only
inboxes with a completed first index (`indexed_at` set and `index_dirty` clear)
are eligible for priority refreshes. Unindexed or interrupted inboxes wait for
their full-pass turn. A single long clone or index can delay a refresh until
that job's inbox finishes; refreshes do not run concurrently with full-pass work.
Full passes keep their start-to-start schedule, with a short pause if overdue.

Both `daemon-state.json` and `sync-state.json` record `last_priority_refresh_at`
and `last_priority_refresh_status` as soon as a priority attempt finishes, even
while a full pass is still running. The timestamp includes failed attempts;
`partial` means at least one eligible inbox failed. The saved timestamp is reused
after restart. `next_priority_refresh_at` and `priority_consecutive_errors` in
sync state preserve the due time and backoff across full-pass and restart
boundaries. Priority errors are recorded on the affected inbox rows and do not
abort the full pass. Due checks use `retry_seconds` after priority errors, and
return to the priority interval after a successful attempt.

After a failed cycle (for example DNS not yet up after a power cut) the daemon
retries after `retry_seconds` (60, 120, 300, then 900 s), capped at the
interval, and returns to the normal schedule after the first good cycle.
Until the next full pass is due, those retries are priority-only cycles. A
coordinator error stays in `last_error` (with `last_error_at`, the first
occurrence, and `last_error_step`) only until a later success of the same
kind: a manifest or DNS failure clears on the next successful cycle-start or
priority manifest fetch, and any other cycle failure clears when a cycle
completes. It then moves to `error_history` with `cleared_at` and
`cleared_by`, so a long import no longer reports a failure that ended hours
ago, while a failure that recurs later in every cycle stays visible.
Per-inbox errors follow the same rule: fetch and preflight failures clear on
the next successful fetch, index failures when the inbox is indexed again.
Errors recorded before steps were tracked clear on the next success of any
kind.
Containers restart with Docker (`unless-stopped`). An inbox interrupted while
indexing keeps `index_dirty` and is indexed again at its next full-pass turn;
status reports `unsafe_index` until then. Priority work uses the same index lock,
per-inbox checkpoints, and storage guards as ordinary full-pass work.

### Unclean extindex exits

Stopping the daemon (for example `docker compose up -d` recreating sync) sends
SIGTERM to the native index leader only: public-inbox shard workers ignore it,
and the leader's git helpers must stay alive until it has flushed, so the
process group is not signalled. The coordinator waits
`index_stop_grace_seconds` (30, below the 45 s Compose stop grace) before
killing the group. A leader that exits cleanly in time needs no repair.

public-inbox commits SQLite, including its last-commit checkpoint, before the
Xapian shards. A writer killed between the two (power cut, OOM, an overrun
grace) leaves documents in over.sqlite3 that are missing from Xapian, and a
resumed run does not revisit them. Before every extindex job the coordinator
records `extindex_floor` (highest over document and xref3 row) and sets
`extindex_running`; a clean exit clears it. When the next extindex job finds
it still set, it queues every document past the floor in public-inbox's
persistent `eidxq` (and clears a stale `eidxq_lock`, which cannot match a
restarted container), and public-inbox reindexes those documents when a run
completes. `extindex_recovery` records the floor and count. The floor only
advances while the queue is empty, so a crash during repair is covered too.
Known limit: a message removed from an inbox in the lost window can leave a
Xapian document without an over row; readers skip it, so only result counts
are affected.

## Monitoring and recovery

```sh
ssh -F /dev/null root@<host> 'cd /srv/lore-mirror && docker compose ps && docker compose exec -T api python /app/scripts/progress.py'
ssh -F /dev/null root@<host> 'df -h /srv/lore-mirror-data; du -sh /srv/lore-mirror-data; free -m; uptime'
ssh -F /dev/null root@<host> 'cd /srv/lore-mirror && docker compose logs --tail=80 sync api gateway; tail -n 10 /srv/lore-mirror-data/logs/*.log'
curl --fail http://<host>:8765/api/health
```

The internal disk guard checks every 250 ms while native jobs run and stops
expansion at 30% reserve. A blocked/error row is retained with its native log.
If capacity projection or observed free space threatens 100 GB, stop sync:

```sh
ssh -F /dev/null root@<host> 'cd /srv/lore-mirror && docker compose stop sync'
```

Keep API/gateway and existing data for diagnosis. The progress command uses
the API container so it also works while sync is stopped; its cgroup fields
then describe API memory, not historical sync peaks. Do not delete epochs or
remove writer locks. After resolving capacity or failed native jobs, resume
with `docker compose up -d sync`. An interrupted index is unsafe until the
same inbox successfully reconciles. Run the full suite before redeployment.
See REPORT-DEPLOY-63.md for actual measurements and unverified projections.
