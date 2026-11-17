"""High level orchestration for discovery and scraping."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from typing import Optional

from .database_backup import backup_database_to_b2
from .discovery import refresh_metadata
from .ingestion_journal import RunJournal, exclusive_writer, policy_fingerprint
from .scraper_core import (
    HostRateLimiter,
    crawl_reading_room,
    get_reading_rooms_to_crawl,
)
from . import published_window as pw
from . import wayback
from .storage_policy import run_download_counter
from .storage import (
    get_connection,
    init_db,
    record_reading_room_crawl_failure,
)
from .utils import Config, load_config, logger


def _record_unexpected_source_failure(
    cfg,
    reading_room_id: int,
    exc: Exception,
) -> None:
    """Best-effort persistence for unexpected per-source failures."""
    conn = None
    try:
        conn = get_connection(cfg.storage.get("db_path"))
        record_reading_room_crawl_failure(
            conn,
            reading_room_id,
            datetime.now(timezone.utc).isoformat(),
            f"{type(exc).__name__}: {exc}"[:2000],
        )
    except Exception:
        logger.exception(
            "Could not persist unexpected failure state for reading room %s",
            reading_room_id,
        )
    finally:
        if conn is not None:
            conn.close()


def _bounded_crawl_count(value, *, fallback: int, label: str, maximum: int = 10000) -> int:
    """Fail closed instead of silently crawling unlimited sources/documents."""
    if value is None:
        return fallback
    if type(value) is int:
        parsed = value
    elif type(value) is str and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError(f"Invalid {label} crawl limit")
    if not 1 <= parsed <= maximum:
        raise ValueError(f"Unbounded {label} crawl limit")
    return parsed


def _validate_page_limits(crawler) -> None:
    """Reject unsafe page budgets before metadata, database or crawler work."""
    for name, default, minimum, maximum in (
        ("max_pages_per_source", 50, 1, 5000),
        ("max_discovered_docs_per_source", 1000, 1, 10000),
        ("max_depth", 3, 0, 10),
    ):
        _bounded_integer(crawler.get(name, default), minimum, maximum, name)
    for name, default, minimum, maximum in (
        ("page_timeout_seconds", 30, 1.0, 300.0),
        ("page_max_size_mb", 5, 0.1, 100.0),
    ):
        value = crawler.get(name, default)
        if type(value) not in (int, float, str):
            raise ValueError("Invalid crawler." + name)
        try:
            parsed = float(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError("Invalid crawler." + name) from exc
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ValueError("Unbounded crawler." + name)


def _bounded_integer(value, minimum: int, maximum: int, label: str) -> int:
    if type(value) is int:
        parsed = value
    elif type(value) is str and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValueError("Invalid crawler." + label)
    if not minimum <= parsed <= maximum:
        raise ValueError("Unbounded crawler." + label)
    return parsed


def _validated_dry_run(value) -> bool:
    """Never turn a malformed dry-run setting into permission to write."""
    if type(value) is bool:
        return value
    if type(value) is str:
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    raise ValueError("Invalid crawler.dry_run; explicit boolean required")


def run_once(
    config_path: str = "config/settings.yaml",
    dry_run: Optional[bool] = None,
    max_docs_per_source: Optional[int] = None,
    refresh_metadata_enabled: bool = True,
    backup_enabled: bool = True,
    published_window: Optional[dict] = None,
    window_stats_out: Optional[dict] = None,
) -> bool:
    """Run one crawl cycle.

    ``published_window`` (see ``published_window.resolve_window``) limits
    downloads to documents whose detected published date falls inside it;
    the per-verdict counts are copied into ``window_stats_out`` when given.

    Returns True when metadata refresh succeeded or was intentionally skipped.
    Returns False when metadata refresh failed after retries; crawling still
    continues using the last known active source set.
    """
    cfg = load_config(
        config_path,
        overrides={
            "crawler": {
                "dry_run": dry_run,
                "max_docs_per_source": max_docs_per_source,
                "published_window": published_window,
            }
        },
    )

    # Validate resource limits before any lock, database or network action.
    limits = _validated_cycle_limits(cfg)

    # Shared across host-parallel workers (deep copies keep this object).
    run_download_counter(cfg)

    # Lock the resolved database path before schema writes or external work.
    with exclusive_writer(cfg.storage.get("db_path"), allow_nested=True):
        init_db(cfg.storage.get("db_path"), cfg.storage.get("files_dir"))
        result = _run_cycle(cfg, refresh_metadata_enabled, backup_enabled, limits)
        if not limits["dry_run"] and wayback.enabled():
            conn = get_connection(cfg.storage.get("db_path"))
            try:
                counts = wayback.preserve(conn, cfg.crawler.get("user_agent", "FOIAArchiveBot/0.1"))
                logger.info("Wayback preservation: %s", counts)
            except Exception as exc:  # noqa: BLE001 - preservation never fails a crawl
                logger.warning("Wayback preservation failed: %s", type(exc).__name__)
            finally:
                conn.close()
    window = pw.window_from_config(cfg)
    if window is not None:
        counts = pw.stats(cfg)
        logger.info("Published-date window %s: %s", window.as_dict(), counts)
        if window_stats_out is not None:
            window_stats_out.update(counts)
            window_stats_out["window"] = window.as_dict()
    return result


def _validated_cycle_limits(cfg) -> dict:
    """FA-010 scale policy: fail closed on unbounded or unthrottled settings."""
    source_limit = _bounded_crawl_count(
        cfg.crawler.get("source_limit"), fallback=10000, label="source"
    )
    max_docs = _bounded_crawl_count(
        cfg.crawler.get("max_docs_per_source"),
        fallback=10, label="per-source document",
    )
    dry_run_flag = _validated_dry_run(cfg.crawler.get("dry_run", True))
    cycle_limit_seconds = _bounded_crawl_count(
        cfg.crawler.get("maximum_cycle_seconds"),
        fallback=3600, label="cycle seconds", maximum=86400,
    )
    delay_raw = cfg.crawler.get("per_host_delay_seconds", 0.5)
    try:
        if type(delay_raw) is bool:
            raise ValueError("Boolean delay invalid")
        per_host_delay = float(delay_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid per-host request delay") from exc
    if not math.isfinite(per_host_delay) or not 0 <= per_host_delay <= 60:
        raise ValueError("Unbounded per-host request delay")
    if not dry_run_flag and per_host_delay <= 0:
        raise ValueError("Live crawling requires a positive per-host delay")
    _validate_page_limits(cfg.crawler)
    pw.window_from_config(cfg)  # fail closed on a malformed window
    parallel_hosts = _bounded_integer(
        cfg.crawler.get("parallel_hosts", 1), 1, 16, "parallel_hosts"
    )
    return {
        "parallel_hosts": parallel_hosts,
        "source_limit": source_limit,
        "max_docs": max_docs,
        "dry_run": dry_run_flag,
        "cycle_limit_seconds": cycle_limit_seconds,
        "per_host_delay": per_host_delay,
    }


def _host_group(url: str) -> str:
    from .scraper_core import registrable_domain
    try:
        return registrable_domain(urlsplit(url).hostname or "") or url
    except ValueError:
        return url


def _merge_counts(target: dict, source: dict) -> None:
    for key, value in (source or {}).items():
        if isinstance(value, int) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value


def _crawl_host_groups(cfg, pending, room_by_id, parallel, crawl_one, stop) -> None:
    """Crawl different organisations concurrently, each one sequentially.

    Sources sharing a registrable domain stay in one worker, so per-host
    politeness is unchanged; the shared rate limiter is thread-safe. Each
    worker uses its own config copy for per-source crawl state; counters are
    merged back afterwards.
    """
    groups: dict = {}
    for source_id in pending:
        room = room_by_id.get(source_id)
        key = _host_group(room["url"]) if room is not None else f"missing-{source_id}"
        groups.setdefault(key, []).append(source_id)
    ordered = sorted(groups.values(), key=len, reverse=True)
    merge_lock = threading.Lock()

    def worker(source_ids):
        local_cfg = Config(copy.deepcopy(cfg.data))
        # Counters start empty per worker and are merged back once.
        for key in (pw.STATS_KEY, "_date_probe_totals"):
            local_cfg.data.pop(key, None)
        try:
            for source_id in source_ids:
                if stop.is_set():
                    break
                crawl_one(source_id, local_cfg)
        finally:
            with merge_lock:
                for key in (pw.STATS_KEY, "_date_probe_totals"):
                    if key in local_cfg.data:
                        _merge_counts(cfg.data.setdefault(key, {}), local_cfg.data[key])
                if local_cfg.data.get("_storage_quota_reached"):
                    cfg.data["_storage_quota_reached"] = True

    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="crawl") as pool:
        for future in [pool.submit(worker, group) for group in ordered]:
            future.result()


def _run_cycle(cfg, refresh_metadata_enabled, backup_enabled, limits):
    cycle_start = time.monotonic()

    metadata_ok = True
    if refresh_metadata_enabled:
        logger.info("Refreshing metadata from FOIA Hub")
        try:
            refresh_metadata(cfg)
        except Exception as exc:
            metadata_ok = False
            logger.warning(
                "FOIA.gov metadata refresh failed after retries; continuing "
                "with the last known active source set: %s: %s",
                type(exc).__name__,
                exc,
            )
    else:
        logger.info(
            "Skipping FOIA.gov metadata refresh for this crawl cycle "
            "(refresh cadence not yet due)"
        )

    source_limit = limits["source_limit"]
    # Enforce the cap locally even if a provider adapter ignores its limit.
    rooms = get_reading_rooms_to_crawl(cfg, limit=source_limit)[:source_limit]
    logger.info(
        "Crawling %s reading rooms (source limit %s)", len(rooms), source_limit
    )

    dry_run_flag = limits["dry_run"]
    max_docs = limits["max_docs"]
    rate_limiter = HostRateLimiter(limits["per_host_delay"])
    journal = RunJournal(cfg.storage.get("db_path"), policy_fingerprint(cfg), [rr["id"] for rr in rooms])
    completed = journal.completed_sources()
    source_failures = False
    # Resume the original bounded plan, rather than silently shifting its denominator.
    room_by_id = {rr["id"]: rr for rr in rooms}
    pending = [source_id for source_id in journal.source_ids if source_id not in completed]
    state = {"failures": False, "cap": False, "time": False}
    lock = threading.Lock()
    stop = threading.Event()

    def crawl_one(source_id, local_cfg) -> None:
        if stop.is_set():
            return
        if time.monotonic() - cycle_start >= limits["cycle_limit_seconds"]:
            # Remaining sources stay unreceipted; the journal resumes them
            # in the next cycle with the same policy fingerprint.
            with lock:
                if not state["time"]:
                    logger.warning(
                        "Crawler cycle time budget exhausted; remaining sources deferred"
                    )
                state["time"] = True
            stop.set()
            return
        journal.source_started(source_id)
        if source_id not in room_by_id:
            journal.source_finished(source_id, "SOURCE_NOT_ELIGIBLE")
            state["failures"] = True
            return
        try:
            crawl_reading_room(
                source_id, local_cfg, dry_run=dry_run_flag,
                max_docs=max_docs, rate_limiter=rate_limiter,
            )
            failed = journal.source_failed(source_id)
            journal.source_finished(source_id, "SOURCE_CRAWL_FAILED" if failed else None)
            if failed:
                state["failures"] = True
            if local_cfg.data.get("_storage_quota_reached"):
                state["cap"] = True
                stop.set()
        except sqlite3.Error:
            raise
        except Exception as exc:
            _record_unexpected_source_failure(local_cfg, source_id, exc)
            journal.source_finished(source_id, type(exc).__name__)
            state["failures"] = True

    parallel = limits.get("parallel_hosts", 1)
    try:
        if parallel <= 1:
            for source_id in pending:
                crawl_one(source_id, cfg)
                if stop.is_set():
                    break
        else:
            _crawl_host_groups(cfg, pending, room_by_id, parallel, crawl_one, stop)
        source_failures = state["failures"]
        cap_reached = state["cap"]
        time_budget_exhausted = state["time"]
        journal.checkpoint("PENDING" if backup_enabled else "EXTERNAL_CHECKPOINT_REQUIRED")
        if time_budget_exhausted:
            journal.finish(metadata_ok=metadata_ok, state="INTERRUPTED",
                           reason="cycle_time_budget_exhausted")
        else:
            journal.finish(
                metadata_ok=metadata_ok,
                state="DEGRADED" if source_failures or not metadata_ok else "COMPLETED",
                reason="storage_cap_reached" if cap_reached
                else "source_or_metadata_failure" if source_failures or not metadata_ok
                else "bounded_wave_finished",
            )
    except BaseException as exc:
        journal.finish(metadata_ok=metadata_ok,
                       state="FAILED" if isinstance(exc, Exception) else "INTERRUPTED",
                       reason=type(exc).__name__)
        raise

    try:
        if backup_enabled:
            checkpoint = backup_database_to_b2(cfg)
            journal.checkpoint("VERIFIED" if checkpoint is not None else "DEFERRED", checkpoint.key if checkpoint is not None else None)
    except Exception as exc:
        journal.checkpoint("FAILED")
        journal.finish(metadata_ok=metadata_ok, state="DEGRADED", reason="checkpoint_failure")
        logger.warning("SQLite checkpoint failed for run %s; next cycle must retry (%s: %s)",
                       journal.id, type(exc).__name__, str(exc)[:300])

    logger.info("Ingestion run %s finished; durable receipt retained", journal.id)
    return metadata_ok

