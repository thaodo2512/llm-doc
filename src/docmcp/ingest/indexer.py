"""Build `index.json` (machine) and `index.md` (human) from the manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import Settings
from ..types import IndexEntry


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _title_for(type_: str, text: str, fallback: str) -> str:
    if type_ in ("markdown", "code"):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or fallback
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return fallback


def build_entries(settings: Settings, manifest: dict) -> list[IndexEntry]:
    """Derive index entries from the manifest, reading each curated file."""
    entries: list[IndexEntry] = []
    for source_path, rec in manifest.items():
        curated_logical = rec["curated_path"]
        type_ = rec["type"]
        fs = settings.doc_root / curated_logical.lstrip("/")
        if not fs.is_file():
            continue
        raw = fs.read_bytes()
        text = raw.decode("utf-8", "replace")
        entries.append(
            IndexEntry(
                path=curated_logical,
                title=_title_for(type_, text, Path(curated_logical).name),
                type=type_,
                source_path=source_path,
                bytes=len(raw),
                mtime=fs.stat().st_mtime,
                sha256=_sha256(raw),
            )
        )
    entries.sort(key=lambda entry: entry.path)
    return entries


def write_index(settings: Settings, entries: list[IndexEntry]) -> None:
    settings.doc_root.mkdir(parents=True, exist_ok=True)
    settings.index_json.write_text(
        json.dumps([entry.model_dump() for entry in entries], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = ["# Documentation index", "", f"{len(entries)} document(s).", ""]
    for entry in entries:
        target = entry.path.lstrip("/")
        lines.append(
            f"- [`{entry.path}`]({target}) — {entry.title}  "
            f"_({entry.type}, {entry.bytes} bytes)_"
        )
    settings.index_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
