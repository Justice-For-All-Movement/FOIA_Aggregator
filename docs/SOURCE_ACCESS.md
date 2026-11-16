# Public source access compatibility

Some official federal reading rooms return HTTP 403 to generic HTTP clients even
when the same public page is available in an ordinary browser. The crawler uses
a deliberately narrow compatibility fallback for that case.

## Behavior

For ordinary public reading-room pages and document downloads:

1. request the public URL using the archive's descriptive crawler User-Agent;
2. preserve ordinary session cookies within one reading-room crawl when enabled;
3. if the public endpoint returns HTTP 403, retry that same public URL once with
   conventional browser-compatible HTTP headers;
4. if the second request is still 403, stop and classify the source/document as
   access blocked.

PAL adapters use the same one-time 403 compatibility fallback while retaining
their existing session behavior.

## What this does not do

The crawler does **not**:

- solve or bypass CAPTCHAs;
- authenticate to non-public resources;
- evade IP blocks by rotating proxies;
- spoof logged-in sessions;
- bypass robots, paywalls, access tokens, or deliberate authorization controls;
- repeatedly hammer a source that continues to deny access.

A persistent 403 remains visible in source-health telemetry so an administrator
can decide whether the source needs a legitimate site-specific adapter, a
corrected official URL, or simply cannot be crawled from the current runner.

## Configuration

```yaml
crawler:
  browser_compatibility_fallback: true
  preserve_session_cookies: true
  browser_user_agent: "Mozilla/5.0 ..."
```

The browser-compatible retry is limited to a single retry after HTTP 403.
Normal retry/backoff rules continue to govern transient 429/5xx responses.

## Administrator visibility

The administrator dashboard reports:

- sources with current errors;
- 403 / Forbidden failures;
- rate-limited failures;
- recent error text and source URLs.

After deploying this compatibility layer, run another broad crawl and compare
the remaining 403 queue with the pre-deployment baseline. Persistent sources can
then receive narrow source-specific treatment where appropriate.

## November 2026 update: robots.txt and no header fallback

- Every crawler request (pages, feeds, HEAD probes, downloads, PAL endpoints,
  probe/API fetches) checks `robots.txt` first (RFC 9309, cached per host per
  run); `Crawl-delay` raises that host's delay (capped at 60 s). A 4xx
  robots.txt means no restrictions; an unreachable/5xx robots.txt means the
  source is skipped for that run and recorded as BLOCKED with the reason.
- `browser_compatibility_fallback` now defaults to `false`: a 403 is recorded
  as BLOCKED and never retried with browser-like headers.
- Document links are kept only on the source's own registrable domain or a
  known agency document host (e.g. `media.defense.gov`); off-site footer
  documents are dropped.
