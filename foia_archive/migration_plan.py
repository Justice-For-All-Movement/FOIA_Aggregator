"""FC-11: contingent paid-storage / search migration gate (no spend here).

Nothing in this module authorizes cost. ``storage_cost_estimate`` requires the
caller to supply current published prices (none are hard-coded, because they
change); ``migration_gate`` stays closed until an explicit owner authorization
record with a monthly budget covers the estimate; ``shadow_compare`` checks
query parity between the current index and a candidate before any cutover.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

TB = 1_000_000_000_000


def storage_cost_estimate(*, stored_bytes: int, price_per_tb_month: float, egress_bytes_month: int = 0,
                          egress_price_per_tb: float = 0.0, free_bytes: int = 0) -> Dict[str, Any]:
    for value in (stored_bytes, egress_bytes_month, free_bytes):
        if type(value) is not int or value < 0:
            raise ValueError("Byte quantities must be non-negative integers")
    if price_per_tb_month < 0 or egress_price_per_tb < 0:
        raise ValueError("Prices must be non-negative")
    billable = max(0, stored_bytes - free_bytes)
    monthly = billable / TB * price_per_tb_month + egress_bytes_month / TB * egress_price_per_tb
    return {"billable_bytes": billable, "monthly_usd": round(monthly, 2),
            "inputs_are_caller_supplied": True, "spend_authorized": False}


def migration_gate(estimate: Dict[str, Any], authorization: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Open only with {approved_by, approved_at (ISO), monthly_budget_usd >= estimate}."""
    reasons = []
    if not authorization:
        reasons.append("no_owner_authorization")
    else:
        if not str(authorization.get("approved_by") or "").strip():
            reasons.append("approver_missing")
        try:
            datetime.fromisoformat(str(authorization.get("approved_at")).replace("Z", "+00:00"))
        except ValueError:
            reasons.append("approval_time_invalid")
        budget = authorization.get("monthly_budget_usd")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget < estimate["monthly_usd"]:
            reasons.append("budget_below_estimate")
    return {"open": not reasons, "reasons": reasons, "estimate_usd": estimate["monthly_usd"]}


def shadow_compare(queries: Iterable[str], primary, candidate, *, top_k: int = 20) -> Dict[str, Any]:
    """Run each query against both search functions; report set and rank parity."""
    rows: List[Dict[str, Any]] = []
    for query in queries:
        a: Sequence[Any] = list(primary(query))[:top_k]
        b: Sequence[Any] = list(candidate(query))[:top_k]
        rows.append({"query": query, "primary": len(a), "candidate": len(b),
                     "missing_in_candidate": sorted(set(a) - set(b), key=str),
                     "extra_in_candidate": sorted(set(b) - set(a), key=str),
                     "same_order": list(a) == list(b)})
    parity = all(not r["missing_in_candidate"] for r in rows)
    return {"queries": len(rows), "result_parity": parity,
            "exact_order_parity": parity and all(r["same_order"] for r in rows),
            "details": rows, "cutover_ready": False}
