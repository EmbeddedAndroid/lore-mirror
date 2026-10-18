# Full-manifest deployment on <host>

The laptop checkout is authoritative. `scripts/sync-deploy-63.sh` transfers
committed source with rsync over SSH and records SOURCE_COMMIT. Its transport
container provides rsync and SSH without installing laptop packages. The host
requires Docker, Compose 2.24.4 or newer, Bash and rsync. No CIL resources are
used. Mail is a bind mount at /srv/lore-mirror-data, owned by 10001:10001.

Run on the laptop after committing the deployment change:

```sh
scripts/sync-deploy-63.sh
ssh -F /dev/null root@<host> 'cd /srv/lore-mirror && mkdir -p evidence/deploy-63 && nohup scripts/deploy-63.sh > evidence/deploy-63/deploy.log 2>&1 < /dev/null &'
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

The host-only override publishes `<host>:8765` for the explicitly
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
`priority_lists` every `priority_interval_seconds` (300), so replies on those
lists become searchable within minutes of reaching lore. A full pass that runs
longer than the interval is followed by one priority pass before the next
full pass; priority lists are not refreshed while a long full pass runs.

After a failed cycle (for example DNS not yet up after a power cut) the daemon
retries after `retry_seconds` (60, 120, 300, then 900 s), capped at the
interval, and returns to the normal schedule after the first good cycle.
Containers restart with Docker (`unless-stopped`). An inbox interrupted while
indexing keeps `index_dirty` and is indexed again on the next cycle that
includes it; status reports `unsafe_index` until then.

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
