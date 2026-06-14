"""The console's subprocess command allowlist — the security perimeter.

A host process that can run ``docker compose`` and edit ``tokens.json`` is
root-equivalent, so this module is the entire offensive surface and is written
defensively:

* **Fixed verb set.** ``ACTIONS`` maps a logical action → an argv builder. There is
  no pass-through path; an unknown action raises before anything is built.
* **argv arrays, never shell strings.** Builders return a ``list[str]``; ``runner.py``
  spawns it with ``shell=False``. A shell metacharacter in a user value (``;`` ``|``
  ``$(...)`` newline) is therefore an inert literal argv element.
* **Per-argument validation, allowlist-style.** Each value must match a strict pattern
  or it is rejected. In particular, values can never begin with ``-`` where ``docmcp.sh``
  could misread them as a flag (e.g. a user named ``--all``).

This module does no I/O and spawns nothing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path


class ValidationError(ValueError):
    """A request argument failed validation (callers map this to HTTP 400)."""


# --- executable resolution (never from request input) -----------------------
def repo_root() -> Path:
    """The bind-mounted repo root. Set by ``cmd_console`` via ``DOCMCP_REPO_ROOT``;
    falls back to the process cwd (which ``docker run -w`` sets to the same path)."""
    return Path(os.environ.get("DOCMCP_REPO_ROOT") or os.getcwd())


def docmcp_sh() -> str:
    return str(repo_root() / "docmcp.sh")


def deploy_script(profile: str) -> str:
    name = "local_deploy.sh" if profile == "local" else "remote_deploy.sh"
    return str(repo_root() / name)


# --- validators -------------------------------------------------------------
_USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")  # no leading dash
_GROUP_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PREFIX_SEG_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
_EXPIRES_RE = re.compile(r"^(?:[0-9]{1,5}[dhm]|never|none)$")
_TOKENREF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,127}$")  # tok_… string OR user
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_BUILD_TARGETS = {"server", "ingest", "all"}
PROFILES = {"local", "vpn", "https"}


def valid_user(name: str) -> str:
    if not isinstance(name, str) or not _USER_RE.match(name):
        raise ValidationError(
            f"invalid user name {name!r}: letters/digits/._- only, no leading dash, ≤64 chars"
        )
    return name


def valid_group_name(name: str) -> str:
    if not isinstance(name, str) or not _GROUP_RE.match(name):
        raise ValidationError(f"invalid group name {name!r}: letters/digits/_- only, ≤64 chars")
    return name


def valid_prefix(prefix: str, *, allow_root: bool = False) -> str:
    if not isinstance(prefix, str):
        raise ValidationError("prefix must be a string")
    p = prefix.strip()
    if not p.startswith("/"):
        raise ValidationError(f"prefix must start with '/': {prefix!r}")
    if len(p) > 256:
        raise ValidationError("prefix too long (max 256)")
    segs = [s for s in p.split("/") if s]
    if not segs:
        if allow_root:
            return "/"
        raise ValidationError("a bare '/' grants the whole corpus — use the admin option instead")
    for s in segs:
        if s in (".", ".."):
            raise ValidationError("'.' and '..' are not allowed in a prefix")
        if not _PREFIX_SEG_RE.match(s):
            raise ValidationError(f"invalid characters in prefix segment {s!r}")
    return "/" + "/".join(segs)


def valid_expires(spec: str) -> str:
    if not isinstance(spec, str) or not _EXPIRES_RE.match(spec.strip()):
        raise ValidationError(f"invalid expiry {spec!r} (use Nd | Nh | Nm | never)")
    return spec.strip()


def valid_token_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _TOKENREF_RE.match(ref.strip()):
        raise ValidationError(f"invalid token/user reference {ref!r}")
    return ref.strip()


def valid_port(port) -> str:
    try:
        n = int(str(port).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"invalid port {port!r}") from None
    if not (1 <= n <= 65535):
        raise ValidationError(f"port out of range (1-65535): {n}")
    return str(n)


def valid_ip(addr: str) -> str:
    if not isinstance(addr, str):
        raise ValidationError("ip must be a string")
    a = addr.strip()
    octets = a.split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        raise ValidationError(f"invalid IPv4 address {addr!r}")
    return a


def valid_domain(host: str) -> str:
    if not isinstance(host, str) or not _HOSTNAME_RE.match(host.strip()):
        raise ValidationError(f"invalid hostname {host!r}")
    return host.strip()


def valid_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValidationError(f"invalid profile {profile!r} (one of {sorted(PROFILES)})")
    return profile


def valid_build_target(target: str) -> str:
    if target not in _BUILD_TARGETS:
        raise ValidationError(f"invalid build target {target!r} (one of {sorted(_BUILD_TARGETS)})")
    return target


def valid_import_dir(path: str) -> str:
    """A filesystem path to stage + ingest during setup. This is NOT request input — it is set by
    ``cmd_console`` (from the operator's ``--docs`` / menu prompt) and read from the
    ``CONSOLE_IMPORT_DIR`` env var. We still validate it: absolute, no control chars / leading
    dash (so the deploy script can't misread it as a flag), and present on disk (cmd_console mounts
    it into this container)."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValidationError("import dir must be an absolute path")
    if any(c in path for c in "\x00\n\r"):
        raise ValidationError("import dir contains control characters")
    if not Path(path).exists():
        raise ValidationError(f"import dir not found: {path}")
    return path


def valid_schedule(spec: str) -> str:
    if not isinstance(spec, str):
        raise ValidationError("schedule must be a string")
    s = spec.strip()
    if s in ("off", "remove", "hourly", "daily", "weekly"):
        return s
    m = re.match(r"^([0-9]{1,2})([mh])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if (unit == "m" and 1 <= n <= 59) or (unit == "h" and 1 <= n <= 23):
            return s
        raise ValidationError(f"invalid interval {spec!r} (1-59m or 1-23h)")
    fields = s.split()
    if len(fields) == 5 and all(re.match(r"^[0-9*/,\-]+$", f) for f in fields):
        return s
    raise ValidationError(f"invalid schedule {spec!r} (off|hourly|daily|weekly|Nm|Nh|5-field cron)")


def clean_text(value: str, *, field: str = "value", maxlen: int = 200) -> str:
    """A free-text value (e.g. a comment) — safe as a single argv element, but reject
    control chars / newlines and bound the length so it cannot bloat logs or argv."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if len(value) > maxlen:
        raise ValidationError(f"{field} too long (max {maxlen})")
    if any(ch < " " or ch == "\x7f" for ch in value):
        raise ValidationError(f"{field} contains control characters")
    return value


# --- editable .env keys (config editor) -------------------------------------
def _bool_val(v: str) -> str:
    s = str(v).strip().lower()
    if s in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        return "true" if s in ("true", "1", "yes", "on") else "false"
    raise ValidationError(f"expected a boolean, got {v!r}")


def _int_val(v: str) -> str:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"expected an integer, got {v!r}") from None
    if n < 0:
        raise ValidationError("must be ≥ 0")
    return str(n)


def _backend_val(v: str) -> str:
    if str(v).strip() not in ("ripgrep", "fts5"):
        raise ValidationError("SEARCH_BACKEND must be 'ripgrep' or 'fts5'")
    return str(v).strip()


def _secret_val(v: str) -> str:
    return clean_text(v, field="value", maxlen=512)


def _model_val(v: str) -> str:
    s = clean_text(v, field="value", maxlen=128).strip()
    if not re.match(r"^[A-Za-z0-9._-]+$", s):
        raise ValidationError("invalid model name")
    return s


# key -> per-value validator. SESSION_SECRET / HTTP_BIND / DOMAIN / ALLOW_PLAINTEXT_HTTP
# are DELIBERATELY absent: secrets are regenerate-only and network posture is set by the
# wizard, never via an ad-hoc key edit (which could quietly expose plaintext off loopback).
EDITABLE_KEYS = {
    "ENABLE_VECTOR": _bool_val,
    "PORTAL_ENABLED": _bool_val,
    "LOG_REQUESTS": _bool_val,
    "ALLOW_PLAINTEXT_PORTAL": _bool_val,
    "TOKEN_TTL": valid_expires,
    "HTTP_PORT": valid_port,
    "MAX_UPLOAD_BYTES": _int_val,
    "MAX_UPLOAD_FILES": _int_val,
    "SEARCH_BACKEND": _backend_val,
    "OPENAI_API_KEY": _secret_val,
    "OPENAI_EMBED_MODEL": _model_val,
}


def valid_env_key(key: str) -> str:
    if key not in EDITABLE_KEYS:
        raise ValidationError(f"{key!r} is not an editable setting")
    return key


def valid_env_value(key: str, value) -> str:
    return EDITABLE_KEYS[valid_env_key(key)](value)


# --- argv builders ----------------------------------------------------------
def _seq(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    if isinstance(values, Iterable):
        return list(values)
    raise ValidationError("expected a list of strings")


def _token_mint(
    *,
    user: str,
    prefixes=None,
    groups=None,
    writes=None,
    expires: str | None = None,
    comment: str | None = None,
    admin: bool = False,
) -> list[str]:
    argv = [docmcp_sh(), "token"]
    if admin:
        argv += [valid_user(user), "--all"]
        if expires:
            argv += ["--expires", valid_expires(expires)]
        if comment:
            argv += ["--comment", clean_text(comment, field="comment")]
        return argv
    prefixes, groups, writes = _seq(prefixes), _seq(groups), _seq(writes)
    if not prefixes and not groups and not writes:
        raise ValidationError("a scope is required: at least one read prefix, group, or write prefix")
    argv.append(valid_user(user))
    for p in prefixes:
        argv.append(valid_prefix(p))
    for g in groups:
        argv += ["--group", valid_group_name(g)]
    for w in writes:
        argv += ["--write", valid_prefix(w, allow_root=True)]
    if expires:
        argv += ["--expires", valid_expires(expires)]
    if comment:
        argv += ["--comment", clean_text(comment, field="comment")]
    return argv


def _token_revoke(*, ref: str) -> list[str]:
    return [docmcp_sh(), "token-rm", valid_token_ref(ref)]


def _token_rotate(*, user: str) -> list[str]:
    return [docmcp_sh(), "token-rotate", valid_user(user)]


def _group_define(*, name: str, prefixes) -> list[str]:
    prefixes = _seq(prefixes)
    if not prefixes:
        raise ValidationError("a group needs at least one prefix")
    argv = [docmcp_sh(), "group", valid_group_name(name)]
    for p in prefixes:
        argv.append(valid_prefix(p))  # bare '/' rejected (cmd_group also rejects it)
    return argv


def _group_remove(*, name: str) -> list[str]:
    return [docmcp_sh(), "group-rm", valid_group_name(name)]


def _ingest(*, full: bool = False) -> list[str]:
    argv = [docmcp_sh(), "ingest"]
    if full:
        argv.append("--full")
    return argv


def _build(*, target: str = "server") -> list[str]:
    return [docmcp_sh(), "build", valid_build_target(target)]


def _serve() -> list[str]:
    return [docmcp_sh(), "serve"]


def _stop() -> list[str]:
    return [docmcp_sh(), "stop"]


def _backup() -> list[str]:
    return [docmcp_sh(), "backup"]  # default ./backups; no caller-chosen path


def _schedule_set(*, spec: str) -> list[str]:
    return [docmcp_sh(), "schedule", valid_schedule(spec)]


def _schedule_show() -> list[str]:
    return [docmcp_sh(), "schedule"]


def _status() -> list[str]:
    return [docmcp_sh(), "status"]


def _doctor() -> list[str]:
    return [docmcp_sh(), "doctor"]


def _inventory() -> list[str]:
    return [docmcp_sh(), "inventory"]


def _env_set(*, key: str, value) -> list[str]:
    return [docmcp_sh(), "env-set", valid_env_key(key), valid_env_value(key, value)]


def _wizard(
    *,
    profile: str,
    port=None,
    bind=None,
    ip=None,
    domain=None,
    portal: bool = False,
    vector_local: bool = False,
    schedule: str | None = None,
    docs: str | None = None,
) -> list[str]:
    """Drive the non-interactive deploy wizard (``--yes``).

    Vector search: ``vector_local`` emits ``--vector-local`` for the OFFLINE local embedder
    (no API key, no network). The legacy OpenAI backend is enabled instead when the route
    passes a key via the ``DOCMCP_OPENAI_API_KEY`` env var (NOT argv, so it never appears in
    the process list) — so this builder emits no ``--vector-key`` and no key value at all.

    ``docs`` (the operator's ``CONSOLE_IMPORT_DIR``, if any) is passed as ``--docs`` so the
    deploy stages + ingests that folder in the same run — the first-run setup indexes the
    corpus instead of leaving an empty store the user must ingest separately."""
    valid_profile(profile)
    argv = [deploy_script(profile), "--yes"]
    if profile == "local":
        if port is not None:
            argv += ["--port", valid_port(port)]
    elif profile == "vpn":
        if not ip:
            raise ValidationError("the vpn profile requires an ip")
        argv += ["--ip", valid_ip(ip)]
        if bind is not None:
            argv += ["--bind", valid_ip(bind)]
        if port is not None:
            argv += ["--port", valid_port(port)]
    elif profile == "https":
        if not domain:
            raise ValidationError("the https profile requires a domain")
        argv += ["--domain", valid_domain(domain)]
    if portal:
        argv.append("--portal")
    if vector_local:
        argv.append("--vector-local")
    if docs:
        argv += ["--docs", valid_import_dir(docs)]
    if schedule:
        argv += ["--schedule", valid_schedule(schedule)]
    return argv


# The frozen action registry. NOTHING else may construct a subprocess argv.
ACTIONS = {
    "status": _status,
    "doctor": _doctor,
    "inventory": _inventory,
    "schedule.show": _schedule_show,
    "schedule.set": _schedule_set,
    "token.mint": _token_mint,
    "token.revoke": _token_revoke,
    "token.rotate": _token_rotate,
    "group.define": _group_define,
    "group.remove": _group_remove,
    "ingest": _ingest,
    "build": _build,
    "serve": _serve,
    "stop": _stop,
    "backup": _backup,
    "env.set": _env_set,
    "wizard": _wizard,
}


def build(action: str, **kwargs) -> list[str]:
    """Validate ``kwargs`` and return the argv list for ``action``. Raises
    ``ValidationError`` for an unknown action or any bad argument — before any spawn."""
    builder = ACTIONS.get(action)
    if builder is None:
        raise ValidationError(f"unknown action: {action!r}")
    return builder(**kwargs)
