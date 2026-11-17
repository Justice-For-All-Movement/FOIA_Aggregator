# Showcase archive (free 10 GB storage)

The owner cannot raise the Backblaze cap, so stored originals are limited to a diverse
showcase that spans as many agencies as possible. **Searchability does not depend on this:**
metadata, extracted text and SHA-256 for every catalog record live in the published SQLite
snapshot (a GitHub Release), not in B2. Only the viewable original files are limited.

## When
Run this after the log pipeline has produced a closed-request list for a period. Documents
matched to logged closed requests are then picked first within each agency.

## How (owner only)
Actions → **Showcase archive (owner only)** → Run workflow:
1. Leave `confirm` empty first. That is a **dry run**: it shows the plan (agencies, documents,
   bytes, how many are already stored) and how many stored originals it would delete.
   Nothing changes.
2. Run again with `confirm` = `REPLACE ARCHIVE WITH SHOWCASE`. Originals outside the
   selection are permanently deleted (every B2 version). Catalog rows and text are kept,
   and those records become `text_only`. The selected documents are then downloaded,
   text-extracted and stored up to `budget_gb`, and a new catalog snapshot is published.

How the selection works:
* Round-robin, one document per agency per round: tier 1 (CIA, NSA, DOJ/FBI, DHS, ODNI, DoD)
  first, then tier 2, then everyone else (`config/agency_priority.yaml`).
* Within an agency, the order is: tied to a logged closed request, then already stored
  (no re-download), then newest.
* Files over 25 MB are skipped so one release cannot eat the budget.
* The default budget of 8 GB leaves room for new releases found by the weekly crawl.

Run `main.py b2-diagnose --write-probe` (the crawl runs it automatically) to see whether the
current upload failures are the 10 GB storage cap or a daily transaction cap. Freeing space
only helps with the storage cap.
