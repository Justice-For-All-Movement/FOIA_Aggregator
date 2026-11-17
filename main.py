import typer

from foia_archive.archive_storage import B2ArchiveStorage, get_archive_storage
from foia_archive.database_backup import (
    backup_database_to_b2,
    bootstrap_database_from_b2,
    restore_database_from_b2,
)
from foia_archive.engine import run_once
from foia_archive.scheduler import run_forever
from foia_archive.storage import get_connection
from foia_archive.text_index import reindex_downloaded_documents
from foia_archive.utils import load_config, parse_bool

app = typer.Typer(help="FOIA Archive CLI")


@app.command()
def run(
    config: str = "config/settings.yaml",
    dry_run: str | None = typer.Option(
        None,
        "--dry-run",
        help="Set to true/false to override the dry-run flag in config (defaults to config value).",
        metavar="[true|false]",
    ),
    max_docs_per_source: int = 10,
    published_since: str | None = typer.Option(
        None, "--published-since",
        help="Archive only documents published on/after this date (YYYY-MM-DD).",
    ),
    published_until: str | None = typer.Option(
        None, "--published-until",
        help="Archive only documents published on/before this date (YYYY-MM-DD).",
    ),
    since_days: int | None = typer.Option(
        None, "--since-days", min=0, max=3660,
        help="Shorthand for --published-since <today minus N days> (UTC).",
    ),
    include_unknown_dates: bool = typer.Option(
        False, "--include-unknown-dates",
        help="Also download documents with no detectable published date.",
    ),
):
    """Run a single crawl cycle (optionally limited to a published-date window).

    With a window, metadata is still recorded for every discovered document,
    but only documents whose detected published date is inside the window are
    downloaded; unknown dates are skipped unless --include-unknown-dates.
    """
    import json
    from foia_archive.published_window import resolve_window

    try:
        dry_run_flag = parse_bool(dry_run)
        window = resolve_window(
            since=published_since, until=published_until,
            since_days=since_days, include_unknown=include_unknown_dates,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    window_stats: dict = {}
    run_once(
        config_path=config,
        dry_run=dry_run_flag,
        max_docs_per_source=max_docs_per_source,
        published_window=window,
        window_stats_out=window_stats,
    )
    if window is not None:
        typer.echo("Published-date window summary: " + json.dumps(window_stats, sort_keys=True))


@app.command("ingestion-status")
def ingestion_status(config: str = "config/settings.yaml"):
    """Print the last local run receipt; this does not contact or mutate B2."""
    import json
    from foia_archive.ingestion_journal import latest_receipt
    cfg = load_config(config)
    typer.echo(json.dumps(latest_receipt(cfg.storage.get("db_path")), sort_keys=True))


@app.command("backup-db")
def backup_db(
    config: str = "config/settings.yaml",
    force: bool = typer.Option(
        False,
        "--force",
        help="Create a backup even when the normal backup interval has not elapsed.",
    ),
):
    """Create a consistent compressed SQLite backup in configured B2 storage."""
    cfg = load_config(config)
    result = backup_database_to_b2(cfg, force=force)
    if result is None:
        typer.echo(
            "No backup created (B2 storage is not selected, backups are disabled, "
            "or the configured interval has not elapsed)."
        )
        return
    typer.echo(
        f"Uploaded {result.key} ({result.compressed_bytes} bytes)"
    )


@app.command("bootstrap-db")
def bootstrap_db(
    config: str = "config/settings.yaml",
    destination: str | None = typer.Option(
        None,
        "--destination",
        help="Live database destination. Defaults to storage.db_path.",
    ),
):
    """Restore the newest verified B2 snapshot only when the live DB is absent."""
    cfg = load_config(config)
    restored = bootstrap_database_from_b2(
        cfg,
        destination_path=destination,
    )
    if restored is None:
        typer.echo(
            "Database bootstrap skipped: a live database already exists "
            "or no B2 backup is available."
        )
        return
    typer.echo(f"Bootstrapped verified database to {restored}")


@app.command("restore-db")
def restore_db(
    config: str = "config/settings.yaml",
    key: str | None = typer.Option(
        None,
        "--key",
        help="Specific B2 backup object key. Defaults to the newest backup.",
    ),
    destination: str | None = typer.Option(
        None,
        "--destination",
        help="Recovery database path. Defaults beside the live DB as *.restored.db.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Allow replacing an existing recovery destination.",
    ),
):
    """Restore and verify a B2 backup into a separate recovery database."""
    cfg = load_config(config)
    restored = restore_database_from_b2(
        cfg,
        key=key,
        destination_path=destination,
        overwrite=overwrite,
    )
    typer.echo(f"Restored verified database to {restored}")




@app.command("reconcile-b2")
def reconcile_b2(
    config: str = "config/settings.yaml",
):
    """Compare SQLite archive state with the current B2 object manifest."""
    cfg = load_config(config)
    backend = get_archive_storage(cfg)
    if not isinstance(backend, B2ArchiveStorage):
        raise typer.BadParameter("reconcile-b2 requires B2 archive storage")

    manifest = backend.current_object_manifest(prefix="documents/")
    conn = get_connection(cfg.storage.get("db_path"))
    try:
        rows = conn.execute(
            """
            SELECT storage_key, MAX(COALESCE(file_size, 0)) AS file_size
            FROM documents
            WHERE storage_backend = 'b2'
              AND download_status = 'downloaded'
              AND storage_key IS NOT NULL
              AND storage_key != ''
            GROUP BY storage_key
            """
        ).fetchall()
    finally:
        conn.close()

    database = {
        str(row["storage_key"]): int(row["file_size"] or 0)
        for row in rows
    }
    missing = sorted(set(database) - set(manifest))
    unexpected = sorted(set(manifest) - set(database))
    size_mismatches = sorted(
        key
        for key in set(database) & set(manifest)
        if database[key] > 0 and manifest[key] != database[key]
    )

    typer.echo(
        "B2 reconciliation: "
        f"database={len(database)}, "
        f"manifest={len(manifest)}, "
        f"missing={len(missing)}, "
        f"unexpected={len(unexpected)}, "
        f"size_mismatches={len(size_mismatches)}"
    )
    for label, keys in (
        ("missing", missing),
        ("unexpected", unexpected),
        ("size mismatch", size_mismatches),
    ):
        for key in keys[:25]:
            typer.echo(f"{label}: {key}")
        if len(keys) > 25:
            typer.echo(f"{label}: ... and {len(keys) - 25} more")

    if missing or size_mismatches:
        raise typer.Exit(code=1)


@app.command("reindex-text")
def reindex_text(
    config: str = "config/settings.yaml",
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of archived documents to process.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-extract documents that already have extraction state.",
    ),
):
    """Backfill full-text search from already archived documents."""
    cfg = load_config(config)
    summary = reindex_downloaded_documents(
        cfg,
        limit=limit,
        force=force,
    )
    typer.echo(
        "Text reindex complete: "
        f"attempted={summary.attempted}, "
        f"indexed={summary.indexed}, "
        f"empty={summary.empty}, "
        f"unsupported={summary.unsupported}, "
        f"failed={summary.failed}"
    )


