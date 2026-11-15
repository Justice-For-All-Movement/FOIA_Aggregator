# FC-13 research-ready export contract and API

Status (2026-11-15): **contract, API and snapshot tooling complete with
fixtures**. Exports reflect whatever archive snapshot the service has loaded;
they say nothing about corpus completeness (that is FC-12/FC-14).

## Contract: `foia-catalog-export/1`

* JSON Schema: [`schemas/catalog-record.v1.schema.json`](../schemas/catalog-record.v1.schema.json)
  (also served at `/api/v1/export/schema`). Example page:
  `tests/fixtures/export/catalog_v1_page.json` (golden test).
* `record_id` = `rec_` + 24 hex of SHA-256 over the normalized document URL —
  stable across database rebuilds (SQLite rowids are not).
* `sources[]` carry FC-01 `source_id`s and provenance; `preservation.status`
  is `archived` or `metadata_only` (remote pointer); `sha256`/`file_size` are
  only present for archived binaries; `text` reports index status/method.
* Never exported: B2 storage keys, local paths, storage backend, download error
  text, request-log requester names. No synthetic enrichment.
* Breaking changes require `foia-catalog-export/2`; additive fields are noted here.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/export/schema` | JSON Schema for one record |
| `GET /api/v1/export/manifest` | schema version, `snapshot_id`, counts (records, archived, metadata-only, text-indexed, agencies, sources), excluded fields, license note, archive connection status |
| `GET /api/v1/records?limit=1..500&cursor=…` | ordered page + `next_cursor` |

Cursors are opaque and bound to the `snapshot_id`; if the archive changes
mid-export the API returns `409` so clients restart instead of silently mixing
snapshots. Bad limits return `400`. A per-client fixed window limit
(`FOIA_EXPORT_RATE_PER_MINUTE`, default 120) returns `429` with `Retry-After`.

## Snapshot files

`python main.py export-catalog <dir>` (read-only SQLite) writes
`records-00001.jsonl …` and `manifest.json` with per-file SHA-256 and record
counts, then runs `verify_snapshot()`, which a downstream consumer can also
use to replay a downloaded snapshot (hashes, counts, duplicate IDs).
