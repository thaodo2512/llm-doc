"""Structured access logging — one JSON line per tool call, secret-free.

Emits the authenticated **user id**, the tool, the caller's prefix **count** (not
the prefixes themselves), the requested path/query, the result size, a denied/
truncated flag, and the duration. It deliberately never logs the bearer **token**,
request headers, raw document content, or API keys.

Lines go to stderr (captured by `docker logs`) as compact JSON so they can be
shipped/parsed downstream. Toggle with ``LOG_REQUESTS=false``.
"""

from __future__ import annotations

import json
import os
import sys
import time

_TRUTHY_OFF = {"0", "false", "no", "off"}
_ENABLED = os.environ.get("LOG_REQUESTS", "true").strip().lower() not in _TRUTHY_OFF

# Keys that must never appear in a log line, as a defensive backstop even if a
# caller passes them by mistake.
_FORBIDDEN = {"token", "authorization", "auth", "headers", "content", "api_key", "openai_api_key"}


def log_call(**fields: object) -> None:
    """Emit one structured access-log line (no-op when LOG_REQUESTS is off)."""
    if not _ENABLED:
        return
    for key in _FORBIDDEN:
        fields.pop(key, None)
    fields.setdefault("ts", int(time.time()))
    try:
        line = json.dumps(fields, separators=(",", ":"), sort_keys=True, default=str)
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:  # logging must never break a request
        pass