@app.command("source-inventory")
def source_inventory(
    config: str = "config/settings.yaml",
    census_json: str | None = typer.Option(
        None, "--census-json",
        help="source-census.json produced by scripts/source_census.py.",
    ),
    output: str = typer.Option("source-inventory.json", "--output"),
):
    """FC-01: write the universal source inventory (read-only; no network)."""
    import json
    from pathlib import Path
    import sqlite3
    from foia_archive.source_inventory import build_source_inventory

    cfg = load_config(config)
    census = json.loads(Path(census_json).read_text()) if census_json else None
    db_path = Path(cfg.storage.get("db_path"))
    conn = None
    if db_path.is_file():
        # Read-only: the inventory never competes with the single writer.
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    try:
        inventory = build_source_inventory(census, conn=conn)
    finally:
        if conn is not None:
            conn.close()
    Path(output).write_text(json.dumps(inventory, indent=2, sort_keys=True))
    typer.echo(json.dumps(inventory["summary"], indent=2, sort_keys=True))


@app.command("ingest-request-log")
def ingest_request_log(
    path: str = typer.Argument(..., help="Local copy of an official request log (CSV/TSV/XLSX)."),
    source_url: str = typer.Option(..., "--source-url", help="Official URL the log was obtained from."),
    agency: str | None = typer.Option(None, "--agency"),
    config: str = "config/settings.yaml",
):
    """FC-03: normalize a request log into request records (no network)."""
    from pathlib import Path
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.request_logs import normalize_request_log, store_request_log
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    log = normalize_request_log(Path(path).read_bytes(), source_url=source_url,
                                filename=path, agency=agency)
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            stored = store_request_log(conn, log)
        finally:
            conn.close()
    typer.echo(f"{'Stored' if stored else 'Already stored'} {log.log_id}: "
               f"{len(log.rows)} rows, {len(log.failures)} queued failures")


