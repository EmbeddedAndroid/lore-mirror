# Opt-in code catalog

The current `.63` HDD/4 GB-class host remains the target. This feature does not
require an SSD, RAM upgrade, separate worker, new credentials, or public search
port. Mail-only configurations are unchanged: absent `code` means disabled and
an empty catalog. The supplied catalog is also **disabled**. No code mirror is
created merely by loading a configuration or invoking a read tool.

This document covers defining and validating the catalog, including the
`index_refs` an optional index pilot may select; it does not fetch, read or
search code.

## Add a catalog

`config/code-trees.json` is a configuration fragment, not a replacement for an
existing mail/deployment configuration. Copy its `code` object into the existing
configuration and review its trees, refs, and resource limits. Keep
`enabled: false` until the operator is ready to fetch. For a smaller pilot,
explicitly retain only the desired tree entries. IDs are durable public API
identities, not labels to rename casually.

Both `load_config` and `prepare_config` use
`normalize_code_config(config.get("code"), data_dir=config["data_dir"])`.
Normalization is offline, idempotent, and has no filesystem mutation or network
side effects. Invalid configuration fails with `invalid_config`, including a
malformed section that is currently disabled. Actual upstream existence is
checked by the coordinator, not during API startup.

### Code section

- `enabled`: boolean, default false. True requires at least one explicit tree.
- `root_dir`: defaults to `data_dir/code`; normalized to an absolute path.
- `trees`: at most 32 logical repositories. Put two branches of the same
  upstream in one entry, not two duplicate mirrors.
- `max_refs`: positive integer, at most 10,000; default 10,000.
- `min_free_fraction`: finite number in `[0.30, 1)`, default 0.30.
- `limits`: bounded resource settings below. Unknown fields are rejected rather
  than silently ignoring a misspelled safety limit.

### Tree entry

- `id`: 1-64 lowercase ASCII letters/digits, `_` or `-`, beginning alphanumeric.
- `name`: optional human-readable label, defaulting to the ID.
- `url`: trusted HTTPS `.git` URL. Kernel repositories are restricted to
  `git.kernel.org/pub/scm/linux/kernel/git/`; U-Boot to the `/u-boot/` namespace
  on `git.u-boot-project.org` or legacy `source.denx.de`. Credentials, custom
  ports, query strings, fragments, encoded/traversing paths, and other hosts
  are rejected. A hostname or manifest cannot come from an API request.
- `refs`: nonempty list of exact `refs/heads/...` or `refs/tags/...` names.
  No `HEAD`, short names, arbitrary object IDs, revision expressions, refspecs,
  or branch wildcards inside `refs`. Duplicate entries are rejected.
- `default_ref`: required exact member of `refs`; never silently replaced.
- `tag_patterns`: optional list of `{ "pattern": "refs/tags/prefix*", "keep": N }`.
  Only a literal tag prefix plus one final `*` is allowed. `keep` is explicit:
  1-1000, or `null` to select all matches subject to `max_refs`. Selected tags
  are sorted by descending ref name, not semantic version or commit time.
  This gives chronological ordering for `next-YYYYMMDD`. `v*` includes release
  and release-candidate tags. It does not enable historical indexing.
- `branch_patterns`: optional mirror-only numeric-release selectors, e.g.
  `{ "pattern": "refs/heads/arm64-for-*", "keep": 1 }`. Only a nonempty literal
  head prefix and one final `*` are allowed. `keep` must be the integer 1.
  The coordinator accepts only a suffix matching dotted nonnegative integers,
  compares version components numerically (`7.10` is newer than `7.9`), and
  uses a deterministic name tie-break for numerically equal versions. Names
  with `-rc`, `-fixes`, or any other nonnumeric suffix do not match. No match is
  an explicit error, not permission to replace the selector with `master`.
  The newest selected branch per prefix is mirrored, **not indexed**. Prior
  protected generations may retain older objects; this is not automatic GC.
- `index_refs`: optional exact subset of `refs`, default empty. It is separate
  from tag retention. `index.pilot_ref` must name one of them (`code-index.md`).
- `fetch_mode`: `grokmirror_manifest` for kernel.org; `conditional_refs` for
  U-Boot. Omission uses the matching default; an inconsistent mode is rejected.
- `manifest_url`: for kernel only, fixed to
  `https://git.kernel.org/manifest.js.gz` (the default).
- `manifest_key`: for kernel only, exactly the URL's repository path, e.g.
  `/pub/scm/linux/kernel/git/torvalds/linux.git`. It cannot point at another
  repository's fingerprint.
- `interval_seconds`: default 3600. Kernel minimum 300; U-Boot minimum 3600;
  maximum seven days. These are eligibility intervals, not freshness promises.

The legacy/current U-Boot hosts are recognized as the same repository identity
for duplicate detection, but the configured URL is not silently redirected or
rewritten. Full-history reachability is distinct from advertising every upstream
ref: only the selected refs are published.

## Requested upstreams and verification

Checked on **2026-10-21**. This is provenance for a reviewed example, not a live
availability guarantee. Public web/cached mirror evidence can lag a Git server;
the coordinator must verify the actual advertised refs before publishing.

