"""Decide which SQLite index the read-only web process serves, and say why.

The public web service must start even when Backblaze B2 is not configured
(for example a fresh Render deploy before secrets are entered). In that case
it serves an empty placeholder index at a *separate* path, so the configured
FOIA_DB_PATH stays absent and a later verified B2 bootstrap is never blocked
or overwritten. The web process never publishes checkpoints, so serving a
placeholder can not replace larger verified archive state.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional

from .archive_storage import _storage_backend_name
from .utils import Config

TRUE_VALUES = {"1", "true", "yes", "on"}

# Values the container entrypoint may export after its bootstrap attempt.
BOOTSTRAP_RESULTS = {
    "restored", "existing", "skipped_no_credentials", "failed",
    "no_backup", "disabled",
}

MESSAGES = {
    "b2_credentials_missing": (
        "The document archive is not connected: Backblaze B2 credentials are "
        "not configured, so archived copies are unavailable."
    ),
    "snapshot_not_loaded": (
        "The document archive is not connected: no verified archive snapshot "
        "has been loaded yet, so the search index is empty."
    ),
}


@dataclass(frozen=True)
class ArchiveStatus:
    mode: str  # "connected", "local" or "not_connected"
    archive_connected: bool
    storage_backend: str
    reason: Optional[str]
    message: Optional[str]
    serving_placeholder_index: bool
    bootstrap_result: Optional[str]

    def public(self) -> dict:
        """Safe for /healthz and templates: no paths, keys or bucket names."""
        return asdict(self)


def b2_credentials_present(env: Mapping[str, str] = os.environ) -> bool:
    return bool((env.get("B2_KEY_ID") or "").strip()
                and (env.get("B2_APPLICATION_KEY") or "").strip())


def database_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def placeholder_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.stem}.unconnected{db_path.suffix or '.db'}")


def resolve_web_database(
    config: Config, env: Mapping[str, str] = os.environ,
) -> tuple[Path, ArchiveStatus]:
    db_path = Path(config.storage.get("db_path", "data/foia_archive.db"))
    backend = _storage_backend_name(config)
    credentials = b2_credentials_present(env)
    bootstrap_expected = (
        (env.get("FOIA_BOOTSTRAP_DB_FROM_B2") or "").strip().lower() in TRUE_VALUES
    )
    result = (env.get("FOIA_ARCHIVE_BOOTSTRAP_RESULT") or "").strip().lower()
    result = result if result in BOOTSTRAP_RESULTS else None
    present = database_present(db_path)

    def status(mode, reason=None, placeholder=False):
        return ArchiveStatus(
            mode=mode,
            archive_connected=mode != "not_connected",
            storage_backend=backend,
            reason=reason,
            message=MESSAGES.get(reason) if reason else None,
            serving_placeholder_index=placeholder,
            bootstrap_result=result,
        )

    if backend != "b2":
        # Local development/writer layout: the DB is created and served here.
        return db_path, status("local")
    if not credentials:
        if present:
            # Metadata search still works; archived copies cannot be served.
            return db_path, status("not_connected", "b2_credentials_missing")
        return placeholder_path(db_path), status(
            "not_connected", "b2_credentials_missing", placeholder=True)
    if present or not bootstrap_expected:
        # Either a verified snapshot was restored, or a co-located single
        # writer (e.g. docker-compose crawler) owns and creates this DB.
        return db_path, status("connected")
    return placeholder_path(db_path), status(
        "not_connected", "snapshot_not_loaded", placeholder=True)
