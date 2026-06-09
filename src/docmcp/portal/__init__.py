"""Optional upload/manage portal.

A SEPARATE web service (entrypoint ``docmcp-portal``) that lets authorized users
publish/manage documents into folders their token may write (``writable_prefixes``).
It writes ONLY to the staging/``raw/`` area — never the curated store — and never
touches Docker; the existing cron ``schedule`` ingests what it writes. ``docs-mcp``
stays read-only.
"""
