"""FC-03: normalize official FOIA request/release logs into request records.

A log row describes a *request* (tracking number, dates, disposition); it is
never a released document and is stored separately from ``documents``.
Each ingested log keeps its raw bytes hash and source URL, every row keeps
its row number, raw cells, per-field mapping confidence and parse warnings,
and rows that cannot be normalized go to a failed-row queue instead of being
dropped or guessed. Supported formats: CSV/TSV and XLSX; PDF and scanned
logs are queued as unsupported until a reviewed extraction path exists.

Text-layer PDF logs are read table-by-table with ``pdfplumber`` when it is
installed (every PDF-derived field is confidence-discounted and warned);
scanned PDFs without a text layer stay queued as unsupported. Legacy ``.xls``
workbooks are read with ``xlrd`` when installed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_LOG_BYTES = 50 * 1024 * 1024
MAX_ROWS = 200_000
MAX_CELL_CHARS = 20_000

# Canonical field -> known header spellings (lower-case, punctuation-free).
FIELD_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "tracking_number": (
        "tracking number", "request number", "request id", "foia request number",
        "control number", "case number", "request no", "foia number", "log number",
        "tracking no", "request tracking number", "foia control number",
    ),
    "request_description": (
        "request description", "description", "subject", "records requested",
        "summary", "request summary", "short description", "description of request",
        "subject of request",
    ),
    "received_date": (
        "received date", "date received", "received", "request date",
        "date of request", "initial received date", "submitted date", "date submitted",
    ),
    "closed_date": (
        "closed date", "date closed", "closed", "completion date", "date completed",
        "close date", "final response date", "response date", "completed date",
        "date closed completed", "closed completed date", "final disposition date",
        "closure date", "date of final response",
    ),
    "disposition": (
        "disposition", "final disposition", "status", "outcome", "closure disposition",
        "action taken", "request status",
    ),
    "requester_name": (
        "requester", "requester name", "requestor", "requestor name", "name",
        "requester full name",
    ),
    "requester_organization": (
        "organization", "requester organization", "requestor organization",
        "company", "affiliation",
    ),
    "component": ("component", "office", "sub agency", "subagency", "bureau", "division"),
    "exemptions": ("exemptions", "exemptions cited", "exemption", "foia exemptions"),
    "released_record_reference": (
        "released records", "release url", "link", "url", "document link",
        "reading room link", "released documents",
    ),
    "pages_released": (
        "pages released", "number of pages released", "total pages released",
        "released pages", "pages produced", "number of pages produced",
        "page count released", "pages",
    ),
}
PERSONAL_FIELDS = frozenset({"requester_name"})
DATE_FIELDS = frozenset({"received_date", "closed_date"})
DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d-%b-%Y", "%d-%b-%y",
    "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
)
EXCEL_EPOCH = date(1899, 12, 30)
MAX_PDF_PAGES = 2_000
PDF_CONFIDENCE_FACTOR = 0.8
STORED_COLUMNS = ("tracking_number", "request_description", "received_date", "closed_date",
                  "disposition", "requester_name", "requester_organization", "component",
                  "exemptions", "released_record_reference", "pages_released")


class RequestLogError(ValueError):
    pass


def _header_key(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()


def map_columns(headers: List[Any]) -> Dict[int, Tuple[str, float]]:
    """Map column index -> (canonical field, confidence). Unmapped stay raw."""
    mapping: Dict[int, Tuple[str, float]] = {}
    taken: Dict[str, float] = {}
    candidates = []
    for index, header in enumerate(headers):
        key = _header_key(header)
        if not key:
            continue
        for canonical, synonyms in FIELD_SYNONYMS.items():
            if key == canonical.replace("_", " "):
                candidates.append((1.0, index, canonical))
            elif key in synonyms:
                candidates.append((0.9, index, canonical))
            else:
                tokens = set(key.split())
                if any(len(set(s.split())) > 1 and set(s.split()) <= tokens for s in synonyms):
                    candidates.append((0.6, index, canonical))
    # Highest confidence first; each column and field is used at most once.
    for confidence, index, canonical in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if index in mapping or canonical in taken:
            continue
        mapping[index] = (canonical, confidence)
        taken[canonical] = confidence
    return mapping


def parse_date(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return (ISO date, warning). Never guesses an ambiguous/invalid value."""
    if value is None or value == "":
        return None, None
    if isinstance(value, datetime):
        return value.date().isoformat(), None
    if isinstance(value, date):
        return value.isoformat(), None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 <= value <= 80000:  # plausible Excel serial (1954..2119)
            return (EXCEL_EPOCH + timedelta(days=int(value))).isoformat(), "excel_serial_date"
        return None, "unparseable_date"
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if not 1966 <= parsed.year <= 2100:  # FOIA enacted 1966
            return None, "implausible_date"
        return parsed.isoformat(), None
    return None, "unparseable_date"