| ID | Repository / selected refs | Evidence and caveat |
| --- | --- | --- |
| `linux-mainline` | `torvalds/linux.git`: `master`; all `v*` release/RC tags | [Official mirror](https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux/) identifies the kernel.org source, `master`, and release tags. Only `master` is a future index target. |
| `linux-next` | `next/linux-next.git`: `master`; latest three `next-*` tags | [Kernel documentation](https://www.kernel.org/doc/man-pages/linux-next.html) gives the source URL; [official mirror](https://kernel.googlesource.com/pub/scm/linux/kernel/git/next/linux-next/) exposes `master` and dated tags. Only `master` is a future index target, never all daily tags. |
| `linux-stable` | `stable/linux.git`: `linux-6.18.y`, `linux-6.12.y`, `linux-6.6.y`, `linux-6.1.y`, `linux-5.15.y`, `linux-5.10.y` | [Kernel.org active release policy](https://www.kernel.org/releases.html) lists these six longterm series at review time; [official mirror](https://kernel.googlesource.com/pub/scm/linux/kernel/git/stable/linux/) identifies the source. No non-LTS branches are selected. The example is intentionally pinned, not a dynamic promise of “current forever”; reassess when upstream LTS policy changes. |
| `linux-qcom` | `qcom/linux.git`: exact `for-next` for reads/indexing; newest numeric `arm64-for-*`, `drivers-for-*`, `clk-for-*` branches mirror-only | The [official mirror listing](https://kernel.googlesource.com/pub/scm/linux/kernel/git/qcom/linux/+refs) observed during review exposed `for-next`, `arm64-for-7.4`, `drivers-for-7.4`, and `clk-for-7.4`. Numeric selectors track future versions; live canonical Git confirmation still occurs at synchronization, since canonical web requests returned 403. |
| `u-boot` | `u-boot/u-boot.git`: `master`, `next` | [Current source documentation](https://docs.u-boot.org/en/latest/build/source.html) names the new `git.u-boot-project.org` host. The [release-cycle page](https://docs.u-boot.org/en/latest/develop/release_cycle.html) describes `next` as closed until 2026-11-09; that alone does not prove the branch is deleted. Live refs were not independently verified because the canonical web host denied access. |
| `u-boot-qcom` | `u-boot/custodians/u-boot-snapdragon.git`: `qcom-main`, `qcom-next` | [Maintainer's 2026-10-01 pull request](https://mail-archive.com/u-boot%40lists.u-boot-project.org/msg07602.html) explicitly names the new canonical URL and `qcom-next`. Both requested names remain unchanged in the example. Current branch existence is **unverified**, not claimed absent; canonical web access was unavailable. |

For kernel trees, the mode consumes the upstream grokmirror-style manifest as a
change signal; it is not a claim that this project installs/runs grokmirror or
uses its object-pool deduplication. Upstream's [configuration example](https://github.com/mricon/grokmirror/blob/master/grokmirror.conf)
identifies the manifest URL and conditional-request model. Pooling, alternates,
and automatic repack/GC are not part of this catalog contract.

**Missing refs fail explicitly.** A valid declaration is not readiness. For
Qualcomm, `for-next` is the integration and index target and the three latest
numeric release branches are mirror-only. Do not silently replace any later
missing ref or unmatched selector with `master` or another branch. Resolve
changed intent with the operator and keep unrelated mail serving normally.

## Conservative limits

Values are bytes unless a name says otherwise. These are guardrails, **not**
measured production requirements or a promise a Linux clone fits. In particular,
the 8 GiB repository / 2 GiB per-fetch growth defaults may block a large initial
Linux fetch. Review available disk and measured costs before increasing them;
never weaken the 30% free-space reserve. The current host stays unchanged.

| Setting | Default | Validation maximum |
| --- | ---: | ---: |
| `fetch_timeout_seconds` | 900 | 3600 |
| `fetch_memory_bytes` | 512 MiB | 1 GiB |
| `repository_bytes` | 8 GiB | 1 TiB |
| `max_fetch_bytes` | 2 GiB | 64 GiB |
| `command_output_bytes` | 1 MiB | 16 MiB |
| `manifest_bytes` | 4 MiB | 16 MiB |
| `manifest_expanded_bytes` | 32 MiB | 64 MiB |
| `query_timeout` | 15 seconds | 120 seconds |
| `query_output_bytes` | 4 MiB | 16 MiB |
| `query_memory_bytes` | 256 MiB | 2 GiB |
| `max_file_bytes` | 1 MiB | 16 MiB |
| `max_line_bytes` | 4096 | 8192 |
| `scan_limit` | 1000 | 10000 |
| `max_sessions` | 16 | 64 |
| `session_bytes` | 8 MiB | 32 MiB |
| `cursor_ttl` | 900 seconds | 3600 seconds |

Code cursor capacity, bytes and expiry use an independent pool; code requests
cannot consume or evict mail cursor sessions. All code callers use the normalized
defaults in one place, and exact head/tag syntax uses the same validator.

All are positive integers except `query_timeout`, which also accepts a positive
finite fraction for small test budgets. `manifest_expanded_bytes` must be at
least `manifest_bytes`. Validation maxima are not suggested operating values on
the 4 GB host. `max_fetch_bytes` is sampled repository growth, **not an exact
network-byte quota**. Existing response-envelope limits still apply separately.

## Offline checks

Run `PYTHONPATH=src python -m unittest discover -s tests -p 'test_code_config.py' -v`.
The catalog tests use no production Git fetches or public network access. They
cover disabled defaults, idempotence, no load-time side effects, hostile URLs,
path-safe IDs, exact refs, bounded tag intent, default/index subset checks,
canonical manifest binding, duplicate repositories, cadence, resource bounds,
numeric release-selector intent, and the example's integration-versus-mirror-only scope.
