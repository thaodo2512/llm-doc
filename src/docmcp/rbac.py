"""Path-prefix RBAC — the single place prefix membership is decided.

Logical doc paths are POSIX, rooted at DOC_ROOT, and start with "/"
(e.g. "/public/foo.md"). An `allowed_prefixes` entry of "/" (or "") grants the
whole tree. Prefix matching is segment-aware: "/pub" does NOT match "/public".
"""

from __future__ import annotations

from collections.abc import Iterable


def _norm(path: str) -> str:
    """Normalize to a leading-slash, no-trailing-slash logical path."""
    return "/" + path.strip().strip("/")


def is_allowed(path: str, allowed_prefixes: Iterable[str]) -> bool:
    """True iff `path` lies under any of `allowed_prefixes` (segment-aware)."""
    p = _norm(path)
    for prefix in allowed_prefixes:
        if prefix.strip() in ("", "/"):
            return True
        pref = _norm(prefix)
        if p == pref or p.startswith(pref + "/"):
            return True
    return False


def filter_allowed(items, allowed_prefixes: Iterable[str], *, key=lambda x: x):
    """Filter an iterable to the entries whose `key(item)` path is allowed."""
    allowed = list(allowed_prefixes)
    return [item for item in items if is_allowed(key(item), allowed)]
