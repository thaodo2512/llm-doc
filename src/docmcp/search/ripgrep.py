"""Keyword search via ripgrep (primary v1 backend).

Runs `rg --json` as a subprocess, scopes the search to the caller's allowed
prefixes with include globs, and post-filters every hit through the RBAC check
(defense in depth). Uses fixed-string (`-F`) smart-case matching — ideal for
exact code symbols and config keys, per the brief.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import rbac
from ..config import Settings
from ..types import Hit
from .base import MAX_SNIPPET, SearchBackend

# Never surface the index/manifest/db as search results.
_EXCLUDES = ["!/index.json", "!/index.md", "!/.manifest.json", "!*.sqlite*"]


def _include_globs(allowed_prefixes: list[str]) -> list[str]:
    """Anchored include globs from allowed prefixes. Empty => unrestricted ('/')."""
    globs: list[str] = []
    for prefix in allowed_prefixes:
        norm = prefix.strip().strip("/")
        if norm == "":
            return []  # "/" grants the whole tree
        globs.append(f"/{norm}/**")
        globs.append(f"/{norm}")
    return globs


class RipgrepBackend(SearchBackend):
    def __init__(self, settings: Settings, rg_binary: str = "rg"):
        self.root = Path(settings.doc_root).expanduser().resolve()
        self.rg = rg_binary

    def search(self, query: str, allowed_prefixes: list[str], limit: int = 10) -> list[Hit]:
        query = (query or "").strip()
        if not query or not allowed_prefixes or not self.root.is_dir():
            return []

        cmd = [self.rg, "--json", "-S", "-F", "--max-count", str(max(limit, 1))]
        for glob in _include_globs(allowed_prefixes):
            cmd += ["-g", glob]
        for glob in _EXCLUDES:
            cmd += ["-g", glob]
        cmd += ["--", query, str(self.root)]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode not in (0, 1):  # 1 = no matches (not an error)
            raise RuntimeError(f"ripgrep failed ({proc.returncode}): {proc.stderr.strip()}")

        hits: list[Hit] = []
        for line in proc.stdout.splitlines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            try:
                logical = "/" + Path(data["path"]["text"]).resolve().relative_to(self.root).as_posix()
            except (KeyError, ValueError):
                continue
            if not rbac.is_allowed(logical, allowed_prefixes):
                continue  # defense in depth beyond the include globs
            text = (data.get("lines", {}).get("text") or "").rstrip("\n")
            hits.append(
                Hit(
                    path=logical,
                    line=int(data["line_number"]),
                    snippet=text[:MAX_SNIPPET],
                    score=float(len(data.get("submatches", []))) or 1.0,
                )
            )
            if len(hits) >= limit:
                break
        return hits
