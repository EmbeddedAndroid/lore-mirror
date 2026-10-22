# Opt-in bare Git mirror coordinator

This phase adds storage and exact-revision publication, not a search indexer. It
uses the existing HDD and 3891 MiB deployment envelope. No SSD, RAM upgrade,
Zoekt deployment, object-pool deduplication, or production capacity result is
assumed. Code mirroring stays disabled until the operator selects and enables a
validated catalog. See `code-catalog.md` for configuration and `code-tools.md`
for the separate local-only read interface.

## Invocation and ownership

The mail daemon offers one due code tree after each mail cycle. The Python entry
point is `sync_code_once(full_config, *, now=None, adapter=None,
should_yield=None)`. These phases admit exactly one due tree. The direct `code-sync` CLI also offers one due
tree, not an unbounded bootstrap of the whole catalog. `adapter` is a Python
fixture seam, not an API/config option that can bypass upstream URL policy.

- Disabled code does not create files or contact an upstream.
- A separate `code-sync.lock` admits one code coordinator.
- Every tree takes the existing `<data_dir>/index.lock` with nonblocking flock.
  This is the same lock used by mail indexing and other code writers.
  Readers only probe it at fresh-query admission, then release it immediately.
  It is released between trees. A held lock yields `waiting_for_io`, not an
  overlapping job. The callback yields before a due mail-priority checkpoint.
- Default one-tree work units, oldest-due ordering, completion-based intervals,
  and deterministic 0-10% jitter avoid a burst of missed catch-up runs. U-Boot
  checks cannot be configured more often than hourly. Kernel checks have a
  five-minute minimum, with an hourly default.
- A running code unit can still delay mail indexing until its deadline. This
  release does not promise a five-minute mail-freshness SLA or preempt/resume a
  pack download halfway through. Default code unit deadline is 900 seconds.
- Code errors do not rewrite mail readiness, generation, or checkpoints.

The state is `<code.root_dir>/code-state.json`; repositories are bare directories
at `<code.root_dir>/repositories/<tree-id>.git`. The API should mount this data
read-only. API tools never import or call the coordinator and never fetch,
repair, initialize, index, or garbage-collect a repository.

## Upstream checks

Only explicit canonical HTTPS URLs within the trusted kernel.org and U-Boot
project namespaces are accepted. Arbitrary hosts, credentials, query strings,
encoded URL tricks, custom ports, SSH/file transports and redirects are rejected.
A deployment using a legacy redirecting URL must configure its verified canonical
URL; the coordinator does not guess or follow the redirect. Once an ID is bound
to an upstream, changing that URL requires a new ID or explicit operator migration.
The immutable per-ID binding survives removal of its status row from the catalog.

Kernel.org trees use the grokmirror `manifest.js.gz` fingerprint for the exact
configured repository path. The cache persists ETag and Last-Modified, and a
conditional GET returning 304 reuses validated cached fingerprints. If the
fingerprint and selected-ref configuration are unchanged, no `ls-remote` or
fetch is performed. Otherwise the coordinator queries the configured ref subset;
an unrelated whole-repository change still need not trigger a fetch. Manifest
fields cannot supply a fetch URL or object-pool/alternates path. A missing
configured entry fails that tree rather than silently selecting another one.

U-Boot uses a conditional **comparison of the advertised ref map**, not an HTTP
conditional GET: bounded `git ls-remote` runs at the slower configured cadence.
Its unchanged raw object-ID map skips `git fetch`. This distinction matters when
counting upstream operations: an unchanged U-Boot check still has one Git ref
negotiation. A changed check has the extra fetch negotiation. HTTP 429/503
Retry-After is respected for manifest requests; Git transport failures use
bounded exponential backoff because Git does not expose a structured HTTP
Retry-After value here.

Only explicit heads/tags and configured tag-prefix selectors are imported.
Bounded tag selections use descending natural numeric version order: `v6.10`
is newer than `v6.9`, and `v6.10` follows `v6.10-rc10`, which follows
`v6.10-rc2`. Numeric runs are compared as integers; a terminal `-rcN` precedes
its corresponding final release. The full ref name breaks equivalent-number
ties deterministically. This also orders dated `next-YYYYMMDD` tags correctly;
it is not a general SemVer parser. No advertised remote namespace is blindly
mirrored. Exact missing branches fail clearly, with no fallback to `master`
or another branch.

## Exact publication and recovery

A successful unit performs this sequence:

