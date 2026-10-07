# Capacity findings, 2026-10-07

Full-manifest trial on <host> and what a production host needs.
Source details: REPORT-DEPLOY-63.md, docs/pilot-measurements.md.

## Trial host

Socionext Developer Box (SynQuacer), Debian 13 arm64, 24x Cortex-A53,
3.8 GiB RAM, one 1 TB 7200 rpm HDD (848 GiB free). Image `lore-mirror:deploy-63`
from commit 7a567b2. Mail on bind mount /srv/lore-mirror-data.

State left running: API and gateway healthy at http://<host>:8765/mcp;
sync stopped at 20:52 UTC on purpose (capacity rule). 4 of 356 inboxes are
complete and searchable (363 MB on disk).

## Measured

- Manifest: 356 inboxes, 405 Git epochs, full history.
- Advertised Git packs: 110.3 GB (102.7 GiB), 27.0 GiB of it in 12 shared
  pools counted once. One epoch (lvfs-general/git/0.git) has unknown size.
- Effective rates on this host: about 16 h to clone everything, about 435 h
  to index (about 19 days), before HDD slowdown as the index grows.

## Size projections (estimates, not measurements)

| Basis | Total mail data |
|---|---|
| Ratio of the 4 small lists completed here (8.4x Git) | 866 GiB (pessimistic: fixed overhead dominates tiny lists) |
| Ratio of the large lists in the x86 pilot (5.8x to 6.8x Git) | 593 to 703 GiB |
| Per-list indexes only, no cross-list index (Git plus about 25%) | 130 to 150 GiB |

The cross-list (extindex, lore `/all/` style) search index is most of the
size. Kernel mail is heavily cross-posted, so deduplication in that index may
bring the real figure below the large-list estimate; unproven until measured.

## Why it stopped

The brief required stopping if free space would drop below 100 GB. The
866 GiB case exceeds the 848 GiB free; even 593 to 703 GiB leaves little
margin, and the stack's own 30% headroom rule caps usable space at about
576 GiB on this disk.

## Requirements for the next host

| Scope | Disk | RAM | CPU | Initial build |
|---|---|---|---|---|
| Everything, with cross-list index | 2 TB, SSD strongly preferred | 8 to 16 GB | 4+ x86 or modern Arm cores | days on SSD/x86; weeks on HDD/A53 |
| Cross-list index for the lists we use, per-list for the rest | 500 GB to 1 TB | 8 GB | 4+ cores | roughly half of the above |
| Per-list indexes only | 250 GB | 4 to 8 GB | 2 to 4 cores | 1 to 3 days |

## Before restarting a long sync

1. Add a runtime free-space guard: the sync service stops itself below a
   configurable floor. Today the check only runs once at setup
   (scripts/setup.sh, min_free_fraction).
2. Pick the scope (table above) in the config.
3. Move the existing 4 completed inboxes or start fresh; the backup/restore
   steps in README.md cover copying the mail directory.

## Other open items

- The packaging work is one large commit (b4d37b4) plus a deployment commit;
  split into logical commits before treating the repo as reviewable.
- REPORT.md, REPORT-DEPLOY-63.md and HANDOFF.md are agent notes committed in
  the repo; move them out of history when cleaning up.
