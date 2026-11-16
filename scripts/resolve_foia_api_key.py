"""Print FOIA.gov's *published* testing API key (from its public swagger.json).

FOIA.gov documents a shared testing key inside the public API description at
github.com/usdoj/foia.gov. Workflows use it only when no FOIA_API_KEY secret
is configured. Usage: ``python scripts/resolve_foia_api_key.py`` prints the
key (CI masks it before writing it to $GITHUB_ENV).
"""
from __future__ import annotations

import re
import sys

import requests
from bs4 import BeautifulSoup


def published_testing_key(timeout: int = 30) -> str:
    repo = requests.get("https://api.github.com/repos/usdoj/foia.gov", timeout=timeout)
    repo.raise_for_status()
    branch = repo.json()["default_branch"]
    spec = requests.get(
        f"https://raw.githubusercontent.com/usdoj/foia.gov/{branch}/swagger.json", timeout=timeout)
    spec.raise_for_status()
    text = BeautifulSoup(spec.json()["info"]["description"], "html.parser").get_text(" ", strip=True)
    match = re.search(
        r"use\s+the\s+following\s+api[_ -]?key\s+for\s+testing\.?\s*([A-Za-z0-9_-]{20,})",
        text, flags=re.IGNORECASE)
    if not match:
        raise SystemExit("Could not recover FOIA.gov published testing API key")
    return match.group(1)


if __name__ == "__main__":
    sys.stdout.write(published_testing_key() + "\n")
