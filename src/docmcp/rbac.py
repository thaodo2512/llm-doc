"""Path-prefix RBAC — the single place prefix membership is decided.

Logical doc paths are POSIX, rooted at DOC_ROOT, and start with "/"
(e.g. "/public/foo.md"). An `allowed_prefixes` entry of "/" (or "") grants the
whole tree. Prefix matching is segment-aware: "/pub" does NOT match "/public".
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable


def _norm(path: str) -> str:
    """Normalize to a leading-slash logical path, collapsing `.`/`..` segments.

    Collapsing `..` here keeps prefix membership honest: "/public/../secret" must
    not be treated as living under "/public".
    """
    return posixpath.normpath("/" + path.strip().strip("/"))


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
