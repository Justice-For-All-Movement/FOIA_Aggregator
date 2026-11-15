"""Identify the code that produced an export (manifests name the producer commit).

Sources, in order: ``FOIA_BUILD_COMMIT`` (baked in at image build time),
``RENDER_GIT_COMMIT`` (set by Render at runtime), ``GITHUB_SHA`` (Actions),
then ``git rev-parse HEAD`` for local CLI runs. Only a full 40-hex SHA is
reported; otherwise ``commit`` is ``null`` and consumers must treat the export
as unpinned.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Mapping, Optional

PRODUCER_NAME = "FOIA_Aggregator"
REPOSITORY = "Justice-For-All-Movement/FOIA_Aggregator"
ENV_SOURCES = ("FOIA_BUILD_COMMIT", "RENDER_GIT_COMMIT", "GITHUB_SHA")
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_head() -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    return value if completed.returncode == 0 and _SHA.fullmatch(value) else None


def producer_info(env: Mapping[str, str] = os.environ, *, use_git: bool = True) -> Dict[str, Optional[str]]:
    for name in ENV_SOURCES:
        value = (env.get(name) or "").strip().lower()
        if _SHA.fullmatch(value):
            return {"name": PRODUCER_NAME, "repository": REPOSITORY, "commit": value, "commit_source": name}
    head = _git_head() if use_git else None
    return {"name": PRODUCER_NAME, "repository": REPOSITORY, "commit": head,
            "commit_source": "git" if head else None}