@app.command("published-logs")
def published_logs_command(
    registry: str = typer.Option("config/published_logs.yaml", "--registry"),
    source: list[str] = typer.Option([], "--source", help="Only these source ids (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List log links only; no downloads or writes."),
    max_files: int | None = typer.Option(None, "--max-files", min=0, max=200),
    summary_json: str | None = typer.Option(None, "--summary-json"),
    config: str = "config/settings.yaml",
):
    """Scrape FOIA request logs agencies already publish (robots, honest UA, BLOCKED never evaded)."""
    import json
    import sqlite3
    from pathlib import Path
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.polite_http import fetch_bytes, fetch_text
    from foia_archive.published_logs import load_registry, scrape_source
    from foia_archive.scraper_core import HostRateLimiter
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    sources = [s for s in load_registry(registry) if not source or s.source_id in source]
    limiter = HostRateLimiter(max(2.0, float(cfg.crawler.get("per_host_delay_seconds", 0.5))))

    def page(url):
        status, _, final_url, text = fetch_text(url, cfg, limiter, accept="text/html,*/*", max_bytes=5 * 1024 * 1024)
        return status, final_url, text

    def file(url):
        status, _, final_url, body = fetch_bytes(url, cfg, limiter)
        return status, final_url, body

    def run(conn):
        return [scrape_source(conn, s, page, file, dry_run=dry_run, max_files=max_files) for s in sources]

    if dry_run:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        results = run(conn)
    else:
        db_path = cfg.storage.get("db_path")
        with exclusive_writer(db_path):
            init_db(db_path, cfg.storage.get("files_dir"))
            conn = get_connection(db_path)
            try:
                results = run(conn)
            finally:
                conn.close()
    states = {}
    for r in results:
        states[r["state"]] = states.get(r["state"], 0) + 1
        typer.echo(f"{r['state']:>12}  {r['source_id']:<28} {r.get('detail', '')}")
    summary = {"sources": len(results), "states": states,
               "files_found": sum(r.get("files_found", 0) for r in results),
               "files_ingested": sum(r.get("files_ingested", 0) for r in results), "results": results}
    if summary_json:
        Path(summary_json).write_text(json.dumps(summary, indent=2, sort_keys=True))
    typer.echo(json.dumps({k: v for k, v in summary.items() if k != "results"}, sort_keys=True))


