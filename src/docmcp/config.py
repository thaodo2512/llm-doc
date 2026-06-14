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
VALID_EMBED_BACKENDS = {"local", "openai"}
VALID_POOLINGS = {"cls", "mean"}


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


def _validated_choice(value: str, valid: set[str], *, name: str) -> str:
    v = value.strip().lower()
    if v not in valid:
        raise ValueError(f"{name} must be one of {sorted(valid)}, got {value!r}")
    return v


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
    docstore_root: Path
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

    # Resource bounds (DoS guards for authenticated callers). Defaulted so direct
    # construction stays easy; Settings.load() always sets them from the environment.
    max_search_limit: int = 50  # clamp for search_docs / semantic_search `limit`
    max_read_bytes: int = 1_048_576  # most bytes read_doc will pull off disk in one call
    max_read_lines: int = 5000  # most lines read_doc will return in one call

    # Ingest parallelism. Default 1 = in-process sequential (deterministic; what the
    # test suite and any monkeypatching caller rely on). The real ingest wrapper
    # (docmcp.sh ingest) sets INGEST_WORKERS to a CPU-derived value so the expensive
    # Docling/tree-sitter parse fans out across worker processes. See ingest.pipeline.
    ingest_workers: int = 1

    # Embedding backend for vector/semantic search (only consulted when enable_vector).
    # "local" = offline ONNX model run in-process (no network — the default, honours an
    # air-gapped deployment); "openai" = legacy online API. The dim/pooling/prefixes MUST
    # match the chosen model (see models/bge-small-en-v1.5/README.md). Changing the backend
    # or dim requires a full re-ingest (the Qdrant collection is rebuilt at embedder.dim).
    embed_backend: str = "local"
    embed_model_dir: str = "/opt/models/embed"
    embed_dim: int = 384
    embed_pooling: str = "cls"  # "cls" (BGE) | "mean" (MiniLM / e5)
    embed_query_prefix: str = ""
    embed_passage_prefix: str = ""

    # Optional upload/manage portal (a WRITE surface; OFF by default). Defaulted for
    # easy direct construction; Settings.load() sets them from the environment.
    portal_enabled: bool = False
    session_secret: str = ""  # HMAC key for portal session cookies (required if enabled)
    staging_root: Path | None = None  # where the portal writes uploads; None => SOURCE_DIRS[0]
    max_upload_bytes: int = 26_214_400  # 25 MiB per file
    max_upload_files: int = 20  # files per upload request
    allow_plaintext_portal: bool = False  # opt-in: serve portal cookies over plain HTTP

    # Internal/derived files live at the DOCSTORE ROOT, *outside* DOC_ROOT, so they
    # are never reachable by read_doc/list/search (DocStore.resolve is contained to
    # DOC_ROOT). DOC_ROOT holds only readable curated docs. See PLAN Appendix A.5.
    @property
    def index_json(self) -> Path:
        return self.docstore_root / "index.json"

    @property
    def index_md(self) -> Path:
        return self.docstore_root / "index.md"

    @property
    def manifest_file(self) -> Path:
        return self.docstore_root / ".manifest.json"

    @property
    def ingest_status_file(self) -> Path:
        """Operator-facing run summary (last ingest result). Read by `doctor`."""
        return self.docstore_root / "ingest-status.json"

    @property
    def ingest_lock_file(self) -> Path:
        """Cross-process ingest lock (fcntl.flock target). See PLAN Appendix A.4."""
        return self.docstore_root / ".ingest.lock"

    @property
    def groups_file(self) -> Path:
        """Optional RBAC group definitions ({name: [prefixes]}); sibling of tokens.json."""
        return self.tokens_file.parent / "groups.json"

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

        # DOC_ROOT holds only readable curated docs; everything internal/derived
        # (index, manifest, status, lock, sqlite) lives at the DOCSTORE ROOT, which
        # defaults to DOC_ROOT's parent. Refuse a config where DOC_ROOT is not a
        # strict subdirectory, so internal files can never land in the served tree.
        doc_root = Path(env.get("DOC_ROOT", "/srv/docs/curated")).expanduser()
        docstore_root = Path(env.get("DOCSTORE_ROOT", str(doc_root.parent))).expanduser()
        _dr, _sr = doc_root.resolve(), docstore_root.resolve()
        if _dr == _sr or not _dr.is_relative_to(_sr):
            raise ValueError(
                "DOC_ROOT must be a strict subdirectory of DOCSTORE_ROOT so internal "
                "index/manifest/status/lock files stay outside the readable doc tree "
                f"(DOC_ROOT={doc_root}, DOCSTORE_ROOT={docstore_root})"
            )

        return cls(
            doc_root=doc_root,
            docstore_root=docstore_root,
            source_dirs=_split_csv(env.get("SOURCE_DIRS", "/srv/docs/raw")),
            bind_host=env.get("BIND_HOST", "127.0.0.1"),
            bind_port=_as_int(env.get("BIND_PORT", "8080"), name="BIND_PORT", minimum=1, maximum=65535),
            tokens_file=Path(env.get("TOKENS_FILE", "/srv/docs/tokens.json")).expanduser(),
            search_backend=backend,
            fts5_db=Path(env.get("FTS5_DB", str(docstore_root / "index.sqlite"))).expanduser(),
            enable_vector=_as_bool(env.get("ENABLE_VECTOR", "false")),
            qdrant_url=env.get("QDRANT_URL", "http://qdrant:6333"),
            openai_api_key=env.get("OPENAI_API_KEY", ""),
            openai_embed_model=env.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
            embed_chunk_tokens=_as_int(
                env.get("EMBED_CHUNK_TOKENS", "512"), name="EMBED_CHUNK_TOKENS", minimum=1
            ),
            allowed_origins=_split_csv(env.get("ALLOWED_ORIGINS", "")),
            allowed_hosts=_split_csv(env.get("ALLOWED_HOSTS", "localhost,127.0.0.1")),
            max_search_limit=_as_int(
                env.get("MAX_SEARCH_LIMIT", "50"), name="MAX_SEARCH_LIMIT", minimum=1
            ),
            max_read_bytes=_as_int(
                env.get("MAX_READ_BYTES", "1048576"), name="MAX_READ_BYTES", minimum=1024
            ),
            max_read_lines=_as_int(
                env.get("MAX_READ_LINES", "5000"), name="MAX_READ_LINES", minimum=1
            ),
            ingest_workers=_as_int(
                env.get("INGEST_WORKERS", "1"), name="INGEST_WORKERS", minimum=1, maximum=64
            ),
            embed_backend=_validated_choice(
                env.get("EMBED_BACKEND", "local"), VALID_EMBED_BACKENDS, name="EMBED_BACKEND"
            ),
            embed_model_dir=env.get("EMBED_MODEL_DIR", "/opt/models/embed"),
            embed_dim=_as_int(env.get("EMBED_DIM", "384"), name="EMBED_DIM", minimum=1),
            embed_pooling=_validated_choice(
                env.get("EMBED_POOLING", "cls"), VALID_POOLINGS, name="EMBED_POOLING"
            ),
            embed_query_prefix=env.get("EMBED_QUERY_PREFIX", ""),
            embed_passage_prefix=env.get("EMBED_PASSAGE_PREFIX", ""),
            portal_enabled=_as_bool(env.get("PORTAL_ENABLED", "false")),
            session_secret=env.get("SESSION_SECRET", ""),
            staging_root=(
                Path(env["STAGING_ROOT"]).expanduser() if env.get("STAGING_ROOT") else None
            ),
            max_upload_bytes=_as_int(
                env.get("MAX_UPLOAD_BYTES", "26214400"), name="MAX_UPLOAD_BYTES", minimum=1024
            ),
            max_upload_files=_as_int(
                env.get("MAX_UPLOAD_FILES", "20"), name="MAX_UPLOAD_FILES", minimum=1
            ),
            allow_plaintext_portal=_as_bool(env.get("ALLOW_PLAINTEXT_PORTAL", "false")),
        )

    @property
    def staging_dir(self) -> Path:
        """The raw-source root the portal writes uploads into (defaults to the first
        SOURCE_DIRS entry). Uploads are contained under here; ingest reads from here."""
        if self.staging_root is not None:
            return self.staging_root
        return Path(self.source_dirs[0]).expanduser() if self.source_dirs else Path("/srv/raw")

    def redacted(self) -> dict:
        """Dict form for printing/logging — never exposes the API key."""
        data = asdict(self)
        for key in ("doc_root", "docstore_root", "tokens_file", "fts5_db"):
            data[key] = str(getattr(self, key))
        data["openai_api_key"] = "***set***" if self.openai_api_key else ""
        data["session_secret"] = "***set***" if self.session_secret else ""
        data["staging_root"] = str(self.staging_root) if self.staging_root else None
        data["qdrant_url"] = _redact_url(self.qdrant_url)  # may carry credentials
        data["index_json"] = str(self.index_json)
        data["manifest_file"] = str(self.manifest_file)
        data["ingest_status_file"] = str(self.ingest_status_file)
        data["fts5_db"] = str(self.fts5_db)
        return data


def main() -> None:
    settings = Settings.load()
    print(json.dumps(settings.redacted(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
