"""Environment-driven settings — the single source of configuration truth.

Every value comes from an environment variable (optionally loaded from a local
`.env` file). See `.env.example` for the full list and defaults. Run

    python -m docmcp.config

to print the resolved settings (secrets redacted).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:  # python-dotenv is a base dependency; degrade gracefully if missing.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover

    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False


VALID_BACKENDS = {"ripgrep", "fts5"}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str, *, name: str, minimum: int, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {value!r}") from None
    if result < minimum or (maximum is not None and result > maximum):
        bound = f">= {minimum}" if maximum is None else f"in [{minimum}, {maximum}]"
        raise ValueError(f"{name} must be {bound}, got {result}")
    return result


def _redact_url(url: str) -> str:
    """Strip any user:password@ credentials from a URL for safe printing."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
    return url


@dataclass(frozen=True)
class Settings:
    """Resolved, validated configuration. Immutable once loaded."""

    doc_root: Path
    source_dirs: list[str]
    bind_host: str
    bind_port: int
    tokens_file: Path
    search_backend: str  # "ripgrep" | "fts5"
    fts5_db: Path
    enable_vector: bool
    qdrant_url: str
    openai_api_key: str
    openai_embed_model: str
    embed_chunk_tokens: int
    allowed_origins: list[str]
    allowed_hosts: list[str]

    # Index/manifest locations are derived from the doc root so they travel with it.
    @property
    def index_json(self) -> Path:
        return self.doc_root / "index.json"

    @property
    def index_md(self) -> Path:
        return self.doc_root / "index.md"

    @property
    def manifest_file(self) -> Path:
        return self.doc_root / ".manifest.json"

    @classmethod
    def load(cls, *, dotenv: bool = True) -> "Settings":
        if dotenv:
            load_dotenv()
        env = os.environ

        backend = env.get("SEARCH_BACKEND", "ripgrep").strip().lower()
        if backend not in VALID_BACKENDS:
            raise ValueError(
                f"SEARCH_BACKEND must be one of {sorted(VALID_BACKENDS)}, got {backend!r}"
            )

        return cls(
            doc_root=Path(env.get("DOC_ROOT", "/srv/docs/curated")).expanduser(),
            source_dirs=_split_csv(env.get("SOURCE_DIRS", "/srv/docs/raw")),
            bind_host=env.get("BIND_HOST", "127.0.0.1"),
            bind_port=_as_int(env.get("BIND_PORT", "8080"), name="BIND_PORT", minimum=1, maximum=65535),
            tokens_file=Path(env.get("TOKENS_FILE", "/srv/docs/tokens.json")).expanduser(),
            search_backend=backend,
            fts5_db=Path(env.get("FTS5_DB", "/srv/docs/index.sqlite")).expanduser(),
            enable_vector=_as_bool(env.get("ENABLE_VECTOR", "false")),
            qdrant_url=env.get("QDRANT_URL", "http://qdrant:6333"),
            openai_api_key=env.get("OPENAI_API_KEY", ""),
            openai_embed_model=env.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
            embed_chunk_tokens=_as_int(
                env.get("EMBED_CHUNK_TOKENS", "512"), name="EMBED_CHUNK_TOKENS", minimum=1
            ),
            allowed_origins=_split_csv(env.get("ALLOWED_ORIGINS", "")),
            allowed_hosts=_split_csv(env.get("ALLOWED_HOSTS", "localhost,127.0.0.1")),
        )

    def redacted(self) -> dict:
        """Dict form for printing/logging — never exposes the API key."""
        data = asdict(self)
        for key in ("doc_root", "tokens_file", "fts5_db"):
            data[key] = str(getattr(self, key))
        data["openai_api_key"] = "***set***" if self.openai_api_key else ""
        data["qdrant_url"] = _redact_url(self.qdrant_url)  # may carry credentials
        data["index_json"] = str(self.index_json)
        data["fts5_db"] = str(self.fts5_db)
        return data


def main() -> None:
    settings = Settings.load()
    print(json.dumps(settings.redacted(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
