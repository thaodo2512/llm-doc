---
name: doc-html-report
description: >-
  Generate a single self-contained HTML report of the documentation (via the docs
  MCP server), with explicit truncation warnings when the server caps a read. Use
  when the user asks to create, build, export, or generate an HTML report/copy/
  bundle of the docs, or wants a shareable HTML file of the documentation.
---
Prerequisite: the `docs` MCP server is connected (`list_docs`, `read_doc`,
`search_docs`). This skill writes one self-contained **HTML file** containing each
document's available content (not a summary). If a read returns `truncated=true`,
the report must show a visible warning for that document; never silently present
partial content as complete.

This folder ships two assets: `templates/report.html` (the design — its `<style>`
is the single source of truth) and `render.py` (a deterministic Markdown→HTML
renderer + assembler that reuses that `<style>`). **Prefer `render.py`** — it
escapes all content and handles tables/code/escaping reliably, so you do not
hand-roll fragile parsing per run.

1. Decide the SCOPE:
   - Whole corpus or a folder → `list_docs` (no path, or the prefix the user named)
     to get the documents to cover (`path`, `title`, `type`); include each whole doc.
   - A TOPIC (e.g. "multipart transfer") → run `search_docs` first; then for each
     hit `read_doc` a FOCUSED line range around the match (its section/heading) and
     include only those excerpts, marked `"excerpt": true`. Do NOT dump whole
     documents for a topic — a small match in a large PDF-derived doc would produce
     a giant file. Export a full document only if the user explicitly asks for the
     whole file.
2. For each document, gather content with `read_doc`, paging long docs:
   - `read_doc(path, start_line=1, end_line=400)`; if `truncated=false` and
     `total_lines <= 400`, that covers it.
   - Otherwise request the next ranges (`401-800`, `801-1200`, …) until all lines
     are covered or a response returns no new content.
   - If any response has `truncated=true`, keep the returned content and mark that
     document `"truncated": true`. Never invent or summarize — use what `read_doc`
     returns verbatim.
3. Render with the bundled script (PREFERRED). Build a JSON manifest and run it:
   - manifest: `{ "title", "scope", "company"?, "date"?, "docs": [ { "title",
     "type", "path", "content", "truncated"?, "excerpt"?, "collapsed"? } ] }` —
     `content` is the raw Markdown you gathered; set `"collapsed": true` for long
     docs (roughly >400 lines) so they start collapsed.
   - run the bundled renderer from THIS skill folder:
     `python render.py manifest.json docs-report.html` (or pipe the manifest on
     stdin: `… | python render.py - docs-report.html`). It renders Markdown→HTML,
     escapes every value, wraps wide tables so long rows scroll, adds the
     truncation/excerpt notices, and assembles the page from `templates/report.html`.
   - print the saved file path (default `docs-report.html`, or a path the user gave).
4. FALLBACK — only if you cannot run `render.py` (no Python available): assemble by
   hand. Copy the `<style>` from `templates/report.html` **verbatim** and follow its
   structure (brand bar → hero → `<nav class="toc">` → one `<section class="doc">`
   per document → footer). Render the Markdown to HTML (headings, lists, tables,
   blockquotes, fenced code → `<pre><code>`), **escape** all HTML special characters
   so content cannot break the page, and wrap each table in
   `<div class="md-table-wrap">` so wide rows scroll. Add a visible
   `<p class="truncated">…</p>` for truncated docs (and a similar note for topical
   excerpts). Keep it self-contained — inline CSS only, no external assets or CDNs.
   Save the file and print its path. Each section shows the doc's real `path` for
   citation.