@dataclass
class NormalizedLog:
    log_id: str
    source_url: str
    agency: Optional[str]
    raw_sha256: str
    file_format: str
    column_mapping: Dict[str, Dict[str, Any]]
    rows: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)


def _detect_format(filename: str, raw: bytes) -> str:
    lower = filename.lower()
    if raw[:4] == b"PK\x03\x04" or lower.endswith(".xlsx"):
        return "xlsx"
    if raw[:5] == b"%PDF-" or lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(".xls") or (raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                                  and not lower.endswith(".doc")):
        return "xls" if _xlrd_available() else "legacy_office"
    if lower.endswith(".doc"):
        return "legacy_office"
    if lower.endswith(".tsv"):
        return "tsv"
    return "csv"


def _xlrd_available() -> bool:
    try:
        import xlrd  # noqa: F401
    except ImportError:
        return False
    return True


def _pdfplumber_available() -> bool:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        return False
    return True


def _clean_pdf_cell(value: Any) -> Any:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip()


def _read_pdf_tables(raw: bytes) -> List[Tuple[str, int, List[Any]]]:
    """Rows from the text layer of a tabular PDF log (one table stream)."""
    import pdfplumber

    rows: List[Tuple[str, int, List[Any]]] = []
    number = 0
    try:
        pdf = pdfplumber.open(io.BytesIO(raw))
    except Exception as exc:
        raise RequestLogError(f"Unreadable PDF request log: {type(exc).__name__}") from None
    with pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise RequestLogError("PDF request log exceeds the page limit")
        for page_number, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables() or []:
                for cells in table:
                    number += 1
                    cleaned = [_clean_pdf_cell(c) for c in cells]
                    if any(c not in (None, "") for c in cleaned):
                        rows.append((f"page-{page_number}", number, cleaned))
                    if len(rows) > MAX_ROWS:
                        raise RequestLogError("Request log exceeds the row limit")
    return rows


