---
name: doc-html-report
description: >-
  Generate a single self-contained HTML report containing the FULL content of the
  internal documentation (via the docs MCP server). Use when the user asks to
  create, build, export, or generate an HTML report/copy/bundle of the docs, or
  wants a shareable HTML file of the documentation.
---
Prerequisite: the `docs` MCP server is connected (tools `list_docs`, `read_doc`,
`search_docs`). This skill writes one self-contained **HTML file** that contains
each document's **full content** (not a summary).

Use the design in `templates/report.html` (in this skill folder): copy its
`<style>` block **verbatim** and follow its structure (header → table of contents
→ one `<section class="doc">` per document). Keep the output self-contained —
inline CSS only, no external assets or CDNs.

1. Call `list_docs` (no path for everything, or the prefix/topic the user gave) to
   get the documents to cover — `path`, `title`, `type`. If the user named a topic,
   use `search_docs` first and include only the matching docs.
2. For EACH document, `read_doc` it and include its **entire content** in the
   report. The doc store is Markdown, so **render that Markdown to HTML** — headings,
   lists, tables, blockquotes, and fenced code blocks → `<pre><code>`. **Escape**
   HTML special characters in the content so it can't break the page. Never invent
   or summarize — copy what `read_doc` returns.
3. Assemble the report from `templates/report.html` (copy its `<style>` verbatim):
   - brand bar: the company name / logo and the label (leave as placeholders unless
     the user gives a company name or logo);
   - hero header: the kicker, a title, and a sub-line "Generated <today's date>
     &middot; <count> documents &middot; scope <scope>";
   - `<nav class="toc">`: one linked `<li>` per document, each with its type `badge`;
   - one `<section class="doc">` per document → the `summary` shows the title, type
     `badge`, and the real `path`; `.doc-body` holds the rendered full content;
   - footer: the company and current year.
4. Collapsing: wrap each document in `<details open>`. For long documents (roughly
   >400 lines), omit `open` so they start collapsed and the report stays navigable.
   (The template's print styles expand everything when printed.)
5. Save the file (default `docs-report.html`, or a path the user specifies) and
   print the saved file path. Each section already shows the doc's real `path` for
   citation.
