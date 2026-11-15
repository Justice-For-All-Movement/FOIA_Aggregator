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
):
    """Run a single crawl cycle."""
    try:
        dry_run_flag = parse_bool(dry_run)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    run_once(
        config_path=config,
        dry_run=dry_run_flag,
        max_docs_per_source=max_docs_per_source,
    )


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


@app.command()
def daemon(config: str = "config/settings.yaml"):
    """Run continuous crawler."""
    run_forever(config_path=config)


if __name__ == "__main__":
    app()
