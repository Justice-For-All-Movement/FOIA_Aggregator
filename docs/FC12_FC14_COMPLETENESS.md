# FC-12 completeness dashboard and FC-14 corpus review

Status (2026-11-15): **code complete with fixtures**; the deployed numbers are
only as good as the loaded snapshot (none while the archive is not connected).

## FC-12 — `/coverage` and `GET /api/v1/coverage`
`foia_archive/completeness.compute_completeness()` reports distinct
denominators, never one blended percentage:

* sources: known, official vs third-party, verified reachable, unverified
  (unknown), blocked (not evaded), fully enumerated (FC-02), never successfully
  crawled, last verified crawl, database sources not explained by the
  inventory, by source type;
* IC elements with a recognized source (of 18);
* records: cataloged, preserved, remote-only, preserved bytes, text indexed,
  OCR-indexed, acquisition and extraction status breakdowns;
* by agency (with oldest/newest published year) and by published year;
* FC-08 dedupe and FC-03 request-log summaries when present;
* `unknowns`: the total number of public FOIA records is reported as
  unknowable (`null`), with counts of records lacking dates and unverified sources.

Cached for five minutes per process; the archive connection status is included.

## FC-14 — `python main.py corpus-review [--census-json …]`
Writes `fc14-corpus-review/1`: strata by agency, period, source type,
provenance, document type, extraction method, request-log disposition and IC
coverage; missing known files; blocked sources; and gates (≥25 sources fully
enumerated, 18/18 IC, ≥20 agencies, ≥10 years, third-party present, every
archived record hashed, request logs present, latest run checkpoint VERIFIED)
reported as pass / fail / unknown from evidence only. `review_status` is always
`draft` and `downstream_unblocked` always `false`: acceptance is an owner
decision recorded outside the generator, and the log-coverage manifest keeps
`fc14_review.status = "absent"` until then. The gate thresholds are proposals
for that review, not certified coverage targets.
