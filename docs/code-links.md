# Qualified local mail/code evidence (phase 5)

This feature builds an independent local evidence database from already
published code mirrors and already mirrored mail. It does not fetch mail or
code, follow a commit's URLs, apply patches, send messages, or enable
`code.blame`. The existing HDD/4 GB-class `.63` host remains the target; no
hardware upgrade, new host, or new Compose service is introduced.

The links builder is **disabled by default** and runs only through the
operator's `code-links` CLI. Neither API queries, mail synchronization, code
mirroring, nor the Zoekt indexer start a backfill. A Zoekt index is not required.
Fixture tests and reviewed limits do not establish production capacity.

## Explicit coverage policy

Add this fragment inside the existing `code` object to retain the disabled
defaults:

```json
"links": {
  "enabled": false,
  "patch_since": null,
  "batch_commits": 50,
  "batch_mails": 25,
  "timeout_seconds": 30,
  "max_db_bytes": 1073741824,
  "max_patch_bytes": 8388608
}
```

Before enabling, the operator must choose and enter a real calendar date as an
exact ASCII `YYYY-MM-DD` string in `patch_since`. There is deliberately no
invented date, rolling default, or implicit all-history patch-ID backfill.
`enabled: true` requires both `code.enabled: true` and that explicit date.
The date is a declared scan horizon, not evidence that every eligible message
or commit has already been processed. An older-date policy can require much
more work even if per-run batches stay small.

Evidence backfill starts from each configured tree's exact `index_refs`,
resolved to their already published OIDs. At least one such ref is required at
run time. This reuses the catalog's explicit hot-ref selection; it does **not**
require enabling `code.index` or building Zoekt. Merely mirroring a branch/tag,
including the numeric Qualcomm release selectors, does not add it to the
evidence scan. Select only the desired scope before the first batch.

Trailer scanning traverses that selected history independently of dates.
Expensive commit patch IDs are limited to commits whose **committer timestamp**
is on or after the selected date at midnight UTC. Mail patch IDs require one
valid timezone-aware `Date` header on or after the same instant. Missing,
ambiguous, malformed, timezone-free or earlier dates retain an unambiguous mail
identity for citation evidence but do not produce a patch ID. Mail dates are
sender-provided metadata, not proof of delivery time or authenticity.

| Field | Default | Maximum / validation |
| --- | --- | --- |
| `enabled` | false | Strict boolean; requires enabled code and explicit horizon. |
| `patch_since` | null | Valid `YYYY-MM-DD`, required when enabled. |
| `batch_commits` | 50 | Positive integer, maximum 500. |
| `batch_mails` | 25 | Positive integer, maximum 250. Counts mail hashed in this batch; already-hashed variants only cost a lookup. |
| `timeout_seconds` | 30 | Positive integer, maximum 120. |
| `max_db_bytes` | 1 GiB | Positive integer, maximum 2 GiB. |
| `max_patch_bytes` | 8 MiB | Positive integer, maximum 16 MiB. |

Unknown fields, numbers supplied as booleans, fractional limits, invalid dates,
and malformed disabled policies fail with `invalid_config`. A disabled policy
may retain a valid explicit date for later review. Normalization through both
`load_config` and `prepare_config` is offline and creates no data directories.
There is no command-line horizon override or daemon mode for `code-links`.

## One bounded batch, using the existing writable deployment

Use the same reviewed configuration and physical data store as API and sync.
The builder needs **read-write** access to `data_dir`, `code.root_dir`, and the
shared lock. Do not run it inside the API's read-only `/data` mount. Build and
verify the updated runtime image before use; an older image does not acquire
the new CLI merely because the configuration has changed.

For the `.63` named-volume deployment, after pausing the existing sync daemon
through the usual approved maintenance procedure:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml \
  run --rm --no-deps sync code-links --config /config/pilot.json
```

For the existing bind-backed deployment, keep its override and the same
`LORE_DATA_DIR`:

```sh
docker compose -f compose.yaml -f compose.deploy-63.yaml -f compose.bind.yaml \
  run --rm --no-deps sync code-links --config /config/pilot.json
