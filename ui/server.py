"""FastAPI UI for browsing FOIA archive documents."""
from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from foia_archive.archive_storage import (
    ArchiveStorageError,
    B2ArchiveStorage,
    LocalArchiveStorage,
    get_archive_storage,
)
from foia_archive.archive_status import database_present, resolve_web_database
from foia_archive.database_backup import list_database_backups
from foia_archive.export_extensions import (
    export_log_rows_page,
    export_relations_page,
    export_text_page,
    load_extension_schema,
    log_coverage_files,
)
from foia_archive import sfl_interchange
from foia_archive.identity import related_versions
from foia_archive.export_contract import (
    ExportCursorError,
    export_manifest,
    export_page,
    load_schema,
)
from foia_archive.incident_ledger import list_incidents
from foia_archive.storage import (
    SORT_ORDERS,
    get_admin_dashboard_stats,
    get_archive_stats,
    get_connection,
    get_document_detail,
    get_document_source_details,
    get_schema_version,
    init_db,
    query_document_snippets,
    query_documents_page,
)
from foia_archive.utils import load_config

config = load_config("config/settings.yaml")
CONFIGURED_DB_PATH = Path(config.storage.get("db_path"))
FILES_DIR = Path(config.storage.get("files_dir"))
# Start cleanly without B2: serve an empty placeholder index (never at the
# configured path, so a later verified bootstrap is not blocked).
DB_PATH, ARCHIVE_STATUS = resolve_web_database(config)
init_db(DB_PATH, FILES_DIR)

app = FastAPI(title="FOIA Aggregator")
templates = Jinja2Templates(directory="ui/templates")
PUBLIC_BASE_URL = (os.getenv("FOIA_PUBLIC_BASE_URL") or "").strip().rstrip("/")
PROCESS_STARTED_AT = datetime.now(timezone.utc)
ADMIN_SECURITY = HTTPBasic(auto_error=False)
_ADMIN_STORAGE_CACHE = {"updated": 0.0, "data": None}
app.mount("/static", StaticFiles(directory="ui/static"), name="static")
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")


def human_file_size(value: Optional[int]) -> str:
    if value is None:
        return "Unknown"
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value)} B"


templates.env.filters["filesize"] = human_file_size


def human_timestamp(value: Optional[str]) -> str:
    if not value:
        return "Not yet updated"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%b %-d, %Y · %H:%M UTC")
    except (TypeError, ValueError):
        return raw


templates.env.filters["timestamp"] = human_timestamp


def _require_admin(
    credentials: Optional[HTTPBasicCredentials] = Depends(ADMIN_SECURITY),
) -> str:
    expected_password = (os.getenv("FOIA_ADMIN_PASSWORD") or "").strip()
    if not expected_password:
        raise HTTPException(status_code=404, detail="Admin dashboard disabled")

    expected_username = (
        os.getenv("FOIA_ADMIN_USERNAME") or "admin"
    ).strip() or "admin"
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Administrator authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_username.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid administrator credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return expected_username


def _admin_storage_metrics() -> dict:
    now = time.monotonic()
    cached = _ADMIN_STORAGE_CACHE.get("data")
    updated = float(_ADMIN_STORAGE_CACHE.get("updated") or 0.0)
    if cached is not None and now - updated < 300:
        return cached

    result = {
        "backend": "unknown",
        "error": None,
        "stored_bytes": 0,
        "document_bytes": 0,
        "backup_bytes": 0,
        "other_bytes": 0,
        "version_count": 0,
        "current_object_count": 0,
        "document_cap_bytes": 0,
        "target_total_bytes": 9_000_000_000,
        "latest_backup_at": None,
        "backup_count": 0,
    }
    try:
        backend = get_archive_storage(config)
        result["backend"] = backend.name
        if isinstance(backend, B2ArchiveStorage):
            usage = backend.usage_summary()
            result.update(
                {
                    "stored_bytes": usage.stored_bytes,
                    "document_bytes": usage.document_bytes,
                    "backup_bytes": usage.backup_bytes,
                    "other_bytes": usage.other_bytes,
                    "version_count": usage.version_count,
                    "current_object_count": usage.current_object_count,
                    "document_cap_bytes": backend.max_archive_bytes,
                }
            )
            backups = list_database_backups(config)
            result["backup_count"] = len(backups)
            if backups:
                latest = backups[0].get("LastModified")
                result["latest_backup_at"] = (
                    latest.isoformat()
                    if hasattr(latest, "isoformat")
                    else str(latest or "")
                )
    except Exception as exc:
        result["error"] = str(exc)

    target_raw = (
        os.getenv("FOIA_EXPANSION_TARGET_TOTAL_BYTES") or "9000000000"
    ).strip()
    try:
        result["target_total_bytes"] = max(1, int(target_raw))
    except ValueError:
        result["target_total_bytes"] = 9_000_000_000

    _ADMIN_STORAGE_CACHE["updated"] = now
    _ADMIN_STORAGE_CACHE["data"] = result
    return result


