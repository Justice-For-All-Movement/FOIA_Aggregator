# Export extensions and Send_FOIA_Log_Requests interchange (2026-11-15)

All additions are **parallel contracts**; `foia-catalog-export/1` records and
`schemas/catalog-record.v1.schema.json` are byte-for-byte unchanged (SHA-256
`1e8abc95…f34`, pinned by downstream consumers at `ca822fa`). The FC-13 manifest
gains only additive fields: `contract_revision: "1.1"` and `producer`.

## Producer identity (all manifests)
`producer = {name, repository, commit, commit_source}` from `FOIA_BUILD_COMMIT`,
`RENDER_GIT_COMMIT`, `GITHUB_SHA`, else `git rev-parse HEAD`. Only a full 40-hex
SHA is reported; otherwise `commit: null` (consumers must treat it as unpinned).

## Contracts
| Contract | Schema | API | CLI |
| --- | --- | --- | --- |
| `foia-document-text-export/1` | `schemas/document-text.v1.schema.json` | `GET /api/v1/export/document-text?limit=1..100&cursor=` | `export-dataset document-text <dir>` |
| `foia-request-log-export/1` | `schemas/request-log-row.v1.schema.json` | `GET /api/v1/export/request-log-rows?limit=1..1000&cursor=` | `export-dataset request-log-rows <dir>` |
| `foia-log-coverage/1` (contract `foia-aggregator-export`, dataset `log_coverage`) | `schemas/log-coverage-{manifest,record}.v1.schema.json` | `GET /api/v1/export/log-coverage/manifest.json`, `…/coverage.jsonl` | `export-log-coverage <dir>` |

Schemas are also served at `/api/v1/export/schemas/<name>`. Paged datasets use
snapshot-bound cursors (409 on change), 400 on bad limits and the shared
per-client rate limit.

* **Document text**: indexed text only, `text_sha256` over the exported UTF-8,
  extraction status/method, truncation flag, and the archived binary's SHA-256
  when preserved; pages are also capped at 8 MiB of text. Same exclusions as FC-13.
* **Request-log rows**: FC-03 rows with log provenance (log ID, source URL, raw
  SHA-256, format, agency), dates, disposition, component, exemptions, released
  reference, field confidence and warnings. Never exported: requester name,
  requester organization, raw cells.
* **Log coverage**: one record per (log, agency, component, calendar year of
  received date), field-for-field identical to Send_FOIA_Log_Requests'
  `sfl-log-coverage-proposal-1`, so its validator only needs to accept the
  version string `foia-log-coverage/1` (verified locally against SFL
  `a3634b9`). `availability` is `full` only for a finished year with every
  required field mapped and no >31-day gap without a received request;
  otherwise `partial`. Undated rows are not counted. `fc14_review` stays
  `absent` until an FC-14 review is accepted.

## Receiving SFL-12 bundles
`POST /api/v1/ingest/sfl-bundle` with `Authorization: Bearer $FOIA_INGEST_TOKEN`
(disabled — 404 — unless the token is set and at least 24 characters) and a
JSON envelope `foia-sfl-ingest/1`:
`{"envelope_version", "manifest_json": "<exact manifest.json text>", "files": {...3 jsonl...}, "artifacts": {"<sha256>": "<base64>"}}`
(32 MiB cap; `envelope_from_directory()` builds it from a bundle directory).

The bundle is verified as SFL's `import_bundle` does (manifest SHA-256 = bundle
identity, per-file hashes/counts, artifact hashes/sizes) plus strict per-file
field allowlists that reject any requester data. The response is an
acknowledgment for SFL's `record_ack`: deterministic `ack_id` per
bundle/status, `status` `accepted` (201 new, 200 duplicate) or `rejected`
(422 with `detail`), `durability` of the inbox, `consumer_commit`.

The web process **does not write the archive DB**: it stages the bundle under
`FOIA_INGEST_INBOX/<bundle_sha256>/`. The authoritative writer runs
`python main.py import-sfl-inbox` (single-writer lock, migration 11 tables
`sfl_bundles`, `sfl_components`, `sfl_requests`, `sfl_fulfilled_logs`);
CSV/TSV/XLSX fulfilled logs also flow into FC-03 with source
`urn:sfl:<request_id>:<sha256>`. On Render free the inbox is ephemeral, which
the ack states, so a durable import needs a persistent writer host.
