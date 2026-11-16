# FOIA Aggregator — verified handoff status

**Snapshot:** November 15, 2026, second checkpoint (after PRs #75–#79; refreshed from the GitHub REST API). Earlier sections are retained as dated history. Refresh all claims before execution; this is a recorded checkpoint, not a live dashboard.

## Authoritative documents
- [Full development plan](DEVELOPMENT_PLAN.md): 10 approved operational FA units plus 14 added FC broad-coverage deliverables.
- [Agent operating rules](../AGENTS.md).
- [README](../README.md), [corpus expansion/storage](CORPUS_EXPANSION.md), [source access](SOURCE_ACCESS.md), [deployment](PRESENTATION_DEPLOYMENT.md), [admin](ADMIN_DASHBOARD.md).
- [Engine Room certification audit](https://github.com/neophilism/Engine-Room/issues/71) (external repository; not modified here).

## FA-001 recovery-audit hardening — October 17, 2026

The recovery audit now compares source/agency identities, document attribution,
known metadata, per-document object associations, extracted-text fingerprints and
search-row fingerprints, not only document counts or independent URL/key sets.
All evidence is read in one read-only SQLite transaction. Missing files, corrupt
or unsupported schemas, orphaned relationships and downloaded records without
verifiable B2 identities fail closed. Surrogate ID changes and previously unknown
metadata enrichment do not imply lost evidence. Divergent known values require
review rather than automatic selection of either database.

The existing `candidate_appears_safe_for_archived_objects` promotion field now
also requires preservation of that evidence and no known object-size mismatch.
Reports contain counts and booleans, not document bodies or signed URLs. The
candidate cannot be used as its own restore destination, and comparison restore
targets must be fresh (`overwrite=False`). No checkpoint is published by audit.

Nineteen new credential-free SQLite fixture tests passed locally. Full repository
CI and PR head evidence are tracked on the mapped FA-001 follow-up PR, not inferred
from these fixtures. Existing pending PRs #47 (promotion), #48 (upload fallback)
and #41 (single writer) remain separate work and were inspected, not duplicated.
The gateway cannot inspect/merge those legacy branches as the current mapped work
branch. Never bypass it through direct GitHub mutations.

Still pending: live B2 manifest/restore and selected-candidate audit, explicit
single-writer coordination, safe promotion through authorized tooling, actual
post-promotion restore, and deployed replica verification. Listing size alone
does not prove object-content hashes; no live B2/data synchronization, production
acceptance, migration, paid resource or schedule activation is claimed here.

## Baseline findings

### FA-002 source follow-up — October 17, 2026

The mapped FA-002 follow-up resumes pending PR #48's native fallback and adds
read-back/full-hash/SQLite verification before any retention pruning. It also
adds trusted B2 endpoint checks, redirect denial, authorization fail-closed
behavior, bounded network/gzip reads, fresh keys, atomic restore replacement and
safe pagination. See [CHECKPOINT_SAFETY.md](CHECKPOINT_SAFETY.md) for exact scope,
test requirements, processing limits and provider references. PR #48's old code
must not overwrite these guards when reconciling branches. PR #51 audit changes
remain independent and must be retained when resolving handoff-document conflicts.

No live B2 acceptance, recovery promotion, schedule activation, deployed replica
update or unit completion is established. Actual full-suite test/build results
belong to the mapped PR's current committed head. Existing PR #47 and PR #41
remain subject to their audit, upload, single-writer and live acceptance gates.
- Code already has source discovery, curated and IC coverage, PAL/generic reading-room crawler, safe downloader, OCR/legacy Office text extraction, SQLite FTS5, B2 storage/recovery, FastAPI public search/admin, and manual/scheduled GitHub workflows. **Functionality/production success were not retested in this documentation change.**
- The approved .exechub/project.yml remains a **10-unit** operating expansion. Additional FC-01..14 scope is explicitly subsequent user-requested expansion; track its count separately with audited PR mappings.
- Repository source files include .github/workflows/{expand-corpus,refresh-corpus,preserve-recovery-artifact,audit-recovered-database}.yml. Autonomous scheduled refresh is intentionally gated by repository variable FOIA_AUTONOMOUS_REFRESH_ENABLED. Do not interpret the presence of a schedule as proof runs execute.
- Current documented safety caps: 8,500,000,000 bytes archived originals, 9,000,000,000 bytes actual versioned B2 target, within the nominal free 10 GB. Actual consumption/latest verified checkpoint at this review **not independently measured**, so do not assert corpus size, download count or headroom.
- Presentation Render free service is ephemeral/read-only and must load a separately verified B2 metadata checkpoint. Web heartbeat does not mean authoritative crawling happened.
- The paid future ~1 TB tier requires explicit user approval; meanwhile metadata/source catalog coverage should expand without mislabeling remote-only records as archived.

## Fourth checkpoint — 2026-11-16 (release-date sources, newly found; PR #86)

#86 (schema **15**, additive): `documents.published_date_source` (page > header > metadata), `documents.newly_found_at`, `reading_rooms.baseline_completed_at` (backfilled from `last_successful_crawl_at`). Window crawls download items dated in the window or first seen after a completed baseline; `/recent` and `/api/v1/records` default to "dated or newly found" with per-row date source. #85 recorded #81–#84.

**Live probe (same 10 sources, dry run, HEAD ≤200/source, 2 s/host):** 226 links; **223 got a Last-Modified date** (3 undated: CIA 2, State 1), 0 page dates. 1 link fell in the last 7 days — an FBI Vault footer link to fbijobs.gov (`eeoc_policy.pdf`, file path 2022-07, Last-Modified 2026-11-15), i.e. Last-Modified reflects server/CDN changes and is a weak proxy. 8 sources completed a baseline in the run, so newly found = 0 by design; "newly found" results appear only on later crawls that discover new links. MuckRock still BLOCKED (Cloudflare 403, no token).

## Third checkpoint — 2026-11-16 (date-range releases and requests; PRs #82–#84)

Owner priority: "everything released in the last week". `main` after #84: full suite OK (474 tests), schema version **14** (additive `third_party_requests`). Main stays deployable without B2.

| PR | What | Level |
| --- | --- | --- |
| #82 | `main.py run --since-days N` / `--published-since/--published-until` / `--include-unknown-dates`; `refresh-corpus.yml` dispatch inputs (default 7 days, single-writer group); labeled Posted/Released text dates; `live-probe.yml` | code + CI live probe |
| #83 | `/recent?days=7`, `/recent?since=&until=`, `/api/v1/records?published_since=&published_until=` | code; no real data loaded |
| #84 | FC-04 request listing: `main.py muckrock-requests --since-days 7`, `/requests`, `/api/v1/requests`; token-gated (`MUCKROCK_API_TOKEN`) | code; live access BLOCKED |
| #81 | draft duplicate of #82, closed unmerged | — |

**Live acceptance probe (GitHub runner, 2026-11-16, dry run, no downloads, no B2):**
* Window crawl through the real engine path, 10 public reading rooms, 3 pages/source, 2 s/host: 8 reached (DOJ OIP, CIA, FBI Vault, DHS, NARA, State, DOL, ODNI); NSA **403 BLOCKED**; EPA root URL **404**. 226 document links cataloged, **0 with a detectable published date** → 0 in the 7-day (and 30-day) window. DHS/NARA listings show only unlabeled document dates (deliberately not used as publication dates).
* MuckRock: robots allows `*` (Content-Signal ai-train=no); every API URL returns **Cloudflare 403 "Attention Required"** to runner IPs → `muckrock-requests` live run recorded **BLOCKED** (no token configured). API shape documented from MuckRock's public source in [FC04_MUCKROCK_ADAPTER.md](FC04_MUCKROCK_ADAPTER.md).

**Not yet true:** no last-week releases are in the live site. Needed: a windowed `refresh-corpus` dispatch with B2 secrets (single writer), a published checkpoint the Render service loads, and sources that expose publication dates (most probed ones do not; per-source date parsers or document-page dates are the next step). MuckRock listing needs an owner API token and a network Cloudflare does not block.

## Second checkpoint — 2026-11-15 (downstream asks + FC-05..FC-14 foundations)

`main` after #79: **445 tests, OK**; schema version **13**. No open PRs besides the one carrying this document. Main stays deployable without B2 (all new tables are additive; web reads guard missing tables).

**Downstream asks (#75), all additive; FC-13 records and the pinned `catalog-record.v1.schema.json` (SHA-256 `1e8abc95…f34`) are unchanged:**
1. `foia-document-text-export/1` — text + `text_sha256` + extraction provenance (`/api/v1/export/document-text`).
2. `foia-request-log-export/1` — FC-03 rows without requester name/organization/raw cells (`/api/v1/export/request-log-rows`).
3. `foia-log-coverage/1` — per log/agency/component/year coverage in the exact field shape of Send_FOIA_Log_Requests' `sfl-log-coverage-proposal-1` (`/api/v1/export/log-coverage/{manifest.json,coverage.jsonl}`). SFL's validator accepts it once it adds the version string `foia-log-coverage/1`; `fc14_review` stays `absent`.
4. `POST /api/v1/ingest/sfl-bundle` — bearer `FOIA_INGEST_TOKEN` (≥24 chars; 404 when unset). Verifies `sfl-interchange/1`, stages to an inbox, returns deterministic acks; `main.py import-sfl-inbox` imports under the writer lock (migration 11). On Render free the inbox is ephemeral (the ack says so).
5. Every manifest names `producer.commit` (`FOIA_BUILD_COMMIT` → `RENDER_GIT_COMMIT` → `GITHUB_SHA` → git; `null` if unpinned). FC-13 manifest adds `contract_revision: "1.1"`.
Details: [EXPORT_EXTENSIONS.md](EXPORT_EXTENSIONS.md).

| Unit | PR | Level | Pending |
| --- | --- | --- | --- |
| FC-08 | #76 | relations without merging, related versions UI, relations export | agency reorganizations, tracking-ID joins, real corpus run |
| FC-12 / FC-14 | #77 | `/coverage`, `/api/v1/coverage`; draft corpus review generator | real data; owner FC-14 acceptance |
| FC-05 / FC-06 / FC-07 | #78 | backfill windows, sitemap/JSON adapters, content-type safety | live samples; downloader wiring |
| FC-09 / FC-10 / FC-11 | #79 | acquisition queue, leased work queue, migration gate | crawler wiring after single-writer acceptance; FC-11 needs spend approval |

All FC PRs this round are recorded as `contributingPrs` in `registry/prs.json`: fixture-level code is not acceptance.

Migrations: 11 SFL interchange, 12 document relations, 13 acquisition/work queues.

## Integration checkpoint — 2026-11-15

**Open PRs at checkpoint:** only the registry/handoff refresh PR that carries this document. `python -m unittest discover -s tests` on `main` after #73: **385 tests, OK** (Python 3.12 venv). Status levels below follow AGENTS.md: *code complete / integrated* is not *data synchronized, deployed, or real coverage validated*.

| Unit | PRs (merged) | Level reached | Still pending |
| --- | --- | --- | --- |
| FA-001 | #46, #51, #47 | integrated; #47 promotion workflow is **inert** (dispatch-only, `FOIA_RECOVERY_PROMOTION_ENABLED` + typed `PROMOTE`) | live B2 audit + monotonic promotion |
| FA-002 | #40, #43, #44, #52, #48 (docs) | integrated; native B2 fallback landed hardened in #52 | live restore/retention acceptance |
| FA-003 | #41 | integrated; all B2 writers share `foia-b2-single-writer`; scheduled refresh stays gated OFF | two observed serialized scheduled runs |
| FA-004 | #62 (draft #53) | integrated; recovery audit accepts additive schema ≥ 6 | live run receipts |
| FA-005..FA-009 | #63–#67 (drafts #54–#58), #69 | integrated | live census, telemetry/alert evidence, release acceptance |
| FA-010 | #68 (draft #59) | integrated with FA-004 journal; live crawls need a positive host delay | production scale acceptance |
| FC-01 / FC-02 | #70 | code complete, fixtures | live census and sampled full traversal |
| FC-03 | #71 | code complete, fixtures | real official logs; PDF/scanned logs |
| FC-04 | #73 (contributing) | MuckRock adapter, fixtures, inert | other archives, live sample, binary dedupe |
| FC-13 | #72 (contributing) | export contract/API/snapshots | depends on FC-08 and FC-12 |
| FC-05..FC-12, FC-14 | — | not started | — |

Draft PRs #53–#59 were closed and reopened as ready PRs #62–#68 on the same branches because the REST API cannot un-draft a PR and GraphQL was unavailable; each draft links to its replacement. #61 fixed the PR-registry CI after the repository moved to `Justice-For-All-Movement/FOIA_Aggregator`. `.github/deployment-monitor.json` still names the old owner (changing it triggers an external reusable workflow); follow-up.

Schema is now version 10: 8 = enumeration cursors (FC-02), 9 = request logs (FC-03), 10 = third-party releases (FC-04). All are additive and accepted by the FA-001 recovery audit.

### Deployment (no Render resources were created or changed)
The Docker web service now starts without B2 (#69): the entrypoint no longer crash-loops, the app serves an empty placeholder index at `<db>.unconnected.db` (never at `FOIA_DB_PATH`, so a later verified bootstrap is not blocked), every page shows "Archive not connected", and `/healthz` stays 200 with an `archive` status object. Binding: `uvicorn ui.server:app --host 0.0.0.0 --port ${PORT:-8000}`. `render.yaml` declares `B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET`, `B2_REGION`, `B2_ENDPOINT_URL`, `FOIA_ADMIN_PASSWORD`, `FOIA_PUBLIC_BASE_URL` as `sync: false`. Required vs optional variables: [PRESENTATION_DEPLOYMENT.md §10](PRESENTATION_DEPLOYMENT.md#10-deploying-before-b2-is-connected-render-free-web).

### Blockers (need the owner)
- B2 credentials/acceptance: live checkpoint restore, recovery promotion (#47), first scheduled serialized runs (#41), storage measurement against the 8.5/9.0 GB caps.
- Render: creating/redeploying the service and entering secrets is an owner action.
- Live source access: FC-01/02 census and enumeration, FC-03 official logs and FC-04 MuckRock sampling require bounded live runs that respect robots, rate limits and terms.

## Next unblocked implementation work
- **Without access or spend:** wire FC-07 bundle expansion and FC-09 metadata-only mode into the downloader/engine behind flags; FC-08 agency-reorganization joins; downstream (SFL) adds `foia-log-coverage/1` to its supported versions and pins a producer commit.
- **With B2 credentials:** read-only audit → `restore-db` to a separate file → dispatch #47 promotion only after monotonic proof → enable the gated refresh (#41) and observe two serialized runs.
- **With later explicit paid-tier approval:** FC-11 storage/search migration.

## Runbook
    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v
    python scripts/source_census.py --probe --output-dir source-census-results
    python main.py run --dry-run true --max-docs-per-source 10
    python main.py reconcile-b2
    python main.py restore-db
    python main.py source-inventory --census-json source-census-results/source-census.json
    python main.py ingest-request-log <local-log.csv> --source-url <official URL> --agency <slug>
    python main.py export-catalog <output-dir>
    python main.py ingestion-status
    python main.py export-dataset {document-text|request-log-rows|document-relations} <dir>
    python main.py export-log-coverage <dir>
    python main.py import-sfl-inbox
    python main.py analyze-identity
    python main.py corpus-review --output corpus-review.json

B2 commands require correctly configured server-side environment/secrets and must never print keys. Do not initiate a live archiving wave without an exclusive writer, actual remaining capacity and verified checkpoint.

## Release/handoff exit criteria
Every plan unit has explicit actual PR mapping and test evidence; one authoritative writer; two observed successful serialized scheduled runs; non-regressing B2 DB checkpoints and full object manifest; diverse primary/third-party FOIA source census and historical backfill status; date/format/body/OCR search and signed downloads on the deployed site; honest current storage/coverage dashboard; cost and API restrictions documented. Until then, do not certify broad nationwide FOIA archive coverage or unblock major dependent FOIA Bias Analysis / Send FOIA Log Requests buildout.

## FA-004 implementation update — 2026-10-18

PR #53 now implements schema-7 run/source receipts, interrupted-wave source resume, same-host writer exclusion, fair bounded source rotation and safe seed/expansion restart preservation. See [ingestion recovery](INGESTION_RECOVERY.md). This is software progress, not production/B2 acceptance; page cursors and live verification remain pending.
