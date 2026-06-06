---
name: doc-html-report
description: >-
  Summarize the internal documentation (via the docs MCP server) and generate a
  single self-contained HTML report file. Use when the user asks to create, build,
  or generate an HTML report or summary of the docs, to "summarize the docs into
  HTML", or wants a shareable summary file of the documentation.
---
Prerequisite: the `docs` MCP server is connected (tools `list_docs`, `read_doc`,
`search_docs`). This skill writes a self-contained **HTML file**.

1. Call `list_docs` (no path for everything, or the prefix/topic the user gave) to
   get the documents to cover — path, title, type. If the user named a topic, use
   `search_docs` to find the relevant docs first.
2. For each document, `read_doc` it and write a faithful **2–4 sentence summary**
   (its purpose + key topics). For long docs, read a line range / the head rather
   than the whole file. Summarize only what `read_doc` returns — do not invent.
3. Generate one self-contained `docs-report.html` (inline `<style>`, no external
   assets or CDNs) with:
   - a title, a "generated on <today's date>" line, and the total doc count;
   - a table of contents linking to each doc section;
   - one section per doc: its title, `path`, type, and the summary.
   Keep the CSS minimal and readable (system font, light background, subtle
   borders, a max-width for legibility).
4. Save the file to the current directory (or a path the user specifies) and print
   the saved file path.
5. Cite each doc's real `path` in its section.