@app.command("build-closed-requests")
def build_closed_requests_command(
    full_text: bool = typer.Option(True, "--full-text/--no-full-text"),
    config: str = "config/settings.yaml",
):
    """Rebuild the closed-request master list and match it to this archive and MuckRock (no network)."""
    import json
    from foia_archive.closed_requests import build_master_list, match_muckrock, match_reading_room
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            result = {"master_list": build_master_list(conn),
                      "reading_room": match_reading_room(conn, use_full_text=full_text),
                      "muckrock": match_muckrock(conn)}
        finally:
            conn.close()
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("match-documentcloud")
def match_documentcloud_command(
    max_queries: int = typer.Option(200, "--max-queries", min=0, max=5000),
    config: str = "config/settings.yaml",
):
    """Search public DocumentCloud for unmatched released requests (bounded, polite)."""
    import json
    from foia_archive.closed_requests import match_documentcloud
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.polite_http import fetch_json
    from foia_archive.scraper_core import HostRateLimiter
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    limiter = HostRateLimiter(max(2.0, float(cfg.crawler.get("per_host_delay_seconds", 0.5))))
    db_path = cfg.storage.get("db_path")
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            result = match_documentcloud(conn, lambda url: fetch_json(url, cfg, limiter),
                                         max_queries=max_queries)
        finally:
            conn.close()
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("release-coverage")
def release_coverage_command(
    granularity: str = typer.Option("month", "--granularity", help="month, quarter or year"),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
    agency: str | None = typer.Option(None, "--agency"),
    config: str = "config/settings.yaml",
):
    """Released records vs records held, per agency and period (read-only)."""
    import json
    from foia_archive.closed_requests import release_coverage

    conn = _read_only_connection(config)
    try:
        report = release_coverage(conn, granularity=granularity, since=since, until=until, agency=agency)
    finally:
        conn.close()
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@app.command("export-release-gaps")
def export_release_gaps_command(
    output_dir: str = typer.Argument(..., help="Directory for manifest.json and gaps.jsonl."),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
    agency: str | None = typer.Option(None, "--agency"),
    config: str = "config/settings.yaml",
):
    """Write foia-release-gaps/1 for Send_FOIA_Log_Requests follow-up drafts (read-only)."""
    import json
    from foia_archive.closed_requests import write_gap_export
    from foia_archive.producer import producer_info

    conn = _read_only_connection(config)
    try:
        manifest = write_gap_export(conn, output_dir, since=since, until=until, agency=agency,
                                    producer_commit=producer_info().get("commit"))
    finally:
        conn.close()
    typer.echo(json.dumps(manifest, sort_keys=True))


@app.command("b2-diagnose")
def b2_diagnose_command(
    write_probe: bool = typer.Option(False, "--write-probe", help="Upload and delete a 16-byte probe object."),
):
    """Report why B2 uploads fail (B2 error codes such as cap_exceeded; never secrets).

    Exit 0 = writable or authorized, 5 = cap exceeded, 6 = other problem, 0 without credentials.
    """
    import json
    from foia_archive.b2_diagnose import diagnose

    report = diagnose(write_probe=write_probe)
    typer.echo(json.dumps(report, indent=2, sort_keys=True, default=str))
    if report["verdict"] == "cap_exceeded":
        raise typer.Exit(code=5)
    if report["verdict"] not in ("writable", "authorized_no_write_probe", "no_credentials"):
        raise typer.Exit(code=6)


@app.command("plan-showcase")
def plan_showcase_command(
    budget_gb: float = typer.Option(8.5, "--budget-gb", min=0.1, max=10_000),
    max_file_mb: float = typer.Option(25, "--max-file-mb", min=0.1, max=1000),
    per_agency_max: int | None = typer.Option(None, "--per-agency-max", min=1),
    priorities: str = typer.Option("config/agency_priority.yaml", "--priorities"),
    config: str = "config/settings.yaml",
):
    """Choose a diverse, budget-capped set of originals (round-robin across agencies). No deletions."""
    import json
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.showcase import load_priorities, plan_showcase
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            plan = plan_showcase(conn, rules=load_priorities(priorities),
                                 budget_bytes=int(budget_gb * 1e9), max_file_bytes=int(max_file_mb * 1024 * 1024),
                                 per_agency_max=per_agency_max)
        finally:
            conn.close()
    typer.echo(json.dumps(plan, indent=2, sort_keys=True))


