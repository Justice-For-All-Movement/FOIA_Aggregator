# Owner quickstart

Everything below is done in the GitHub repository settings
(**Settings → Secrets and variables → Actions**) and the Render dashboard.
Only names are listed here; never commit values.

## 1. First look with zero keys (no storage at all)

Nothing to configure. The **Catalog crawl** workflow
(`.github/workflows/catalog-crawl.yml`) crawls every FOIA.gov publication
source (≈365 reading rooms, plus linked FOIAXpress rooms and a few curated
ones), keeps record metadata, extracted text and SHA-256 hashes, and publishes
a verified SQLite snapshot as an asset of the GitHub Release
`catalog-snapshot` (newest 3 kept). The Render web app downloads the newest
snapshot at startup when no storage keys are set.

1. **Actions → Catalog crawl → Run workflow** (defaults: last 7 days,
   every dated document in the window, up to 10,000 per source). A run takes up to ~5 hours.
2. To run it weekly (Mondays), add the repository **variable**
   `FOIA_CATALOG_CRAWL_ENABLED` = `true`.
3. Restart (or redeploy) the Render service after the first run finishes; the
   site shows a "Catalog snapshot" notice and search works on metadata and
   text. "Open original source" links go to the agency.

## 2. What to expect

- **Run 1 is the baseline.** Every source is crawled for the first time, so
  nothing is "newly found" yet. Documents whose release date (from the page,
  the server's `Last-Modified`, or PDF metadata) falls in the window are
  fetched and indexed; the rest are recorded as metadata only.
- **Run 2 onwards** also marks documents that appear on pages already seen in
  an earlier run as *newly found* (pages reached for the first time, e.g. by
  deeper traversal, are baseline material and never count as new). See
  `/recent` on the site.
- Sources that return 403/CAPTCHA, or whose `robots.txt` disallows crawling,
  are recorded as BLOCKED and skipped — never evaded. Crawl-delay is honoured.
- The run summary lists snapshot size and counts. A snapshot with fewer
  documents than the previous one is refused (never replace a larger catalog).

## 3. Optional: keep original files (S3-compatible storage)

Add these **secrets** (any S3-compatible provider; Backblaze B2's old
`B2_KEY_ID`/`B2_APPLICATION_KEY`/`B2_BUCKET` names still work):

| Provider | `FOIA_S3_ENDPOINT` | `FOIA_S3_REGION` |
|---|---|---|
| Backblaze B2 (10 GB free) | `https://s3.<region>.backblazeb2.com` | e.g. `us-east-005` |
| Cloudflare R2 (10 GB free) | `https://<account-id>.r2.cloudflarestorage.com` | `auto` (or blank) |
| Oracle Object Storage (20 GB free) | `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com` | e.g. `us-ashburn-1` |

Secrets: `FOIA_S3_ENDPOINT`, `FOIA_S3_BUCKET`, `FOIA_S3_REGION`,
`FOIA_S3_ACCESS_KEY_ID`, `FOIA_S3_SECRET_ACCESS_KEY`.

Set the same five as environment variables on the Render service so the web
app can serve archived copies (it then restores the newest verified bucket
checkpoint, falling back to the GitHub snapshot).

Storage policy (always on): metadata, extracted text and SHA-256 are kept for
everything fetched. Original files are kept within a byte budget — variable
`FOIA_ORIGINALS_BUDGET_BYTES` (default: the 8.5 GB archive cap). As the budget
fills, newly found/recent, at-risk and small documents are preferred; beyond
it, records keep text + hash + a pointer to the source ("Text and SHA-256
only" on the record page).

## 4. Optional: Internet Archive preservation

Variable `FOIA_WAYBACK_ENABLED` = `true` makes each crawl record existing
Wayback Machine snapshots for fetched documents and submit missing ones to
Save Page Now (at most 40 per run, ≥12 s apart, stops on any rate limit).
Anonymous by default; for higher limits add secrets `IA_S3_ACCESS_KEY` and
`IA_S3_SECRET_KEY` (archive.org → account → S3 API keys). Snapshot links
appear on record pages.

## 5. Optional keys

- `FOIA_API_KEY` (secret): your own api.data.gov key for FOIA.gov metadata.
  Without it the workflow uses FOIA.gov's published testing key.
- `MUCKROCK_API_TOKEN` (secret): enables the MuckRock request listing
  (MuckRock blocks anonymous API use from GitHub runners).

## Limits and safety

- One writer at a time: every corpus workflow shares the
  `foia-b2-single-writer` concurrency group.
- Snapshots are bounded (refused above 900 MB compressed); only the newest 3
  release assets are kept. They contain only public record metadata and text.
- The older `Autonomous corpus refresh` / `Expand corpus` workflows are B2-only
  and remain gated; use **Catalog crawl** for normal operation.