def _public_origin(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _canonical_url(request: Request) -> str:
    origin = _public_origin(request)
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{origin}{request.url.path}{query}"


def _template_context(request: Request, **extra):
    return {
        "canonical_url": _canonical_url(request),
        "archive_status": ARCHIVE_STATUS.public(),
        **extra,
    }


@app.middleware("http")
async def public_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if request.url.scheme == "https" or forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


@app.exception_handler(404)
async def not_found_page(request: Request, exc):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context=_template_context(
            request,
            status_code=404,
            heading="Record or page not found",
            message=(
                "The requested page is not in this archive. "
                "Try searching the released-records index instead."
            ),
        ),
        status_code=404,
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt(request: Request):
    origin = _public_origin(request)
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /archive/\n"
        f"Sitemap: {origin}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    origin = _public_origin(request)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM documents ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    urls = [f"{origin}/", f"{origin}/about"]
    urls.extend(f"{origin}/record/{row['id']}" for row in rows)
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    body.extend(
        f"  <url><loc>{escape(url)}</loc></url>"
        for url in urls
    )
    body.append("</urlset>")
    return Response(
        content="\n".join(body),
        media_type="application/xml",
    )


def _adopt_restored_database() -> None:
    """Switch from the placeholder once a verified snapshot/writer DB appears."""
    global DB_PATH, ARCHIVE_STATUS
    if not ARCHIVE_STATUS.serving_placeholder_index:
        return
    if not database_present(CONFIGURED_DB_PATH):
        return
    path, status = resolve_web_database(config)
    if path == CONFIGURED_DB_PATH:
        init_db(path, FILES_DIR)
        DB_PATH, ARCHIVE_STATUS = path, status


def _has_relations(conn: sqlite3.Connection) -> bool:
    # Read path must not create tables on an older restored snapshot.
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_relations'"
    ).fetchone() is not None


def get_db() -> sqlite3.Connection:
    _adopt_restored_database()
    return get_connection(DB_PATH)


