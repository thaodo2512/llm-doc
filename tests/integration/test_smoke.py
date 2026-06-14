"""End-to-end smoke test: boot the server, drive tools over authenticated HTTP.

Exercises the full DI path (get_access_token over Streamable HTTP) that unit
tests can't reach.
"""

from __future__ import annotations

import pytest


async def test_smoke_list_search_read(ingested, live_server):
    async with live_server(ingested) as srv:
        async with srv.client("tok_alice_full") as client:
            tool_names = {t.name for t in await client.list_tools()}
            assert {"list_docs", "search_docs", "read_doc", "semantic_search"} <= tool_names

            docs = (await client.call_tool("list_docs", {"path": ""})).data
            assert any(d.path == "/public/welcome.md" for d in docs)

            content = (await client.call_tool("read_doc", {"path": "/public/welcome.md"})).data
            assert "Welcome" in content.content
            assert content.total_lines >= 3

            hits = (await client.call_tool("search_docs", {"query": "deploy_token"})).data
            assert any(h.path == "/public/welcome.md" for h in hits)
            assert all(h.line >= 1 and h.snippet for h in hits)

            # semantic_search is built but disabled by default -> clear error, no Qdrant/OpenAI.
            with pytest.raises(Exception):
                await client.call_tool("semantic_search", {"query": "deploy_token"})


async def test_smoke_rbac_scoped_token(ingested, live_server):
    async with live_server(ingested) as srv:
        async with srv.client("tok_bob_public") as client:
            docs = (await client.call_tool("list_docs", {"path": ""})).data
            assert docs and all(d.path.startswith("/public") for d in docs)

            with pytest.raises(Exception):  # ToolError surfaces as a client-side error
                await client.call_tool("read_doc", {"path": "/team-fw/design.md"})
