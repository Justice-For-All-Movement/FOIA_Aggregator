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

## Request-listing mode (requests others filed, by date) — 2026-11-16

`foia_archive/muckrock_requests.py`, migration 14 (`third_party_requests`),
CLI `main.py muckrock-requests --since/--until | --since-days N
[--date-field submitted|updated|done] [--max-pages N]`, site page
`/requests?days=7` and API `/api/v1/requests?days=7|since=&until=&date_field=`.

**Stored:** request id, title, status, agency id + name, agency tracking id,
tags, submitted/updated/done timestamps, public MuckRock URL when the API
returns one. **Never stored:** `user`, edit/read collaborators,
`requested_docs` (request text), composer, price, any contact details.
Only `embargo_status=public` rows are kept (also requested via the filter).
A request is not a release; the page says so.

### Live findings (CI probe from GitHub-hosted runners, 2026-11-16)
* `https://www.muckrock.com/robots.txt`: `User-agent: *` → `Allow: /`, with
  `Content-Signal: search=yes,ai-train=no,use=reference`; AI-training
  crawlers are disallowed. Our use (reference listing, no training) is
  compatible with the signal.
* Every API URL tried (`/api_v1/foia/`, `/api_v2/`, `/api_v2/requests/`
  with and without filters, `/api_v2/schema/`) returned **HTTP 403 from
  Cloudflare ("Attention Required!")** to the honest crawler User-Agent from
  GitHub runner IPs. This is an access control: recorded as **BLOCKED**, not
  retried with other headers or evaded.
* API shape therefore taken from MuckRock's public source
  (`github.com/MuckRock/muckrock`, `muckrock/foia/api_v2/viewsets.py`,
  `serializers.py`, `muckrock/core/pagination.py`, `settings/base.py`):
  * `FOIARequestViewSet` uses `AuthenticatedAPIMixin` → **API token
    required** (`Authorization: Token <key>`, DRF TokenAuthentication).
  * Filters: `datetime_submitted__gte/__lte` (on composer submit time),
    `datetime_updated__gte/__lte`, `datetime_done__gte/__lte`, `status`,
    `embargo_status`, `agency`, `jurisdiction`, `user`, `tags`, `title`;
    `search` on title.
  * `APIV2CursorPagination`: ordered by pk, `page_size` ≤ 100, `next` /
    `previous` links, `count` only with `?count`.
  * Request fields: id, title, requested_docs, slug, status, agency (id),
    composer, embargo_status, user, edit/read_collaborators,
    datetime_submitted, datetime_updated, datetime_done, tracking_id, price,
    tags, edited_boilerplate, date_due. No absolute URL in v2 list rows.
  * Agencies: `/api_v2/agencies/{id}/` (name looked up, ≤50 per run).
  * No explicit DRF throttle classes in settings; we still use ≥2 s/request.
* Terms: MuckRock's site terms govern API use; confirm the owner's account
  terms when requesting a token. No bulk/automated filing is performed.

### To enable
1. Owner creates a MuckRock account API token (MuckRock profile/API page).
2. Add it as GitHub Actions secret `MUCKROCK_API_TOKEN` (used by the live
   probe) and to whatever host runs the authoritative writer job.
3. Run `python main.py muckrock-requests --since-days 7` on the single writer
   (lock held). Exit code 3 = BLOCKED (e.g. Cloudflare still blocks that
   network); the cursor row records it.
Without the token the command exits 0 with "disabled" and makes no request.
