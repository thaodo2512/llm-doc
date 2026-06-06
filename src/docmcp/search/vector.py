"""Optional vector search: OpenAI embeddings + Qdrant (OFF by default).

Imported ONLY when ENABLE_VECTOR=true (guarded in tools.py / pipeline.py), so
when the flag is false neither Qdrant nor OpenAI is ever contacted. The embedder
is injectable so the machinery can be exercised without calling OpenAI.

RBAC: each point stores `ancestors` (every segment-boundary prefix of its doc
path); the Qdrant filter matches any allowed prefix against it, and a Python
`is_allowed` post-filter is applied as a safety net (Qdrant text matching is not
an anchored prefix match).
"""

from __future__ import annotations

from typing import Protocol

from .. import rbac
from ..config import Settings
from ..types import Hit, IndexEntry
from .base import MAX_SNIPPET, SearchBackend

COLLECTION = "docmcp_chunks"


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OpenAIEmbedder:
    """Embeds via the OpenAI API (`text-embedding-3-small` -> 1536 dims)."""

    def __init__(self, settings: Settings):
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.openai_api_key or None)
        self.model = settings.openai_embed_model
        self.dim = 1536

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # OpenAI allows up to 2048 inputs per request; batch to be safe.
        out: list[list[float]] = []
        for i in range(0, len(texts), 512):
            resp = self._client.embeddings.create(model=self.model, input=texts[i : i + 512])
            out.extend(item.embedding for item in resp.data)
        return out


def _qdrant(settings: Settings):
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.qdrant_url)


def _ancestors(path: str) -> list[str]:
    """All segment-boundary prefixes that grant access to `path`.

    "/a/b/c.md" -> ["/a", "/a/b", "/a/b/c.md"].
    """
    out, current = [], ""
    for segment in [p for p in path.strip("/").split("/") if p]:
        current += "/" + segment
        out.append(current)
    return out


def _chunk_lines(text: str, chunk_tokens: int) -> list[tuple[int, str]]:
    """Line-aware chunking: pack ~chunk_tokens*4 chars, tracking start line."""
    budget = max(256, chunk_tokens * 4)
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    length = 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        if buf and length + len(line) + 1 > budget:
            chunks.append((start, "\n".join(buf)))
            buf, length = [], 0
        if not buf:
            start = line_no
        buf.append(line)
        length += len(line) + 1
    if buf:
        chunks.append((start, "\n".join(buf)))
    return [(s, t) for s, t in chunks if t.strip()]


def embed_and_upsert(
    settings: Settings,
    entries: list[IndexEntry],
    embedder: Embedder | None = None,
    collection: str = COLLECTION,
) -> int:
    """(Re)build the Qdrant collection from curated docs. Returns #points."""
    from qdrant_client.models import (
        Distance,
        PayloadSchemaType,
        PointStruct,
        VectorParams,
    )

    embedder = embedder or OpenAIEmbedder(settings)
    client = _qdrant(settings)

    records: list[tuple[str, int, int, str]] = []  # path, chunk_id, start_line, text
    for entry in entries:
        fs = settings.doc_root / entry.path.lstrip("/")
        if not fs.is_file():
            continue
        text = fs.read_text(encoding="utf-8", errors="replace")
        for chunk_id, (start_line, chunk) in enumerate(_chunk_lines(text, settings.embed_chunk_tokens)):
            records.append((entry.path, chunk_id, start_line, chunk))

    vectors: list[list[float]] = []
    for i in range(0, len(records), 256):
        vectors.extend(embedder.embed([r[3] for r in records[i : i + 256]]))

    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection, vectors_config=VectorParams(size=embedder.dim, distance=Distance.COSINE)
    )
    client.create_payload_index(
        collection, field_name="ancestors", field_schema=PayloadSchemaType.KEYWORD
    )

    points = [
        PointStruct(
            id=idx,
            vector=vectors[idx],
            payload={
                "path": path,
                "chunk_id": chunk_id,
                "line": start_line,
                "snippet": chunk[:MAX_SNIPPET],
                "ancestors": _ancestors(path),
            },
        )
        for idx, (path, chunk_id, start_line, chunk) in enumerate(records)
    ]
    if points:
        client.upsert(collection, points)
    return len(points)


class VectorSearch(SearchBackend):
    def __init__(
        self, settings: Settings, embedder: Embedder | None = None, collection: str = COLLECTION
    ):
        self.settings = settings
        self.collection = collection
        self.embedder = embedder or OpenAIEmbedder(settings)
        self.client = _qdrant(settings)

    def search(self, query: str, allowed_prefixes: list[str], limit: int = 10) -> list[Hit]:
        query = (query or "").strip()
        if not query or not allowed_prefixes:
            return []
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        vector = self.embedder.embed([query])[0]

        normalized = ["/" + p.strip().strip("/") for p in allowed_prefixes]
        unrestricted = "/" in normalized  # "/" => whole tree
        query_filter = None
        if not unrestricted:
            query_filter = Filter(
                must=[FieldCondition(key="ancestors", match=MatchAny(any=normalized))]
            )

        response = self.client.query_points(
            self.collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        hits: list[Hit] = []
        for point in response.points:
            path = point.payload["path"]
            if not rbac.is_allowed(path, allowed_prefixes):  # safety net
                continue
            hits.append(
                Hit(
                    path=path,
                    line=int(point.payload.get("line", 1)),
                    snippet=point.payload.get("snippet", ""),
                    score=float(point.score),
                )
            )
        return hits