1. Persist a `fetching` journal that retains the last-good published ref map.
2. Initialize or validate a coordinator-owned bare repository. Shallow files,
   object alternates and unexpected local Git configuration fail closed.
3. Remove only the coordinator's private staging refs, then fetch full history
   for the selected upstream refs into `refs/lore/staging/...` atomically.
4. Compare the fetched raw OIDs with the advertised map. An upstream race makes
   the unit retry later, rather than publishing a mixed or substituted result.
5. Peel every staged head/tag. Commit-target refs retain their raw tag object in
   the Git pin while the API map contains the exact peeled commit OID. Tree/blob
   tags (including Linux's historical `v2.6.11` tree tag) are skipped and reported
   in `skipped_refs`, not mistaken for commits or allowed to fail valid siblings.
   A non-commit default ref, or a selection containing no commit, blocks the unit
   and preserves the complete previous publication instead of replacing it.
6. Admit the candidate JSON against the fixed 4 MiB reader/writer cap, then write
   `refs/lore/generations/<generation>/...` in a Git ref transaction. Git is run
   with `core.fsync=all` and `core.fsyncMethod=fsync`.
7. Remove private staging refs, then run `git pack-refs --all --prune` after
   publishing the pins. This compacts ref storage without object repacking or
   deleting historical pins. Count object packs, packed refs and generation pins.
8. Atomically replace and fsync the code JSON and its containing directory. This
   is the sole reader publication boundary. Readers do not inspect staging refs.

The state has schema version 1, a global `generation`, and a `trees` map keyed by
configured ID. Each tree includes `published_refs` (full ref name to commit OID),
raw `source_refs` (including skipped advertisements for stable unchanged checks),
per-tree generation, checked/fetched/published timestamps, health and retry fields,
`skipped_refs` object type/OID diagnostics, ref additions/moves/removals, and sampled
resource measurements. `measurements` also contains `object_pack_count`,
`packed_ref_count`, `generation_pin_count` and `loose_ref_count`; generation-pin
counts include retained pins and any prepared pins from an unpublished attempt. `checked_at` can advance without `fetched_at` or
`generation` advancing. There is no `indexed_at` claim in this phase.

A configuration fingerprint change makes the tree due immediately and marks its
old snapshot unsuitable for the changed scope. A recorded desired fingerprint
ensures transient failed attempts still honor backoff, rather than retrying every
wake. Whole-job and manifest deadline expiry always remain transient, preserving
the last-good snapshot. Repeated deadlines use exponential backoff starting at
60 seconds and capped at 24 hours, measured from each failed attempt's completion.
Persisted `consecutive_errors` and `next_check_at` preserve that pacing across
restarts; a successful unit resets the error count. Ordinary transient errors
retain their one-hour exponential cap (server `Retry-After` can extend it to
24 hours). No number of slow attempts permanently blocks a tree: the
temporary packs a killed fetch leaves behind are removed before the next unit
measures its disk budget.

Hard resource budgets (RSS, added/total disk, output and checkpoint caps), storage
failures and non-commit publication errors immediately persist `status=blocked`,
`retry_blocked=true`, a reason category and no `next_check_at`. They do not retry
across restarts or elapsed intervals until mirror configuration or relevant
limits change. A durable fingerprint includes the tree configuration, `max_refs`,
free-space floor, fixed checkpoint cap and all mirror/fetch/manifest limits;
query-only limit changes do not unblock mirror work. Adjusting configuration
unblocks one immediate attempt, not an exemption from the new limits.
Retired trees are removed from reader-visible JSON so it remains bounded to the
current catalog, but their repository directories, pins and ID bindings remain.

A missing exact upstream ref preserves the entire previous advertised generation
and reports degraded. A selector rotating out an old dated tag removes it from
the current map, records that change, and retains its old generation pin.
Force-pushed refs get a new generation; old generation pins remain reachable.
There is deliberately **no automatic object GC, object repack, object prune or
pin expiry** in this release. Ref packing is safe compaction, not object
maintenance. Retained pins grow storage and are counted by the repository budget.
A future maintenance implementation must honor reader/session lifetimes before
reclaiming them. Do not manually delete pins while readers may use them.

Before creating a new bare repository, the coordinator persists an initialization
ownership marker. A budget stop or interruption may leave only a directory or
config; the matching marker permits idempotent `git init` recovery after policy
allows another attempt. Successful bare-repository validation removes the marker.
Unmarked or untrusted existing repositories are not adopted, and recovery never
deletes existing objects, refs or uncertain Git lock files.

After interruption, `fetching` state becomes degraded (or queued without a
last-good snapshot), its pending generation is cleared, and the tree becomes
due again. This recovery is persisted even when the scheduler yields before
any tree work. On a later attempt, private staging is reconciled under the
writer/heavy locks and ordinary unit limits before the unchanged fast path,
even if the upstream returns to the last-good map and no refetch is needed.
When a fetch is needed, it reuses already downloaded complete Git objects where
Git can. This is safe retry, not a promise of byte-offset network resumption.
Incomplete or locked repositories are not automatically repaired by deleting Git
lock files. Corrupt/oversized JSON remains untouched for operator recovery. A
matching upstream fingerprint is insufficient if the advertised local commit
objects no longer validate. Last-good data remains available on ordinary fetch
failure, with degraded status and its actual age. A storage failure during JSON
publication attempts to persist a blocked checkpoint containing the prior exact
map. If storage cannot accept even that smaller failure checkpoint, the write
error is surfaced and the last durable checkpoint remains unchanged; no successful
publication or durable retry-block persistence is claimed in that situation.

A successful per-tree job is not necessarily full catalog readiness. Top-level
`queued` means no usable publication yet; `partial` means other configured trees
remain unpublished; `degraded` means one or more failed units; `ready` requires
all configured trees to have ready published data. `waiting_for_io` is temporary
and is recomputed on the next invocation.

## Resource and execution safeguards

Default limits are intentionally conservative pilot guardrails, not Linux size
estimates or an admission promise: 512 MiB sampled aggregate Git-process RSS,
8 GiB total apparent repository bytes, 2 GiB added apparent disk bytes per unit,
1 MiB combined subprocess output, 4 MiB compressed manifest bytes, 32 MiB expanded
manifest bytes, and a 900-second unit deadline. The configured filesystem keeps
at least 30% free plus a small active guard margin. A full Linux bootstrap may be
blocked by these limits; measure before changing them or enabling more trees.

Manifest body, expanded body, subprocess output/input and state JSON have finite
byte limits. **The fetch-byte budget is sampled added repository disk bytes, not
an exact network-transfer cap.** Disk/RSS sampling can overshoot between checks,
and neither is a hard allocator/filesystem quota. Container limits remain
necessary. Large diagnostic output terminates the job rather than accumulating
an unbounded log. Failed temporary Git files are not blindly deleted and continue
to count against subsequent disk admission.

Git receives a minimal environment, no system/global config, no credential helper
or interactive prompt, no hooks, no external attributes, no automatic maintenance,
no redirects, and only allowed transport protocols. No working tree, submodule
recursion, LFS download or checkout is performed. Every child process has its own
process group; timeout/budget cleanup also kills helpers when the Git leader has
already exited. The shared bounded process runner supervises memory, I/O and time,
including after pipe EOF. Git inherits the coordinator and heavy-I/O lock file
descriptors so a surviving writer cannot lose lock exclusion if its parent dies. Content from Git and manifest servers is data, never executable
instructions.

## Verification performed

`PYTHONPATH=src python -m unittest discover -s tests -p test_code_sync.py -v`
uses offline, tiny local repositories through an explicit test adapter. Coverage
includes full history, annotated commit/tree/blob tags, Linux `v2.6.11`-style tree
tag exclusion, unchanged conditional checks, force updates, required-ref deletions,
version/rc-sorted and bounded dated tags, advertised/fetched OID races, packed-pin
counts, interrupted partial fetch recovery before any refetch, stale-staging
cleanup on unchanged zero-refetch passes, transient deadline
retries capped at 24 hours, temporary-pack cleanup, durable
resource/storage/non-commit blocks, configuration-based
unblocking, publication failure and last-good preservation,
corrupt/oversized state, shared-lock contention and release between trees, custom
storage roots, output/time/RSS/disk guards, manifest expansion bounds, and orphan
helper termination. No production tree was cloned and no HDD load/capacity or
power-loss test is claimed. Production enablement and the later index pilot
remain separate operator decisions.

`PYTHONPATH=src python -m unittest tests.test_code_sync_interruption -v` also
interrupts a real paced local upload-pack after nonempty incoming pack bytes
arrive. It verifies that writer helpers terminate, last-good objects and pins
remain readable, a yield-only pass persists interrupted-journal recovery without
probing or fetching, private stale staging is reconciled on the subsequent
attempt, a new generation publishes after retry, and retained incomplete pack
bytes are not silently deleted.
