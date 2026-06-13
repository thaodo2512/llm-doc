"""The docmcp admin/setup console — a host-run web UI over the docmcp.sh CLI.

A SEPARATE service from the doc-upload portal (``docmcp.portal``). The console runs
inside the ``docs-mcp:console`` image with the Docker socket + repo bind-mounted, so it
can drive the full lifecycle (build / ingest / serve / stop / schedule) and edit
tokens/groups/.env by shelling out to the existing ``docmcp.sh`` verbs. It is published
on LOOPBACK ONLY and requires an admin (whole-corpus) bearer token.

Security boundary: every shell-out is built in ``commands.py`` as a validated argv LIST
(never a shell string); ``runner.py`` executes it. Auth + CSRF live in ``auth.py``.
"""