def _read_tabular(raw: bytes, file_format: str) -> List[Tuple[str, int, List[Any]]]:
    """Return (sheet, 1-based row number, cells) for every non-empty row."""
    rows: List[Tuple[str, int, List[Any]]] = []
    if file_format in ("csv", "tsv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if file_format == "tsv" else ",")
        for number, cells in enumerate(reader, start=1):
            if any(c.strip() for c in cells):
                rows.append(("csv", number, cells))
            if len(rows) > MAX_ROWS:
                raise RequestLogError("Request log exceeds the row limit")
        return rows
    if file_format == "xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - dependency is in requirements
            raise RequestLogError("XLSX support requires openpyxl") from exc
        try:
            workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception as exc:  # corrupt/encrypted/not-a-workbook zip
            raise RequestLogError(f"Unreadable XLSX request log: {type(exc).__name__}") from None
        try:
            sheet = workbook.worksheets[0]
            for number, cells in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = list(cells)
                if any(c not in (None, "") for c in cells):
                    rows.append((sheet.title, number, cells))
                if len(rows) > MAX_ROWS:
                    raise RequestLogError("Request log exceeds the row limit")
        finally:
            workbook.close()
        return rows
    if file_format == "xls":
        import xlrd

        try:
            book = xlrd.open_workbook(file_contents=bytes(raw), on_demand=True)
        except Exception as exc:
            raise RequestLogError(f"Unreadable XLS request log: {type(exc).__name__}") from None
        try:
            sheet = book.sheet_by_index(0)
            for index in range(sheet.nrows):
                cells = []
                for cell in sheet.row(index):
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                        except Exception:
                            pass
                    elif isinstance(value, float) and value.is_integer():
                        value = int(value)
                    cells.append(value)
                if any(c not in (None, "") for c in cells):
                    rows.append((sheet.name, index + 1, cells))
                if len(rows) > MAX_ROWS:
                    raise RequestLogError("Request log exceeds the row limit")
        finally:
            book.release_resources()
        return rows
    if file_format == "pdf":
        return _read_pdf_tables(raw)
    raise RequestLogError(f"Unsupported request log format: {file_format}")


def _find_header(rows) -> int:
    """Logs often have title rows; use the first row mapping >= 2 fields."""
    for position, (_, _, cells) in enumerate(rows[:25]):
        if len(map_columns(cells)) >= 2:
            return position
    raise RequestLogError("No recognizable request-log header row")


def normalize_request_log(
    raw: bytes, *, source_url: str, filename: str = "", agency: Optional[str] = None,
) -> NormalizedLog:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise RequestLogError("Empty request log")
    if len(raw) > MAX_LOG_BYTES:
        raise RequestLogError("Request log exceeds the size limit")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    log_id = "log_" + hashlib.sha256(f"{source_url}\x00{raw_sha256}".encode()).hexdigest()[:24]
    file_format = _detect_format(filename or source_url, bytes(raw))
    def queued(reason: str) -> NormalizedLog:
        log = NormalizedLog(log_id, source_url, agency, raw_sha256, file_format, {})
        log.failures.append({"row_number": None, "sheet": None,
                             "reason": f"unsupported_format:{reason}", "raw": None})
        return log

    if file_format == "legacy_office" or (file_format == "pdf" and not _pdfplumber_available()):
        return queued(file_format)
    try:
        rows = _read_tabular(bytes(raw), file_format)
    except RequestLogError:
        if file_format != "pdf":
            raise
        return queued("pdf_unreadable")
    if file_format == "pdf" and not rows:
        # No text-layer table: a scanned log. Queue it rather than guess (OCR
        # of tabular logs is not a reviewed path).
        return queued("pdf_no_text_table")
    header_position = _find_header(rows)
    _, _, header = rows[header_position]
    mapping = map_columns(header)
    log = NormalizedLog(
        log_id, source_url, agency, raw_sha256, file_format,
        {canonical: {"column": index, "header": str(header[index]), "confidence": confidence}
         for index, (canonical, confidence) in sorted(mapping.items())},
    )
    header_key = [_header_key(h) for h in header]
    pdf = file_format == "pdf"
    for sheet, number, cells in rows[header_position + 1:]:
        if pdf and [_header_key(c) for c in cells] == header_key:
            continue  # header repeated at the top of each PDF page
        raw_cells = {str(header[i]) if i < len(header) and header[i] not in (None, "") else f"column_{i + 1}":
                     (None if c is None else str(c)[:MAX_CELL_CHARS]) for i, c in enumerate(cells)}
        record: Dict[str, Any] = {canonical: None for canonical in FIELD_SYNONYMS}
        confidence: Dict[str, float] = {}
        warnings: List[str] = []
        for index, (canonical, column_confidence) in mapping.items():
            value = cells[index] if index < len(cells) else None
            if canonical in DATE_FIELDS:
                parsed, warning = parse_date(value)
                record[canonical] = parsed
                if warning:
                    warnings.append(f"{canonical}:{warning}")
                if parsed:
                    confidence[canonical] = column_confidence * (0.8 if warning else 1.0)
            else:
                text = None if value is None else str(value).strip()[:MAX_CELL_CHARS] or None
                record[canonical] = text
                if text:
                    confidence[canonical] = column_confidence
        if pdf:
            confidence = {k: round(v * PDF_CONFIDENCE_FACTOR, 3) for k, v in confidence.items()}
            warnings.append("pdf_table_extraction")
        if not record["tracking_number"] and not record["request_description"]:
            log.failures.append({"row_number": number, "sheet": sheet,
                                 "reason": "no_tracking_number_or_description", "raw": raw_cells})
            continue
        if record["received_date"] and record["closed_date"] and record["closed_date"] < record["received_date"]:
            warnings.append("closed_before_received")
        log.rows.append({"row_number": number, "sheet": sheet, **record,
                         "field_confidence": confidence, "warnings": warnings, "raw": raw_cells})
    return log


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS request_logs (
            log_id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            agency TEXT,
            raw_sha256 TEXT NOT NULL,
            file_format TEXT NOT NULL,
            column_mapping_json TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            failed_row_count INTEGER NOT NULL,
            ingested_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS request_log_rows (
            log_id TEXT NOT NULL REFERENCES request_logs(log_id) ON DELETE CASCADE,
            row_number INTEGER NOT NULL,
            sheet TEXT,
            tracking_number TEXT,
            request_description TEXT,
            received_date TEXT,
            closed_date TEXT,
            disposition TEXT,
            requester_name TEXT,
            requester_organization TEXT,
            component TEXT,
            exemptions TEXT,
            released_record_reference TEXT,
            pages_released TEXT,
            field_confidence_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (log_id, sheet, row_number)
        )"""
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(request_log_rows)")}
    if "pages_released" not in existing:
        conn.execute("ALTER TABLE request_log_rows ADD COLUMN pages_released TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_request_log_rows_tracking ON request_log_rows(tracking_number)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS request_log_failures (
            log_id TEXT NOT NULL REFERENCES request_logs(log_id) ON DELETE CASCADE,
            row_number INTEGER,
            sheet TEXT,
            reason TEXT NOT NULL,
            raw_json TEXT,
            queued_at TEXT NOT NULL
        )"""
    )


def store_request_log(conn: sqlite3.Connection, log: NormalizedLog) -> bool:
    """Persist atomically. Returns False when this exact log was already stored."""
    create_tables(conn)
    if conn.execute("SELECT 1 FROM request_logs WHERE log_id=?", (log.log_id,)).fetchone():
        return False
    stamp = datetime.now(timezone.utc).isoformat()
    columns = STORED_COLUMNS
    with conn:
        conn.execute(
            """INSERT INTO request_logs(log_id,source_url,agency,raw_sha256,file_format,
                   column_mapping_json,row_count,failed_row_count,ingested_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (log.log_id, log.source_url, log.agency, log.raw_sha256, log.file_format,
             json.dumps(log.column_mapping, sort_keys=True), len(log.rows), len(log.failures), stamp),
        )
        conn.executemany(
            f"""INSERT INTO request_log_rows(log_id,row_number,sheet,{','.join(columns)},
                   field_confidence_json,warnings_json,raw_json)
                VALUES(?,?,?,{','.join('?' * len(columns))},?,?,?)""",
            [(log.log_id, row["row_number"], row["sheet"], *(row[c] for c in columns),
              json.dumps(row["field_confidence"], sort_keys=True), json.dumps(row["warnings"]),
              json.dumps(row["raw"], sort_keys=True)) for row in log.rows],
        )
        conn.executemany(
            "INSERT INTO request_log_failures(log_id,row_number,sheet,reason,raw_json,queued_at) VALUES(?,?,?,?,?,?)",
            [(log.log_id, f["row_number"], f["sheet"], f["reason"],
              None if f["raw"] is None else json.dumps(f["raw"], sort_keys=True), stamp)
             for f in log.failures],
        )
    return True


def request_log_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    create_tables(conn)
    row = conn.execute(
        """SELECT COUNT(*) AS logs, COALESCE(SUM(row_count),0) AS rows,
                  COALESCE(SUM(failed_row_count),0) AS failed_rows FROM request_logs"""
    ).fetchone()
    distinct = conn.execute(
        """SELECT COUNT(DISTINCT COALESCE(l.agency,'') || '|' || r.tracking_number)
           FROM request_log_rows r JOIN request_logs l USING(log_id)
           WHERE r.tracking_number IS NOT NULL"""
    ).fetchone()[0]
    return {"logs": row["logs"], "rows": row["rows"], "failed_rows": row["failed_rows"],
            "distinct_tracked_requests": distinct}
