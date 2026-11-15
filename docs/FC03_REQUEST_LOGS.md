# FC-03 request/release log normalization

Status (2026-11-15): **code complete with synthetic fixtures**; no official log
has been ingested yet. `foia_archive/request_logs.py`, schema migration 9.

`python main.py ingest-request-log <local-file> --source-url <official URL> [--agency slug]`
normalizes a CSV/TSV/XLSX log and stores it under the single-writer lock.

* **Separate from documents.** Rows land in `request_logs`,
  `request_log_rows` and `request_log_failures`; a log row is a request record,
  not a released document. `released_record_reference` is a pointer only;
  matching it to an archived binary needs separate evidence (FC-08).
* **Provenance.** Each log keeps source URL, SHA-256 of the raw bytes, format
  and the header-to-field mapping; `log_id` is derived from URL + hash, so
  re-ingesting identical bytes is a no-op. Rows keep sheet, row number and raw
  cells verbatim.
* **Confidence.** Header mapping confidence is 1.0 (canonical name), 0.9
  (known synonym) or 0.6 (token match); each field stores its confidence, and
  date cells parsed from Excel serials are discounted.
* **No guessing.** Unparseable or implausible dates become `null` with a
  warning; closed-before-received is flagged; rows with neither tracking number
  nor description go to the failed-row queue with their raw cells.
* **Unsupported formats.** PDF/scanned and legacy `.xls` logs are queued as
  `unsupported_format:*` rather than parsed unreliably.
* **Privacy.** `requester_name` is stored as published but is a personal field;
  FC-13 exports exclude it.
