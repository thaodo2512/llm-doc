"""Append-only audit of console actions → ``var/console-audit.jsonl``.

Records who did what (user, action, redacted argv, job id, result). NEVER logs a pasted
bearer token, a freshly minted token, or a secret env value — callers pass already-safe
fields. Best-effort: an audit write failure never blocks the action.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# argv elements that immediately follow these flags are secret-ish and get masked.
_SENSITIVE_FLAGS = {"--comment"}


def redact_argv(argv: list[str]) -> list[str]:
    """A log-safe view of an argv: drop the absolute script path to its basename and
    mask values that follow a sensitive flag."""
    out: list[str] = []
    mask_next = False
    for i, arg in enumerate(argv):
        if mask_next:
            out.append("***")
            mask_next = False
            continue
        out.append(Path(arg).name if i == 0 else arg)
        if arg in _SENSITIVE_FLAGS:
            mask_next = True
    return out


class ConsoleAudit:
    def __init__(self, repo_root: Path):
        self._path = repo_root / "var" / "console-audit.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def record(self, *, user: str, action: str, result: str, **fields) -> None:
        entry = {"ts": int(time.time()), "user": user, "action": action, "result": result, **fields}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass
