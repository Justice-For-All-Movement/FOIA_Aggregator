# Release coverage: closed-request master list (FC-03/FC-04/FC-12)

Goal: know the real denominator, i.e. how many requests agencies closed with a release in a
period, and what share of those released records this archive holds.

## Pipeline

1. **Published logs** (`main.py published-logs`). `config/published_logs.yaml` lists official
   log index pages: 50 sources seeded 2026-11-17 from web-search research, all `verified: false`
   until a live run finds log files. Index pages are fetched with robots.txt, the honest UA,
   2 s/host and SSRF-safe redirects. Linked CSV/TSV/XLSX/XLS/PDF logs, up to `max_files` per source
   and newest-looking first, go through FC-03 normalization. 401/403/CAPTCHA pages are recorded
   `BLOCKED` and never evaded. Each source gets a `published_log_sources` row.
   *New in FC-03:* a `pages_released` column, text-layer PDF tables via `pdfplumber`
   (confidence ×0.8, warned) and legacy `.xls` via `xlrd`. Scanned PDFs are still queued
   as `unsupported_format:pdf_no_text_table`.
2. **Master list** (`main.py build-closed-requests`). The command builds `closed_requests`,
   one row per agency + normalized tracking number, and keeps the best-evidenced row across
   re-published logs. Open requests (no closed date and no terminal disposition) are excluded.
   Each row gets:
   * `disposition`: one of `full_grant`, `partial_grant`, `full_denial`, `no_records`,
     `withdrawn`, `referred`, `duplicate`, `fee_related`, `improper`, `other` or `unknown`;
   * `first_party_reason`: set for A-files, immigration, personnel, medical/claims, tax/SSA,
     background-investigation and "my own records" requests, or when the log's own type column
     says first party;
   * `releasable`: a full or partial grant that is not first-party. **This is the denominator.**
3. **Matching** (same command, plus `main.py match-documentcloud`). Results go to
   `closed_request_matches` with method, confidence and evidence:
   * `reading_room`: the tracking number appears in a document's title, filename or URL in this
     archive (agency-consistent), or as an exact phrase in its indexed text. Only this source
     counts as **held**.
   * `muckrock`: exact agency tracking id on a public MuckRock request. MuckRock responses go to
     the requester and rarely reach agency reading rooms, so this source is not redundant.
     Listing needs `MUCKROCK_API_TOKEN`.
   * `documentcloud`: a public DocumentCloud search for the quoted tracking number. Bounded by
     `--max-queries`, and each request is re-checked at most every 30 days.
4. **Coverage** (`/release-coverage`, `/api/v1/release-coverage`, `main.py release-coverage`).
   Results are given per agency and per month, quarter or year, as: closed, first-party,
   not released, released, held, elsewhere only, gaps, pages released and held share.
5. **Gaps** (`main.py export-release-gaps DIR`, `/api/v1/export/release-gaps.jsonl`). The export
   format is `foia-release-gaps/1`, with no requester identity. Send_FOIA_Log_Requests
   `plan-followups` turns each gap into a draft request for "records released in response to
   request #X". Drafts only: nothing is sent.

`catalog-crawl.yml` runs steps 1–4 after the crawl, before the snapshot is built (the single
writer). `live-probe.yml` dry-runs step 1 on every PR, so registry URLs are checked live.

## Not yet true (2026-11-17)
* No official log has been ingested yet. The first live dry run happens on this PR's live-probe job.
* The first-party rules are heuristic. Review a sample per agency before treating the shares as final.
* Agencies that publish no log (CIA, NSA, DEA, ATF, BOP, USMS, FCC, VA, SSA, ...) need log
  requests from Send_FOIA_Log_Requests. Sending stays disabled pending owner decisions.
