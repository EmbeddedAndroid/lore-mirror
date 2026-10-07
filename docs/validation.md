# Validation status

Checked 7 October 2026. The approved four-inbox full-history pilot is indexed, and its four-list incremental reconciliation succeeded. The genuine elapsed-time observations are complete. The live hourly daemon has a separate network/runtime limitation described below. This is not a completed full-lore mirror.

## Passed

- 150 tests with native dependencies enabled, no skips, in 40.8 seconds (16:18 UTC final verification).
- Native offline E2E imports three committed real U-Boot messages into fresh public-inbox v2/basic inboxes and a shared full extindex.
- 15 stored golden native searches cover sender, subject, body, dates, List-Id, diff paths, exact IDs and stable patch IDs.
- Exact raw bytes, SHA-256 and Git object IDs survive fixture replay. Distinct transport-header copies remain individually readable even when public-inbox deduplicates their search document.
- Exact secondary Message-ID resolution through local public-inbox metadata, with raw-header validation, alias provenance and conservative thread/series completeness.
- Pinned Linux/U-Boot root MAINTAINERS catalogs pass offline size/hash and project-selection tests.
- Real thread/message/series pagination, explicit unknown-ID errors, local-only query behavior, provenance and read-only archive hashes.
- MCP initialize, tools/list and calls through the official SDK; all eight mail tools and two issue-read tools are read-only, and only issue.report is writable.
- HTTP/JSON boundaries, Host/Origin checks, output/input budgets, subprocess deadline/pipe limits, injection resistance, issue idempotency, cursor binding and index-generation invalidation.
- Sync scope/disk guards, failed-index recovery flags and recorded hourly scheduler state.
- Physical-list/class filters execute before native candidate caps, including unions and scoped raw variants; freshness is unknown without a successful checkpoint and becomes stale after two sync intervals.
- Exact-ID regressions reject X-Alt-only values, truncated terms and comment-only identifiers; primary hits cannot hide secondary raw aliases.
- Docker verification entrypoint does not require host-side application Python dependencies (mocked command-path test, not a Docker runtime test).
- Live localhost TCP smoke passed 19 operations against the completed four-list index, including all mail tools and local issue APIs. A second 20-operation run also passed during a recorded synchronization failure, adding an actual MCP tools/call native search; archive reads remained available and synchronization status reported the failure. All responses stayed within byte budgets. See the two pilot-smoke JSON records.
- Fresh hash-locked Python installation, package consistency, Python 3.11 wheel/hash validation, Python compilation, shell syntax and CLI checks.

Reproduce the entire test suite on a host with native tools:

    source /path/to/native/env.sh
    LORE_NATIVE_E2E_REQUIRED=1 scripts/verify.sh

Or run the explicit native fixture gate:

    LORE_NATIVE_ENV=/path/to/native/env.sh scripts/test-native-e2e.sh

## Not run or unavailable

- Docker and Compose build/runtime enforcement: Docker is unavailable in this executor. Dockerfiles and Compose are checked in, but image construction and container egress/mount enforcement still require the Docker verification command on a Docker host.
- Live hourly daemon: its first recorded manifest fetch failed at the managed proxy CONNECT stage with `<urlopen error Tunnel connection failed: 403 Forbidden>`. The exact policy cause is unverified. The prior successful four-list checkpoint remains readable. Scheduler/backoff and staleness behavior are tested; a successful live hourly daemon cycle is not claimed. The sole authorized status-poll retry returned `Unknown process id 12807`, and no coordinator lock was held. Its recorded future retry is not a verified scheduled run. See final pilot measurements for scheduler evidence.
- LEI runtime: this executor refuses AF_UNIX/SOCK_SEQPACKET. Exact observed stderr was `socket: Operation not permitted at /workspace/shared/lore-mirror-data/install/bin/lei line 83.` This was a runtime capability restriction, not a reviewer rejection. It was not bypassed. The tested direct adapter calls the same pinned public-inbox search implementation without that socket.
- Live lore website parity: intentionally not performed. Golden answers were captured from the completed local native mirror snapshot; subsequent tests are offline. This does not establish parity with a live lore website at a later time.
- The approved scope is four inboxes: linux-arm-msm, u-boot, devicetree-spec and u-boot-amlogic. Full-manifest totals remain unestablished; see the evolving pilot measurements. A genuine greater-than-one-hour observation of the two small lists is complete, with unchanged upstream fingerprints and zero Git/basic-index growth. Shared-index bootstrap growth is excluded. Do not read bootstrap deltas as hourly growth or historical corpus counts as current measurements.

## Deliberate v1 limits

Applied status is unknown; no named code-tree evidence has been ingested. Series discovery is thread-based with explicitly labeled detached candidates, and cannot guarantee discovery of retitled/off-list rerolls. Complex MAINTAINERS regex rules are not executed. Overlong malformed upstream Message-IDs can be truncated by public-inbox's index while original Git mail remains preserved. Authentication is delegated to an existing trusted private proxy; default access is localhost.
