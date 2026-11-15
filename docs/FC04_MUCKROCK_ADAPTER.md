# FC-04 MuckRock third-party release adapter

Status (2026-11-15): **adapter complete against synthetic recorded-shape
fixtures; inert** (no scheduler, CLI or workflow calls it). No live MuckRock
request has been made. Before live use: confirm current API terms/rate limits
and robots, inject a `fetch_json` that uses the crawler's safe HTTP client and
`HostRateLimiter`, and record the first bounded sample as acceptance evidence.

`foia_archive/muckrock_adapter.py`, schema migration 10 (`third_party_releases`).

* Pages of `/api_v1/foia/` are traversed with the FC-02 cursor
  (`json_api_next_link`); `next` links must stay on `www.muckrock.com` over
  HTTPS; persistent denials become `BLOCKED` and are not retried or evaded.
* **Released records only:** files attached to agency *response*
  communications. Requester uploads, request pages and requests without
  responsive files never become release records.
* File URLs must be HTTPS on MuckRock hosts (or MuckRock's own S3 bucket);
  anything else is counted as `unsafe_file_urls` and skipped.
* Attribution is kept separately: original agency (`agency_ref`/name),
  publisher (`MuckRock`), public request URL/status, file URL/title/date.
  `release_id` is stable (publisher + request + normalized file URL).
* Official cross-link: `official_document_id` is set only on an exact URL
  match with an existing official document. Third-party releases are never
  inserted into `documents` implicitly and are not counted as archived.