```

These commands reuse the existing sync service's image, resource limits,
configuration and writable `/data` mount. No code-index overlay is needed.
Inspect the effective Compose configuration first. Do not accidentally omit the
bind override and write evidence into a different named volume, or use
`compose.yaml` alone and inherit its unsuitable 4 GiB sync limit.

Gracefully stop the normal sync daemon before creating this one-shot container,
so its 2304 MiB cap is not duplicated alongside another sync process. Use the
approved maintenance procedure: stop sync, verify it is stopped and writer
children/`index.lock` are clear, run one batch, then start sync and verify its
health and advancing mail freshness. Keep the deployment's bind override on
all commands when applicable. Do not use `docker pause`, which retains memory
and may freeze a lock holder. Do not run concurrently with an index pilot.
Resume the daemon after the inspected batch, including after failed or interrupted
batches; an error or shell `set -e` must not bypass this manual recovery step. This is an
operator maintenance procedure, **not** an automatic deployment or scheduled
loop. It intentionally pauses mail freshness work; no five-minute freshness
SLA is implied. Measure actual available memory and disk headroom first. Caps
are not reservations, and the I/O lock does not reclaim other processes' memory.
Keep the existing minimum 30% free-space reserve.

The builder takes the same `<data_dir>/index.lock` as mail indexing and code
fetch/index writers. Contention defers work instead of starting another writer
scan. Fresh code and link readers probe the lock nonblocking and immediately
release it; they report busy if a writer already holds it. Active readers never
hold or inherit this lock and cannot delay a mail indexer. They read committed
evidence and exact published code identities, rejecting a generation change
before returning. Cached cursor pages never probe the lock, but still validate
their captured code, mail and evidence generations.
Work is bounded by commit/mail batch counts, patch bytes, database capacity and
the time budget. A batch boundary is a checkpoint, not a claim that all history
has been covered. Review the result before deciding to run another batch.
Writer Git subprocesses inherit the held I/O lock so an orphaned helper cannot outlive
the supervisor while permitting a competing heavy scan.

The CLI prints one JSON result with status, links generation, processed counts,
coverage and any bounded error. Exit codes are:

- `0`: `disabled`, `ready`, or `partial`. `partial` is normal bounded progress,
  **not** completed coverage. `ready` concerns only the reported local scope.
- `2`: `blocked`, `degraded`, `waiting_for_io`, or another non-success status.
- `1`: configuration or structured command failure, using the existing error
  JSON on stderr.

A disabled invocation must not initialize a database or scan the archive.
Never turn exit code 0 alone into an assertion that all matching evidence was
found. Read the status and coverage fields.

## Query and evidence interpretation

When links are enabled, the existing MCP/HTTP interface exposes:

- `code.related_mail(tree, commit, ref=null, limit=20, cursor=null)`.
- `mail.related_commits(message_id, tree, ref=null, variant=null, limit=20, cursor=null)`.

`tree` is an explicit configured tree ID. `commit` is a full lowercase Git OID,
not a short hash or revision expression. `ref` selects an exact published local
ref, defaulting to the configured default ref. No query repairs a missing
mirror, changes a branch, or starts the builder. `variant`, when supplied, is
an exact source Git blob identity (40 or 64 lowercase hex characters) or raw
message SHA-256 identity (64 hex characters). Results distinguish `source_blob`
from `raw_sha256`; do not treat either as a Message-ID or a patch ID.

The same Message-ID can have multiple stored raw variants. Cross-posts, duplicate
deliveries, revised patches and cherry-picked commits must remain distinguishable.
Without an exact variant filter, multiple matching evidence rows may be returned.
A commit trailer citing a Message-ID does not identify which raw variant the
author meant. A missing local variant or incomplete inventory limits the claim.

Evidence has distinct meanings:

- `linked`: a recognized commit trailer cites mail known in the local inventory
  when the evidence was recorded. It is a citation, not proof that the cited
  patch was applied or remains in today's mail corpus. `Link:`, `Closes:` and
  `Fixes:` must retain their different meanings; a `Fixes:` commit reference
  does not establish a mail-patch match. Recognized URLs are
  `lore.kernel.org/<list>/<Message-ID>` and b4's fixed
  `patch.msgid.link/<Message-ID>`; a bracketed maintainer note such as
  `[ bp: Massage commit message. ]` inside the final trailer block is skipped.
- `patch_equivalent`: independently computed patch IDs match under the same
  explicitly versioned normalization. Git's stable patch-ID ignores whitespace
  and file-diff ordering, so this is equivalence evidence rather than raw byte
  identity or unique authorship. See [Git's patch-ID documentation](https://git-scm.com/docs/git-patch-id).
- `candidate` or an unknown/unavailable result: weak, ambiguous or unsupported
  evidence is not promoted to a verified match.

Both mail and code patch IDs are computed locally with the same algorithm.
There is **no join against public-inbox `patchid:` values**, nor against a
separate mail-semantic fingerprint whose compatibility has not been established.
Preserve the returned algorithm/version and source references when interpreting
a result. Unsupported binary/combined diffs, merge commits, malformed or
oversized patches, ambiguous MIME, squashed/split revisions and conflict edits
can prevent a match. No-match does not mean a patch was never used.

Reachability is a separate check against the exact published target commit.
Git ancestry answers whether the evidence commit is an ancestor of that
snapshot, not whether its changes still have an effect; see
[Git's ancestry check](https://git-scm.com/docs/git-merge-base).
`reachable`, `not_reachable`, and `unknown` must not be rewritten as an
unqualified “merged” or “not merged”. A maintainer branch is not mainline, a
reachable change may have been reverted, and one patch match says nothing
conclusive about the rest of its series. `application_status` and
`effective_change` remain `unknown`.

### Three distinct horizons

- **Graph-query horizon:** each fresh lookup first makes one exact-OID
  `rev-list --parents --max-count=scan_limit+1` call, rooted at the published
  target and the distinct evidence commits. This is a fast shared graph, not a
  historical eligibility cutoff. A complete path proves reachability; a
  negative graph answer requires the entire target ancestry to be present.
  An unresolved candidate then gets `merge-base --is-ancestor <candidate>
  <published-target>` with **no commit-count or date cutoff**. At most **64
  distinct unresolved candidates**, in returned evidence order, receive these
  fallback checks per lookup; known graph answers and duplicate commits do not
  consume that allowance. Thus a fresh lookup runs at most one graph process
  plus 64 fallback processes, and a complete graph needs no fallbacks.
- **Patch-equivalence horizon:** the operator's explicit `patch_since` date
  limits expensive commit/mail patch-ID work as described above. It does not
  limit trailer traversal or exclude an older evidence commit from an exact
  ancestry check.
- **Evidence-inventory horizon:** a lookup considers only previously committed
  evidence for the requested identities, with at most `min(1000, scan_limit)`
  evidence rows before service pagination. Builder scope, completed batches,
  selected refs, raw mail variants and historical inventory membership still
  limit discovery. Exact ancestry cannot find absent evidence or turn an empty
  page into proof of non-application.

The graph and all fallback checks share the enclosing read request's existing
`query_timeout` deadline, aggregate `query_output_bytes` allowance and per-child
`query_memory_bytes` limit. A fallback never resets the timeout or output
budget. Byte-truncated graph records are discarded; intact positive paths
remain usable. Resource exhaustion stops further checks, and a request whose
overall deadline expires fails with `query_timeout`. Missing/invalid objects,
Git errors, resource limits and candidates beyond the 64-check cap remain
`unknown`, never `not_reachable`. A subprocess cleanup timeout instead fails
the request with `code_cleanup_timeout`, rather than claiming a normal partial
result while a child might remain. Git's completed exit status 0 proves
`reachable`; completed exit status 1 proves `not_reachable`.

If unresolved candidates remain, `partial_reasons` identifies the graph limit
(`ancestry_scan_limit`), fallback cap (`ancestry_fallback_limit`) or relevant
Git/resource error. A count-truncated graph alone adds no ancestry partial
reason when fallbacks resolve every candidate. Results nevertheless retain the
independent evidence/inventory qualifications below. Increasing `scan_limit`
changes the shared graph bound and, up to 1000, the evidence-row bound, not the
date horizon or the fixed 64-fallback cap.

## Coverage, recovery and generations

Always inspect the returned scan coverage and partial reasons alongside hits.
Cheap commit-trailer history scanning and expensive patch-ID work have different
coverage; neither is inferred from the other's completion. Only declared local
mirrors, published ref snapshots and the configured local mail selection are
in scope. Historical tags, removed/rewritten refs, unscanned commits, missing
mail variants and work outside the explicit patch horizon can all matter.

An empty page means **no match in the scanned scope**, not “not applied”, “not
posted” or “not merged”. Pagination exhaustion does not erase incomplete
coverage. A source update can change the relevant scope after an earlier batch
was ready; repeat observations must identify their generations and target OID.

Link-query responses are currently **always partial**, with
`current_mail_membership: "not_verified"`. Stored rows retain the provenance of
their historical mail inventory; queries do not re-fetch or revalidate each raw
variant against the current mail backend. Exact-snapshot scan coverage is
reported separately, including incomplete, not-scanned or stale-catalog scope.
Thus a batch's `ready` status does not make a subsequent query an exhaustive
claim about today's mail corpus. Bounded historical evidence is still useful,
but a record is not proof that the same variant remains locally present now.

The database is `<code.root_dir>/links.sqlite3`. It uses SQLite's DELETE rollback
journal mode and FULL synchronous writes. Evidence rows, coverage checkpoints
and the links generation publish in the same transaction. A separate
`links-state.json` records only the independent budget and health checkpoint;
it is not authoritative for evidence, coverage or generation. The database
budget reserves approximately half for rollback-journal growth, and file/journal
sizes are checked against the configured bound. This is not a limit on the
entire mail/code data store.
Failures preserve the previously committed evidence and checkpoint. Read tools do not repair
corrupt, missing or incompatible state. A failed links batch is separate from
mail readiness and must not rewrite mail synchronization checkpoints.

Each selected snapshot has a persistent parent frontier. Previously completed
exact snapshots can serve as verified traversal boundaries; a rewritten ref
walks until a real completed boundary or graph root, not an assumed date cutoff.
If the ref moves before a walk completes, the new snapshot's job adopts the
unfinished job's visited set and frontier, which is then marked `superseded`,
so an active branch does not restart its walk on every tip change.
Mail enumeration advances an exact raw-variant keyset checkpoint with each
completed item. A changed/dirty inventory pass remains incomplete or restarts;
a restarted pass skips variants it already hashed without spending the batch
quota on them, and stops at half of the remaining batch window so the probes
cannot run into the batch deadline. The source fingerprint includes the SQLite
file's mtime and ctime because a WAL checkpoint followed by sidecar
recreation can otherwise leave every other marker unchanged;
a completed unchanged mail generation is probed without rehashing every message.
The algorithm, Git version, explicit date and patch-byte budget are part of the
processing policy. Changing them triggers a rescan rather than silently reusing
coverage from a different policy.

Completed traversal/inventory does not mean every patch was interpretable.
Skipped or unsupported oversized, binary, merge and ambiguous inputs retain
coverage counters or evidence warnings. Inspect those along with `ready`.

Link-query cursors bind the query arguments and mail, code and links generations.
When one of those generations changes, restart the query
rather than merging pages from different evidence/ref states. Last-good records
may remain useful evidence about earlier commits, but current reachability must
be checked against the requested current snapshot rather than inherited from
old membership. Backfills and history rewrites never justify dropping these
qualifications.
Consistent cursor generations prevent mixed pages; they do **not** prove that
a historically recorded mail variant is a member of the current mail corpus.

## Validation scope

Run the offline config/CLI, evidence parser, links database, backend and service
tests, then the existing regression suite. Tiny local Git/mail fixtures cover
behavior, not production backfill throughput or full-history coverage. Run
container and resource checks separately on a Docker-capable host before any
authorized pilot. No production links build, deployment, or capacity result is
implied by this patch.

Normal commit/mail count boundaries remain resumable `partial` progress. Hard
time, output, memory or storage exhaustion instead records `blocked` in the
independent `links-state.json` budget checkpoint. Already completed items may be
committed when the final transaction fits; incomplete items never advance their
frontier. Later invocations do not re-run expensive work until a relevant limit,
batch size, horizon or selected scope changes. Unrelated reader/session settings
do not reopen it. Last-good evidence remains readable.

Each read lookup uses one short, read-only SQLite transaction for rows and their
generation metadata, never the heavy writer lock. SQLite may still report busy
at its bounded deadline; this is separate from holding the mail/index I/O lock.
Patch-ID commands use the same bounded process runner and configured reader RSS
budget as other local evidence reads, with inherited writer locks and whole
process-group cleanup.
