"""Zero-budget storage policy: what original files to keep.

Always kept for every downloaded record: metadata, extracted text and the
SHA-256 of the original. Original files are kept only within a byte budget,
prioritised as the budget fills:

  fill <= 50%   keep everything that fits
  fill <= 85%   keep newly found / recent, at-risk, or small documents
  fill <= 100%  keep only (newly found / recent or at-risk) small documents
  over budget   keep none (text + SHA-256 + pointer to the source URL)

``FOIA_ORIGINALS_BUDGET_BYTES`` (or ``storage.policy.originals_budget_bytes``)
sets the budget. Remote S3-compatible storage defaults to its archive cap;
local storage defaults to unlimited; the ``none`` backend keeps no originals.
A per-run download budget bounds how much a single crawl fetches at all.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from urllib.parse import urlparse

DEFAULT_SMALL_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_RECENT_DAYS = 30


@dataclass(frozen=True)
class Decision:
    keep: bool
    reason: str


class SharedCounter:
    """Thread-safe counter shared by every per-thread config copy of a run."""

    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit))
        self.value = 0
        self._lock = threading.Lock()

    def __deepcopy__(self, memo):  # shared across copy.deepcopy(config.data)
        return self

    def reserve(self, amount: int) -> bool:
        with self._lock:
            if self.limit and self.value + max(0, amount) > self.limit:
                return False
            self.value += max(0, amount)
            return True

    def exhausted(self) -> bool:
        with self._lock:
            return bool(self.limit) and self.value >= self.limit


def _int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StoragePolicy:
    budget_bytes: int  # 0 = unlimited
    small_file_bytes: int = DEFAULT_SMALL_FILE_BYTES
    recent_days: int = DEFAULT_RECENT_DAYS
    at_risk_hosts: tuple = ()
    keep_originals: bool = True

    @classmethod
    def from_config(cls, config, backend_name: str, remote_cap: int = 0) -> "StoragePolicy":
        settings = (config.storage.get("policy") or {}) if config is not None else {}
        env_budget = (os.getenv("FOIA_ORIGINALS_BUDGET_BYTES") or "").strip()
        if env_budget:
            budget = _int(env_budget, 0)
        elif settings.get("originals_budget_bytes") is not None:
            budget = _int(settings.get("originals_budget_bytes"), 0)
        else:
            budget = remote_cap if backend_name != "local" else 0
        if remote_cap:
            budget = min(budget, remote_cap) if budget else remote_cap
        hosts = tuple(str(h).lower() for h in (settings.get("at_risk_hosts") or ()))
        return cls(
            budget_bytes=max(0, budget),
            small_file_bytes=_int(settings.get("small_file_bytes"), DEFAULT_SMALL_FILE_BYTES),
            recent_days=_int(settings.get("recent_days"), DEFAULT_RECENT_DAYS),
            at_risk_hosts=hosts,
            keep_originals=backend_name != "none",
        )

    def is_recent(self, published_date: Optional[str], newly_found_at: Optional[str],
                  today: Optional[date] = None) -> bool:
        if newly_found_at:
            return True
        if not published_date:
            return False
        today = today or datetime.now(timezone.utc).date()
        try:
            published = date.fromisoformat(str(published_date)[:10])
        except ValueError:
            return False
        return published >= today - timedelta(days=self.recent_days)

    def is_at_risk(self, url: str, source_last_error: Optional[str] = None) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in self.at_risk_hosts):
            return True
        # A source that failed recently may be moving or removing records.
        return bool(source_last_error)

    def decide(self, size: int, used_bytes: int, *, recent: bool, at_risk: bool) -> Decision:
        if not self.keep_originals:
            return Decision(False, "no original-file storage configured (text and SHA-256 kept)")
        if not self.budget_bytes:
            return Decision(True, "unlimited")
        size = max(0, int(size or 0))
        after = used_bytes + size
        if after > self.budget_bytes:
            return Decision(False, "original-file budget reached (text and SHA-256 kept)")
        fill = after / self.budget_bytes
        small = size <= self.small_file_bytes
        priority = recent or at_risk
        if fill <= 0.5:
            return Decision(True, "within budget")
        if fill <= 0.85:
            if priority or small:
                return Decision(True, "prioritised (recent, at-risk or small)")
            return Decision(False, "budget over 50%: large, older original not kept")
        if priority and small:
            return Decision(True, "prioritised (recent or at-risk, small)")
        return Decision(False, "budget over 85%: only small recent/at-risk originals kept")


def run_download_counter(config) -> SharedCounter:
    """Per-run cap on bytes downloaded at all (FOIA_RUN_DOWNLOAD_BUDGET_BYTES)."""
    counter = config.data.get("_run_download_counter")
    if counter is None:
        raw = (os.getenv("FOIA_RUN_DOWNLOAD_BUDGET_BYTES") or "").strip()
        limit = _int(raw, 0) if raw else _int((config.storage.get("policy") or {}).get(
            "run_download_budget_bytes"), 0)
        counter = SharedCounter(limit)
        config.data["_run_download_counter"] = counter
    return counter


def hosts_tuple(values: Iterable[str]) -> tuple:
    return tuple(str(v).lower() for v in values)
