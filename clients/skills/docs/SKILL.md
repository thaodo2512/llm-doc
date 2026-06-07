---
name: docs
description: >-
  Retrieve project documentation. Use when the user asks about APIs, specs,
  runbooks, configs, or "how do we do X here". Trigger words: docs, our spec,
  runbook, design doc.
---
1. Call list_docs to see the index.
2. Call search_docs with specific keywords (symbols, config keys, exact terms).
3. read_doc the top hit. For long files, request a focused line range instead
   of the whole file.
4. If read_doc returns `truncated=true`, tell the user the result is partial and
   request a narrower `start_line`/`end_line` range when more detail is needed.
5. Always cite the doc path you used.