def fetch_agencies(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM agencies ORDER BY name").fetchall()


def fetch_offices(conn: sqlite3.Connection, agency_id: Optional[int] = None) -> List[sqlite3.Row]:
    if agency_id:
        return conn.execute(
            "SELECT id, name FROM offices WHERE agency_id = ? ORDER BY name",
            (agency_id,),
        ).fetchall()
    return conn.execute("SELECT id, name FROM offices ORDER BY name").fetchall()


def fetch_file_types(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT DISTINCT file_type FROM documents WHERE file_type IS NOT NULL").fetchall()
    return [r[0] for r in rows if r[0]]


def _optional_int_filter(value: Optional[str], name: str) -> Optional[int]:
    """Normalize an optional select value from an HTML GET form.

    Browsers submit the "All ..." options as an empty string. Treat that as no
    filter rather than letting FastAPI reject the request before the search
    route can run.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be an integer when specified",
        )
    if parsed <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be a positive integer when specified",
        )
    return parsed


def _page_url(
    page: int,
    *,
    title_query: Optional[str],
    agency_id: Optional[int],
    office_id: Optional[int],
    file_type: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    sort: str,
    page_size: int,
) -> str:
    params = {
        "q": title_query or None,
        "agency_id": agency_id,
        "office_id": office_id,
        "file_type": file_type,
        "start_date": start_date,
        "end_date": end_date,
        "sort": sort if sort != "discovered_desc" else None,
        "page_size": page_size if page_size != 50 else None,
        "page": page if page != 1 else None,
    }
    return "/?" + urlencode(
        {key: value for key, value in params.items() if value not in (None, "")}
    )


@app.get("/healthz")
async def healthz():
    conn = get_db()
    try:
        conn.execute("SELECT 1").fetchone()
        schema_version = get_schema_version(conn)
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable: {exc}",
        ) from exc
    finally:
        conn.close()
    # The web process is healthy without B2; archive connectivity is reported
    # separately so a heartbeat is never mistaken for a loaded archive.
    return {
        "status": "ok",
        "schema_version": schema_version,
        "archive": ARCHIVE_STATUS.public(),
    }


def _export_rate_per_minute() -> int:
    try:
        return max(1, min(6000, int(os.getenv("FOIA_EXPORT_RATE_PER_MINUTE") or 120)))
    except ValueError:
        return 120


_EXPORT_RATE: dict = {}


def _export_rate_limited(request: Request) -> Optional[JSONResponse]:
    """Per-client fixed-window limit for the research export API (FC-13)."""
    now = time.monotonic()
    client = request.client.host if request.client else "unknown"
    window, count = _EXPORT_RATE.get(client, (now, 0))
    if now - window >= 60:
        window, count = now, 0
    if count >= _export_rate_per_minute():
        retry = max(1, int(60 - (now - window)))
        return JSONResponse({"detail": "Export rate limit exceeded"}, status_code=429,
                            headers={"Retry-After": str(retry)})
    if len(_EXPORT_RATE) > 10000:  # bound memory under many clients
        _EXPORT_RATE.clear()
    _EXPORT_RATE[client] = (window, count + 1)
    return None


@app.get("/api/v1/export/schema")
async def export_schema(request: Request):
    return _export_rate_limited(request) or JSONResponse(load_schema())


@app.get("/api/v1/export/manifest")
async def export_manifest_endpoint(request: Request):
    limited = _export_rate_limited(request)
    if limited:
        return limited
    conn = get_db()
    try:
        manifest = export_manifest(conn)
    finally:
        conn.close()
    manifest["archive"] = ARCHIVE_STATUS.public()
    return JSONResponse(manifest)


@app.get("/api/v1/records")
async def export_records(request: Request, cursor: Optional[str] = None, limit: int = 100):
    limited = _export_rate_limited(request)
    if limited:
        return limited
    conn = get_db()
    try:
        page = export_page(conn, cursor=cursor, limit=limit)
    except ExportCursorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return JSONResponse(page)


@app.get("/api/v1/export/schemas/{name}")
async def export_extension_schema(request: Request, name: str):
    limited = _export_rate_limited(request)
    if limited:
        return limited
    try:
        return JSONResponse(load_extension_schema(name))
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown schema") from None


def _paged_export(pager, cursor, limit):
    conn = get_db()
    try:
        return JSONResponse(pager(conn, cursor=cursor, limit=limit))
    except ExportCursorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@app.get("/api/v1/export/document-text")
async def export_document_text(request: Request, cursor: Optional[str] = None, limit: int = 20):
    return _export_rate_limited(request) or _paged_export(export_text_page, cursor, limit)


@app.get("/api/v1/export/request-log-rows")
async def export_request_log_rows(request: Request, cursor: Optional[str] = None, limit: int = 200):
    return _export_rate_limited(request) or _paged_export(export_log_rows_page, cursor, limit)


@app.get("/api/v1/export/document-relations")
async def export_document_relations(request: Request, cursor: Optional[str] = None, limit: int = 500):
    return _export_rate_limited(request) or _paged_export(export_relations_page, cursor, limit)


def _log_coverage_files():
    conn = get_db()
    try:
        return log_coverage_files(conn)
    finally:
        conn.close()


@app.get("/api/v1/export/log-coverage/manifest.json")
async def export_log_coverage_manifest(request: Request):
    limited = _export_rate_limited(request)
    if limited:
        return limited
    return Response(_log_coverage_files()[0], media_type="application/json")


@app.get("/api/v1/export/log-coverage/coverage.jsonl")
async def export_log_coverage_records(request: Request):
    limited = _export_rate_limited(request)
    if limited:
        return limited
    return Response(_log_coverage_files()[1], media_type="application/x-ndjson")


MIN_INGEST_TOKEN_LENGTH = 24


@app.post("/api/v1/ingest/sfl-bundle")
async def ingest_sfl_bundle(request: Request):
    """Receive a Send_FOIA_Log_Requests bundle; disabled unless FOIA_INGEST_TOKEN is set."""
    token = (os.getenv("FOIA_INGEST_TOKEN") or "").strip()
    if len(token) < MIN_INGEST_TOKEN_LENGTH:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        supplied.strip().encode("utf-8"), token.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Bearer token required",
                            headers={"WWW-Authenticate": "Bearer"})
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > sfl_interchange.MAX_ENVELOPE_BYTES:
        raise HTTPException(status_code=413, detail="Bundle too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > sfl_interchange.MAX_ENVELOPE_BYTES:
            raise HTTPException(status_code=413, detail="Bundle too large")
    try:
        envelope = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Body must be a JSON envelope") from None
    inbox = Path(os.getenv("FOIA_INGEST_INBOX") or (CONFIGURED_DB_PATH.parent / "sfl-inbox"))
    durable = (os.getenv("FOIA_INGEST_INBOX_DURABLE") or "").strip().lower() in ("1", "true", "yes", "on")
    status, payload = sfl_interchange.receive(envelope, inbox, durable=durable)
    return JSONResponse(payload, status_code=status)


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    admin_user: str = Depends(_require_admin),
):
    conn = get_db()
    try:
        public_stats = get_archive_stats(conn)
        operational = get_admin_dashboard_stats(conn)
        incidents = list_incidents(conn)
        schema_version = get_schema_version(conn)
    finally:
        conn.close()

    storage_metrics = _admin_storage_metrics()
    target = int(storage_metrics.get("target_total_bytes") or 1)
    storage_metrics["percent_of_target"] = min(
        100.0,
        100.0 * int(storage_metrics.get("stored_bytes") or 0) / target,
    )
    storage_metrics["remaining_to_target"] = max(
        0,
        target - int(storage_metrics.get("stored_bytes") or 0),
    )

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context=_template_context(
            request,
            admin_user=admin_user,
            stats=public_stats,
            operational=operational,
            incidents=incidents,
            storage=storage_metrics,
            schema_version=schema_version,
            database_size=DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            process_started_at=PROCESS_STARTED_AT.isoformat(),
            render_commit=(os.getenv("RENDER_GIT_COMMIT") or "").strip(),
            autonomous_refresh_enabled=(
                (os.getenv("FOIA_AUTONOMOUS_REFRESH_ENABLED") or "")
                .strip()
                .lower()
                == "true"
            ),
        ),
    )


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    conn = get_db()
    try:
        stats = get_archive_stats(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context=_template_context(request, stats=stats),
    )


@app.get("/record/{document_id}", response_class=HTMLResponse)
async def record_detail(request: Request, document_id: int):
    conn = get_db()
    try:
        document = get_document_detail(conn, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        sources = get_document_source_details(conn, document_id)
        related = related_versions(conn, document_id) if _has_relations(conn) else []
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="record.html",
        context=_template_context(
            request,
            document=document,
            sources=sources,
            related=related,
        ),
    )


@app.get("/archive/{document_id}")
async def archived_document(document_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, local_path, storage_backend, storage_key, download_status
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if ARCHIVE_STATUS.reason == "b2_credentials_missing" and row["storage_backend"] == "b2":
        raise HTTPException(
            status_code=503,
            detail="Archived copies are unavailable: the document archive is not connected.",
        )
    if row["download_status"] != "downloaded":
        raise HTTPException(status_code=404, detail="Document is not archived")

    backend_name = row["storage_backend"] or (
        "local" if row["local_path"] else None
    )
    key = row["storage_key"] or row["local_path"]
    if not backend_name or not key:
        raise HTTPException(status_code=404, detail="Archive location is missing")

    try:
        backend = get_archive_storage(config, backend_name=backend_name)
        if isinstance(backend, LocalArchiveStorage):
            path = backend.path_for_key(key)
            if not path.is_file():
                raise HTTPException(status_code=404, detail="Archived file is missing")
            return FileResponse(path)
        return RedirectResponse(
            backend.presigned_url(key, expires_seconds=3600),
            status_code=307,
        )
    except HTTPException:
        raise
    except ArchiveStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Archive storage unavailable: {exc}",
        ) from exc


@app.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: Optional[str] = Query(None, max_length=200),
    agency_id: Optional[str] = Query(None),
    office_id: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    sort: str = Query("discovered_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    agency_filter_id = _optional_int_filter(agency_id, "agency_id")
    office_filter_id = _optional_int_filter(office_id, "office_id")

    if sort not in SORT_ORDERS:
        sort = "discovered_desc"
    if page_size not in {25, 50, 100}:
        page_size = 50

    title_query = (q or "").strip() or None
    conn = get_db()
    try:
        agencies = fetch_agencies(conn)
        offices = fetch_offices(conn, agency_filter_id)
        file_types = fetch_file_types(conn)
        documents, total_results = query_documents_page(
            conn,
            agency_id=agency_filter_id,
            office_id=office_filter_id,
            file_type=file_type,
            start_date=start_date,
            end_date=end_date,
            title_query=title_query,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        stats = get_archive_stats(conn)
        total_pages = max(1, math.ceil(total_results / page_size))
        if total_results and page > total_pages:
            page = total_pages
            documents, total_results = query_documents_page(
                conn,
                agency_id=agency_filter_id,
                office_id=office_filter_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                title_query=title_query,
                sort=sort,
                page=page,
                page_size=page_size,
            )

        snippets = query_document_snippets(
            conn,
            [row["id"] for row in documents],
            title_query,
        )
        document_items = []
        for row in documents:
            item = dict(row)
            item["snippet"] = snippets.get(row["id"])
            document_items.append(item)
    finally:
        conn.close()

    first_result = (page - 1) * page_size + 1 if total_results else 0
    last_result = min(page * page_size, total_results)

    pagination = {
        "page": page,
        "total_pages": total_pages,
        "previous_url": (
            _page_url(
                page - 1,
                title_query=title_query,
                agency_id=agency_filter_id,
                office_id=office_filter_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                sort=sort,
                page_size=page_size,
            )
            if page > 1
            else None
        ),
        "next_url": (
            _page_url(
                page + 1,
                title_query=title_query,
                agency_id=agency_filter_id,
                office_id=office_filter_id,
                file_type=file_type,
                start_date=start_date,
                end_date=end_date,
                sort=sort,
                page_size=page_size,
            )
            if page < total_pages
            else None
        ),
    }

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context=_template_context(
            request,
            agencies=agencies,
            offices=offices,
            file_types=file_types,
            documents=document_items,
            stats=stats,
            filters_active=any(
                (
                    agency_filter_id,
                    office_filter_id,
                    file_type,
                    start_date,
                    end_date,
                    sort != "discovered_desc",
                    page_size != 50,
                )
            ),
            title_query=title_query or "",
            selected_agency=agency_filter_id,
            selected_office=office_filter_id,
            selected_file_type=file_type,
            start_date=start_date,
            end_date=end_date,
            sort=sort,
            page_size=page_size,
            total_results=total_results,
            first_result=first_result,
            last_result=last_result,
            pagination=pagination,
        ),
    )
