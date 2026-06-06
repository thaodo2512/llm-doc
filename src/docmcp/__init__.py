"""docmcp — a self-hosted MCP server for internal documentation.

Two paths share one curated Markdown doc store:
  * serve path  (server.py): authenticate -> authorize -> search/read -> return text
  * build path  (ingest/):   parse raw sources -> curated Markdown + index (+ optional embeddings)
"""

__version__ = "0.1.0"
