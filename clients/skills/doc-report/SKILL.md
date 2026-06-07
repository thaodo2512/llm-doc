---
name: doc-report
description: >-
  Print a concise terminal report of the internal documentation served by the
  docs MCP server — an inventory of which documents exist, grouped by area and
  type, each with a one-line summary. Use when the user asks to list, report on,
  inventory, or get an overview of the docs in the terminal (e.g. "what docs do
  we have", "report the documentation", "give me an overview of our docs").
---
Prerequisite: the `docs` MCP server is connected (tools `list_docs`, `read_doc`,
`search_docs`). This skill prints a report to the **terminal** and writes no files.

1. Call `list_docs` (no path for everything, or the prefix the user named) to
   enumerate every document — path, title, type, bytes.
2. Group the results by top-level prefix (e.g. `/public`, `/team-fw`) and by type
   (pdf, markdown, code, text, …); count the totals.
3. For a one-line summary per document, call `read_doc` with
   `start_line=1, end_line=20` (or use its title / first heading). If
   `truncated=true`, still summarize from the returned prefix but mark the
   summary as partial. For large sets (50+ docs), summarize per group instead of
   per file to stay fast.
4. Print a clean report:
   - a header: total docs + the count per type;
   - then, per group: the prefix heading, and each doc as
     `  <path>  — <one-line summary>`.
5. Terminal output only — do not create files. Always show the real doc `path` so
   the user can open any with `read_doc`.
