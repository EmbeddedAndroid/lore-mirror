# Bounded pilot measurements

As of 2026-10-07T16:16:03.743191+00:00. Phase: **four_list_index_ready_sync_degraded**.

Installed public-inbox commit `94b6bbb95cb88a6de59458b47869c74fc0c901ad` with Debian Xapian 1.4.29 and Perl 5.40.1. The September 25 unknown-thread fix is included.

Approved scope: linux-arm-msm, u-boot, devicetree-spec and u-boot-amlogic. Devicetree and linux-arm-kernel were explicitly deferred to a larger host and are not incomplete requirements of this run.

## Completion and initial measured resources

### devicetree-spec
Status: ready.
- Messages: 1,748
- Allocated Git: 5,459,968 bytes
- Allocated basic index: 1,974,272 bytes
- Marginal shared extindex: 31,203,328 bytes
- Index elapsed: 13.301 seconds
- Sampled peak aggregate RSS: 364,929,024 bytes

### u-boot-amlogic
Status: ready.
- Messages: 3,121
- Allocated Git: 10,850,304 bytes
- Allocated basic index: 3,137,536 bytes
- Marginal shared extindex: 124,698,624 bytes
- Index elapsed: 29.369 seconds
- Sampled peak aggregate RSS: 412,168,192 bytes

### linux-arm-msm
Status: ready.
- Messages: 314,070
- Allocated Git: 1,236,484,096 bytes
- Allocated basic index: 303,534,080 bytes
- Marginal shared extindex: 5,596,749,824 bytes
- Index elapsed: 2999.770 seconds
- Sampled peak aggregate RSS: 1,902,362,624 bytes

### u-boot
Status: ready.
- Messages: 632,345
- Allocated Git: 1,633,308,672 bytes
- Allocated basic index: 465,313,792 bytes
- Marginal shared extindex: 9,083,736,064 bytes
- Index elapsed: 6655.085 seconds
- Sampled peak aggregate RSS: 1,632,903,168 bytes

## Limits and missing measurements

- This is a bounded pilot, not an all-lore archive. Scope and completion are distinct.
- Per-inbox message counts include cross-posts. Do not sum them to claim unique corpus messages.
- Per-list extindex bytes are marginal growth in ingestion order; they are not independent complete indexes.
- Peak RAM is sampled aggregate subprocess-tree RSS at 250ms intervals, not an exact allocation high-water mark.
- The two small lists were initially cloned before the instrumented coordinator run; their recorded fetch steps are subsequent verification/update passes. Their initial clone duration and clone peak RAM were not measured. Initial basic/full-index timings and RAM were instrumented.
- Advertised pack bytes are not measured negotiated Git network transfer bytes. Empty pack listings may refer to pooled objects and are unknown, not zero.
- An immediate second cycle is a restart/idempotence check, not an hourly growth observation. Only the explicitly timed >=1h observations in this report establish hourly deltas, and only for their measured lists.
- LEI daemon is blocked in this executor by socket(AF_UNIX, SOCK_SEQPACKET) Operation not permitted. The pinned direct-library query adapter is tested instead.
- The linux-arm-msm basic-index log contains 34 lines labelled Message-ID too long, truncating. Warnings repeat and related XRF/reference terms also appear; unique affected messages and header fields were not counted. Raw Git bytes remain preserved, while native identifier/reference search terms may be truncated or skipped.
- No full-corpus capacity projection is justified by these pilot data.

## Query-path verification

Real small-list search, exact Message-ID, thread retrieval, unknown-thread zero-result behavior, Xapian newsgroup membership filtering, and raw Git blob lookup were exercised locally. Three lossless fixture emails with SHA256/provenance are retained under `tests/fixtures/real-pilot/`. Native MCP end-to-end verification is recorded separately.

## Operational state

The sync coordinator writes durable atomic state, preserves failed-stage checkpoints, admits only the four approved configured lists, discovers every epoch of an admitted list, serializes index writes, caps Git concurrency at two, and checks disk/RSS/deadline budgets while commands run. Hourly daemon mode is implemented; whether it is running is reported separately from implementation.

## Actual one-hour observation

Excluded: another list is undergoing initial bootstrap into the shared extindex. It cannot be attributed to these small lists or treated as ordinary hourly growth.

- devicetree-spec: 3651.3s elapsed; upstream_unchanged; local message delta 0; allocated Git delta 0 bytes; basic-index delta 0 bytes.
- u-boot-amlogic: 3607.7s elapsed; upstream_unchanged; local message delta 0; allocated Git delta 0 bytes; basic-index delta 0 bytes.

- linux-arm-msm: 7081.1s since completed baseline; upstream_changed=True; local message delta 20; allocated Git delta 479,232 bytes; basic-index delta 28,672 bytes. Actual local mirror change between completed checkpoints. This includes catch-up while the other archive bootstrapped; it is not a clean upstream arrival-rate sample. Shared extindex growth during the baseline interval is excluded.