@app.command("apply-showcase")
def apply_showcase_command(
    confirm: str = typer.Option("", "--confirm", help='Type exactly: REPLACE ARCHIVE WITH SHOWCASE'),
    budget_gb: float = typer.Option(8.5, "--budget-gb", min=0.1, max=10_000),
    config: str = "config/settings.yaml",
):
    """Delete stored originals not in the showcase plan, then download the selected ones.

    Catalog rows, extracted text and SHA-256 are kept for every record. Without the
    exact confirmation phrase this only prints what would happen.
    """
    import json
    from foia_archive.archive_storage import B2ArchiveStorage, get_archive_storage
    from foia_archive.database_backup import _permanently_delete_object
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.scraper_core import HostRateLimiter, download_document
    from foia_archive.showcase import CONFIRM_PHRASE, fill_selection, prune_to_selection, showcase_summary
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    storage = get_archive_storage(cfg)
    if not isinstance(storage, B2ArchiveStorage):
        typer.echo("apply-showcase needs S3-compatible object storage (FOIA_STORAGE_BACKEND=s3).")
        raise typer.Exit(code=2)
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            if confirm != CONFIRM_PHRASE:
                keep = conn.execute(
                    """SELECT COUNT(*) FROM showcase_selection s JOIN documents d ON d.id=s.document_id
                       WHERE d.storage_key IS NOT NULL""").fetchone()[0]
                stored = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE storage_backend='b2' AND storage_key IS NOT NULL"
                ).fetchone()[0]
                typer.echo(json.dumps({"dry_run": True, "stored_originals": stored, "would_keep": keep,
                                       "would_delete_about": stored - keep,
                                       "selection": showcase_summary(conn)}, indent=2, sort_keys=True))
                typer.echo(f'Nothing changed. To apply, pass --confirm "{CONFIRM_PHRASE}".')
                return
            pruned = prune_to_selection(conn, storage, _permanently_delete_object, confirm=confirm)
            limiter = HostRateLimiter(max(2.0, float(cfg.crawler.get("per_host_delay_seconds", 0.5))))
            filled = fill_selection(conn, cfg, storage, budget_bytes=int(budget_gb * 1e9),
                                    download=download_document, rate_limiter=limiter,
                                    files_dir=cfg.storage.get("files_dir"))
        finally:
            conn.close()
    typer.echo(json.dumps({"pruned": pruned, "filled": filled}, indent=2, sort_keys=True))


@app.command("export-catalog")
def export_catalog(
    output_dir: str = typer.Argument(..., help="Directory for manifest.json and records-*.jsonl."),
    config: str = "config/settings.yaml",
):
    """FC-13: write a versioned catalog snapshot with per-file SHA-256 (read-only)."""
    import sqlite3
    from pathlib import Path
    from foia_archive.export_contract import verify_snapshot, write_snapshot

    cfg = load_config(config)
    db_path = Path(cfg.storage.get("db_path"))
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        manifest = write_snapshot(conn, Path(output_dir))
    finally:
        conn.close()
    ok, problems = verify_snapshot(Path(output_dir))
    if not ok:
        raise typer.Exit(code=1)
    typer.echo(f"Exported {manifest['counts']['records']} records "
               f"({manifest['snapshot_id']}) in {len(manifest['files'])} files")


def _read_only_connection(config: str):
    import sqlite3
    from pathlib import Path
    db_path = Path(load_config(config).storage.get("db_path"))
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@app.command("export-dataset")
def export_dataset(
    dataset: str = typer.Argument(..., help="document-text or request-log-rows"),
    output_dir: str = typer.Argument(...),
    config: str = "config/settings.yaml",
):
    """Write a producer-stamped snapshot of an extension dataset (read-only)."""
    from pathlib import Path
    from foia_archive.export_extensions import DATASETS, write_dataset_snapshot
    if dataset not in DATASETS:
        raise typer.BadParameter(f"dataset must be one of {sorted(DATASETS)}")
    conn = _read_only_connection(config)
    try:
        manifest = write_dataset_snapshot(conn, Path(output_dir), dataset)
    finally:
        conn.close()
    typer.echo(f"Exported {manifest['counts']['records']} {dataset} records ({manifest['snapshot_id']})")


@app.command("export-log-coverage")
def export_log_coverage(output_dir: str, config: str = "config/settings.yaml"):
    """Write the per agency/year request-log coverage export (read-only)."""
    from pathlib import Path
    from foia_archive.export_extensions import write_log_coverage
    conn = _read_only_connection(config)
    try:
        manifest = write_log_coverage(conn, Path(output_dir))
    finally:
        conn.close()
    typer.echo(f"Exported {manifest['records']['count']} coverage records; producer commit "
               f"{manifest['producer']['commit'] or 'UNPINNED'}")


