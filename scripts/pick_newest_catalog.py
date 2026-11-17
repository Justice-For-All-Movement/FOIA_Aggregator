#!/usr/bin/env python3
"""Pick the freshest restored catalog: object-storage checkpoint vs GitHub snapshot.

Usage: pick_newest_catalog.py DEST CANDIDATE [CANDIDATE ...]

Freshness = newest ingestion-run timestamp, then document count. The chosen
file is moved to DEST; the others are left in place for inspection. Prevents a
stale checkpoint (e.g. when checkpoint uploads fail) from silently replacing
newer published work. Exit 4 when no candidate is a readable catalog.
"""
import shutil
import sqlite3
import sys
from pathlib import Path


def freshness(path: Path):
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            run = conn.execute(
                "SELECT MAX(COALESCE(finished_at, updated_at, started_at)) FROM ingestion_runs"
            ).fetchone()[0] or ""
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    return (run, docs)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    dest = Path(argv[1])
    scored = []
    for name in argv[2:]:
        path = Path(name)
        if path.is_file() and path.stat().st_size:
            score = freshness(path)
            print(f"{name}: latest run {score[0] if score else '?'}, documents {score[1] if score else '?'}")
            if score is not None:
                scored.append((score, path))
    if not scored:
        return 4
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][1]
    if best.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(best), str(dest))
    print(f"Using {best} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