Four-list reconciliation: ready at 2026-10-07T16:00:20.418507+00:00, generation 2. Latest per-inbox message-ID counts total 951,304.
U-Boot’s immediate repeat is idempotence verification only, not a claimed one-hour sample.

Daemon state: blocked_unverified.

The index remains read-safe at generation 2; last successful four-list reconciliation was 2026-10-07T16:00:20.418507+00:00. The first hourly-daemon cycle failed with `<urlopen error Tunnel connection failed: 403 Forbidden>` at the managed-proxy tunnel, before an archive response. The exact proxy/policy cause is unverified.

The last recorded PID was 2 (current process namespace); zero successful daemon cycles were verified. Its recorded next time was 2026-10-07T17:07:37.963803+00:00, but the process handle is now unavailable and no coordinator lock is held. Do not treat this as a verified scheduled run or workspace-persistent service.


## Selected-scope pack preflight

Advertised packs for the approved four-list subset: 2,792,360,747 bytes. This excludes loose/new objects, pack indexes, and search metadata. The approved four lists do not use the deferred object pools.

All approved epochs have advertised pack sizes. These are source pack sizes, not guaranteed negotiated client storage.

A machine preserving 30% free space needs capacity at least `(steady_state + temporary_reindex_workspace + observed_growth_reserve) / 0.70`. The growth reserve is not established by one quiet hour on two small lists. Reindex workspace is a separate term, not included in the 30% free-space calculation.

## Conditional full-manifest scaling

The current full-manifest message count is unknown. Let N_large and N_small be summed per-inbox message-ID counts in the two content/size strata, including cross-post memberships. The following rates are measured sample ranges per one million such IDs, not a corpus-size claim or confidence interval.

- large: 2 completed samples; 16.47-21.16 GiB of Git/basic/marginal-shared-index per million message IDs; 2.65-2.92 index hours per million.
- small: 2 completed samples; 20.59-41.38 GiB of Git/basic/marginal-shared-index per million message IDs; 2.11-2.61 index hours per million.

Conditional steady-state scenario in bytes: N_large × measured_large_bytes_per_ID + N_small × measured_small_bytes_per_ID + application/derived-store bytes. Index-time scenario in seconds uses the corresponding per-ID time rates. Keep the per-stratum N values unknown until measured; do not substitute a historical self-reported corpus count as today’s count.

Provisioning scenario: (steady_state + separate_reindex_workspace + observed_growth_reserve) / 0.70. Shared-index deduplication, cross-post overlap, ingestion order, body/patch sizes and untested high-volume lists can move actual results outside these sample ranges. The current quiet-hour observation does not establish a daily-growth reserve. Peak RAM is reported only as observed; it is not linearly extrapolated.

### Four-list component subtotal

Measured initial Git + basic + shared extindex: 17.23 GiB. Keeping 30% free requires 24.61 GiB for these known components alone.

A separate workspace equal to one additional shared extindex is 13.82 GiB; including that illustrative workspace and 30% free gives 44.35 GiB, before application/OS, growth reserve and other simultaneous maintenance. This is not a measured reindex peak.

## Deferred larger-host scope

This is capacity context for future work, outside the approved four-list pilot. The deferred namespaces’ object-store pack sizes were measured on the same mirror host.

- linux-devicetree: 3,801,596,910 advertised pack bytes, counting its shared pool once, before client Git/basic/full-index overhead.
- linux-arm-kernel: 4,421,699,135 advertised pack bytes, counting its shared pool once, before client Git/basic/full-index overhead.

No clone of either deferred namespace was started. A runtime clone guard prevents the earlier coordinator from expanding beyond the approved four while it finishes.

## Host requirements and observed runtime

The measurement host was Debian 13 with 9 visible logical CPUs and 9.73 GiB visible RAM. Effective container CPU/memory quotas and utilization were not established. Indexing was serialized; the sum of measured initial basic/full-index stages was 9,697.525 seconds (2.69 hours), excluding fetches, software installation and coordination. The largest sampled index-process-tree RSS was 1,902,362,624 bytes (1.77 GiB). This is not a claim that the entire host or simultaneous query service fits in that amount.

For a fresh pilot host, use 64-bit Linux, Python 3.11+, Git, Perl and the pinned public-inbox/Xapian dependencies in README. A practical starting allocation is 2-4 logical CPUs and 8 GiB RAM for one index worker plus the bounded query service; performance at that CPU allocation has not been benchmarked. The shipped Compose limits are 4 GiB/2 CPUs for sync and 2 GiB/2 CPUs for the API. Native processes are not constrained by those Compose limits.

For disk, the measured corpus alone needs 24.61 GiB capacity to preserve 30% free space. The illustrative additional-index-copy scenario needs 44.35 GiB before OS/application data, growth and other maintenance. A 64 GiB data volume is a reasonable starting point for this four-list pilot with that illustrative workspace, not a guarantee of future retention or a measured rebuild peak. Tyler still chooses the host. The current workspace fits the corpus, but does not have room for a full additional index copy while keeping its reserve.
