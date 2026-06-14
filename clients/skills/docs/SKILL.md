---
name: docs
description: >-
  Answer a SPECIFIC documentation question by finding and citing the exact
  passage — APIs, specs, config keys, error strings, runbook steps, "how do we
  do X here". Use for targeted lookups where the user can name concrete terms.
  NOT for: an inventory/overview of what exists ("what docs do we have" -> use
  doc-report), building or exporting an HTML report/bundle ("make a docs report"
  -> use doc-html-report), or locating something from a vague, half-remembered
  description ("I remember something about..." -> use doc-find).
---
1. Call list_docs to see the index.
2. Call search_docs with specific keywords (symbols, config keys, exact terms).
3. read_doc the top hit. For long files, request a focused line range instead
   of the whole file.
4. If read_doc returns `truncated=true`, tell the user the result is partial and
   request a narrower `start_line`/`end_line` range when more detail is needed.
5. Always cite the doc path you used.
