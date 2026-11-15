# FC-01 source inventory and FC-02 enumeration cursors

Status (2026-11-15): **code complete with fixture tests**; not yet run against
live sources. Live census/enumeration evidence remains FC-01/FC-02 acceptance
work and must respect robots, declared rate limits and access controls.

## FC-01 — `foia_archive/source_inventory.py`

`python main.py source-inventory --census-json source-census-results/source-census.json`
writes `source-inventory.json` (version `source-inventory/1`). It merges the
FOIA.gov component census, the 18 Intelligence Community elements and the
registry of recognized third-party release archives into one list keyed by a
stable `source_id` (`src_` + 16 hex of SHA-256 over the normalized URL).

Per source: provenance (`official` / `third_party`, official wins on overlap),
publisher, source types, agency/component/IC attributions, access status
(`ACTIVE`, `BLOCKED` for persistent 403/CAPTCHA, `FAILING`, `UNVERIFIED`,
`HISTORICAL`), enumeration state from FC-02, and database counts
(discovered / archived / indexed / remote-only). Counts that were not measured
are `null`, never zero. Database sources not explained by any input are listed
in `database_sources_not_in_inventory`. The command opens SQLite read-only.

## FC-02 — `foia_archive/enumeration.py` (schema migration 8)

`enumerate_source()` traverses one source (or date window) through an
adapter-supplied `fetch_page(cursor) -> Page`, persisting the cursor after
every page in `source_cursors` and de-duplicating item keys per source in
`source_enumeration_items`.

* page budget reached → `IN_PROGRESS`, resumes at the next unseen page;
* transient error → `FAILED` with the cursor preserved, retried next time;
* `SourceAccessBlocked` → `BLOCKED`, recorded and **not** retried or evaded;
* repeated cursor → `FAILED pagination_cycle`; keyless items fail without
  partial writes; `next` links must stay on the source host over HTTPS;
* `date_windows()` + `enumerate_windows()` slice deep result sets by date;
* `coverage_report()` shows windows exhausted/open/blocked, pages, unique
  items, and flags a source-declared total that disagrees with what was seen.