@app.command("import-sfl-inbox")
def import_sfl_inbox(
    inbox: str = typer.Option(None, "--inbox", help="Defaults to FOIA_INGEST_INBOX or <db dir>/sfl-inbox."),
    config: str = "config/settings.yaml",
):
    """Import staged Send_FOIA_Log_Requests bundles (authoritative writer only)."""
    import os
    import re
    from pathlib import Path
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.sfl_interchange import import_staged
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    root = Path(inbox or os.getenv("FOIA_INGEST_INBOX") or Path(db_path).parent / "sfl-inbox")
    staged = sorted(p for p in root.iterdir() if re.fullmatch(r"[0-9a-f]{64}", p.name)) if root.is_dir() else []
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            for directory in staged:
                result = import_staged(conn, directory)
                typer.echo(f"{result['bundle_sha256']}: imported={result['imported']} "
                           f"request_logs={result['request_logs']}")
        finally:
            conn.close()
    typer.echo(f"{len(staged)} staged bundle(s) processed")


@app.command("analyze-identity")
def analyze_identity_command(config: str = "config/settings.yaml"):
    """FC-08: record dedupe/version relations (writer lock; never merges records)."""
    import json
    from foia_archive.identity import analyze_identity, relation_summary
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.storage import get_connection, init_db

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            analyze_identity(conn)
            typer.echo(json.dumps(relation_summary(conn), indent=2, sort_keys=True))
        finally:
            conn.close()


@app.command("corpus-review")
def corpus_review(
    output: str = typer.Option("corpus-review.json", "--output"),
    census_json: str | None = typer.Option(None, "--census-json"),
    config: str = "config/settings.yaml",
):
    """FC-14: write the stratified corpus review evidence (read-only; status stays draft)."""
    import json
    from pathlib import Path
    from foia_archive.completeness import build_corpus_review
    census = json.loads(Path(census_json).read_text()) if census_json else None
    conn = _read_only_connection(config)
    try:
        review = build_corpus_review(conn, census)
    finally:
        conn.close()
    Path(output).write_text(json.dumps(review, indent=2, sort_keys=True))
    for gate in review["gates"]:
        typer.echo(f"{gate['result']:>7}  {gate['id']}: {gate['description']}")


@app.command("muckrock-requests")
def muckrock_requests_command(
    since: str | None = typer.Option(None, "--since", help="Window start YYYY-MM-DD (inclusive)."),
    until: str | None = typer.Option(None, "--until", help="Window end YYYY-MM-DD (inclusive)."),
    since_days: int | None = typer.Option(None, "--since-days", min=0, max=3660,
                                          help="Shorthand for --since <today minus N days>."),
    date_field: str = typer.Option("submitted", "--date-field",
                                   help="Which request date the window applies to: submitted, updated or done."),
    max_pages: int = typer.Option(5, "--max-pages", min=1, max=200),
    config: str = "config/settings.yaml",
):
    """FC-04: store public MuckRock request metadata filed in a date window.

    Requires MUCKROCK_API_TOKEN (MuckRock's API v2 list endpoint is
    authenticated); without it the command exits without network access.
    Stores no requester identity. Runs under the single-writer lock.
    """
    import json
    from foia_archive.ingestion_journal import exclusive_writer
    from foia_archive.muckrock_requests import DATE_FIELDS, api_token, list_requests
    from foia_archive.polite_http import fetch_json
    from foia_archive.published_window import resolve_window
    from foia_archive.scraper_core import HostRateLimiter
    from foia_archive.storage import init_db

    if date_field not in DATE_FIELDS:
        raise typer.BadParameter(f"--date-field must be one of {sorted(DATE_FIELDS)}")
    try:
        window = resolve_window(since=since, until=until, since_days=since_days)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if window is None:
        raise typer.BadParameter("give --since, --until or --since-days")
    token = api_token()
    if token is None:
        typer.echo("MuckRock request listing disabled: set MUCKROCK_API_TOKEN (no request was made).")
        raise typer.Exit(code=0)

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    # No published throttle found; stay conservative at >= 2 s per request.
    limiter = HostRateLimiter(max(2.0, float(cfg.crawler.get("per_host_delay_seconds", 0.5))))
    with exclusive_writer(db_path):
        init_db(db_path, cfg.storage.get("files_dir"))
        conn = get_connection(db_path)
        try:
            result = list_requests(
                conn, lambda url: fetch_json(url, cfg, limiter, token=token),
                since=window["since"], until=window["until"], date_field=date_field,
                max_pages=max_pages,
            )
        finally:
            conn.close()
    typer.echo(json.dumps(result, sort_keys=True))
    if result["state"] == "BLOCKED":
        raise typer.Exit(code=3)


