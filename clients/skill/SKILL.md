---
name: internal-docs
description: Retrieve internal documentation. Use when the user asks about internal
  APIs, specs, runbooks, configs, or "how do we do X here". Trigger words: internal
  docs, our spec, runbook, design doc.
---
1. Call list_docs to see the index.
2. Call search_docs with specific keywords (symbols, config keys, exact terms).
3. read_doc the top hit (use a line range for long files).
4. Always cite the doc path you used.