@app.command("bootstrap-github-snapshot")
def bootstrap_github_snapshot(
    destination: str = typer.Option(..., "--destination", help="Database path to create."),
    repo: str = typer.Option(None, "--repo", help="owner/name; defaults to FOIA_GITHUB_SNAPSHOT_REPO."),
):
    """Install the newest published catalog snapshot (GitHub Release) when no DB exists.

    Exit 0 = restored, 4 = none published yet, 1 = failed verification/download.
    """
    import os
    from pathlib import Path
    from foia_archive import github_snapshot as gs

    repo = repo or os.getenv("FOIA_GITHUB_SNAPSHOT_REPO") or gs.DEFAULT_REPO
    target = Path(destination)
    if target.exists() and target.stat().st_size > 0:
        typer.echo("A database already exists; snapshot bootstrap skipped.")
        return
    try:
        manifest = gs.restore_latest(repo, target)
    except Exception as exc:  # noqa: BLE001 - report and let the caller fall back
        typer.echo(f"Catalog snapshot restore failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1)
    if manifest is None:
        typer.echo("No catalog snapshot has been published yet.")
        raise typer.Exit(code=4)
    typer.echo(f"Restored catalog snapshot {manifest['asset']} "
               f"({(manifest.get('counts') or {}).get('documents')} documents)")


@app.command("build-catalog-snapshot")
def build_catalog_snapshot(
    output_dir: str = typer.Argument(..., help="Directory for the .db.gz asset and latest.json."),
    previous_manifest: str = typer.Option(None, "--previous-manifest"),
    allow_shrink: bool = typer.Option(False, "--allow-shrink"),
    config: str = "config/settings.yaml",
):
    """Write a verified, gzip'd SQLite catalog snapshot (metadata + text) for publication."""
    import json
    from pathlib import Path
    from foia_archive import github_snapshot as gs

    cfg = load_config(config)
    previous = None
    if previous_manifest and Path(previous_manifest).is_file():
        previous = json.loads(Path(previous_manifest).read_text())
    try:
        manifest = gs.build_snapshot(Path(cfg.storage.get("db_path")), Path(output_dir),
                                     previous=previous, allow_shrink=allow_shrink)
    except gs.SnapshotError as exc:
        typer.echo(f"Snapshot refused: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(manifest, indent=2, sort_keys=True))


@app.command("snapshot-prune-plan")
def snapshot_prune_plan(keep: int = typer.Option(3, "--keep", min=1, max=20)):
    """Read release asset names on stdin; print the dated snapshots to delete."""
    import sys
    from foia_archive.github_snapshot import prune_plan

    for name in prune_plan([line.strip() for line in sys.stdin if line.strip()], keep):
        typer.echo(name)


@app.command("wayback-preserve")
def wayback_preserve(
    config: str = "config/settings.yaml",
    max_per_run: int = typer.Option(None, "--max", min=1, max=500),
):
    """Record/request Internet Archive snapshots for fetched records (authoritative writer only)."""
    import json
    from foia_archive import wayback
    from foia_archive.ingestion_journal import exclusive_writer

    cfg = load_config(config)
    db_path = cfg.storage.get("db_path")
    with exclusive_writer(db_path):
        conn = get_connection(db_path)
        try:
            counts = wayback.preserve(conn, cfg.crawler.get("user_agent", "FOIAArchiveBot/0.1"),
                                      max_per_run=max_per_run)
        finally:
            conn.close()
    typer.echo(json.dumps(counts, sort_keys=True))


@app.command()
def daemon(config: str = "config/settings.yaml"):
    """Run continuous crawler."""
    run_forever(config_path=config)


if __name__ == "__main__":
    app()
