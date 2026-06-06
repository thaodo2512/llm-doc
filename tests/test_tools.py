"""DocTools core: listing, reading, and RBAC denial (brief §7.2 / §7.3)."""

from __future__ import annotations

import httpx
import pytest

from docmcp.server import build_asgi_app
from docmcp.tools import DocTools
from fastmcp.exceptions import ToolError


def test_list_filtered_to_allowed_prefixes(ingested):
    tools = DocTools(ingested)
    all_paths = {e.path for e in tools.do_list("", ["/"])}
    assert {"/public/welcome.md", "/public/notes.txt", "/team-fw/design.md"} <= all_paths

    scoped = tools.do_list("", ["/public"])
    assert scoped and all(e.path.startswith("/public") for e in scoped)


def test_list_path_subtree_filter(ingested):
    tools = DocTools(ingested)
    res = tools.do_list("/team-fw", ["/"])
    assert res and all(e.path.startswith("/team-fw") for e in res)


def test_read_allowed(ingested):
    tools = DocTools(ingested)
    doc = tools.do_read("/public/welcome.md", None, None, ["/"])
    assert "Welcome" in doc.content
    assert doc.total_lines >= 3


def test_read_line_range(ingested):
    tools = DocTools(ingested)
    doc = tools.do_read("/public/welcome.md", 1, 1, ["/"])
    assert "Welcome" in doc.content


def test_read_denied_outside_prefix(ingested):
    tools = DocTools(ingested)
    with pytest.raises(ToolError):
        tools.do_read("/team-fw/design.md", None, None, ["/public"])


def test_read_missing_is_error(ingested):
    tools = DocTools(ingested)
    with pytest.raises(ToolError):
        tools.do_read("/public/missing.md", None, None, ["/"])


def test_read_traversal_is_denied(ingested):
    tools = DocTools(ingested)
    with pytest.raises(ToolError):
        tools.do_read("/../../etc/passwd", None, None, ["/"])


# --- HTTP transport: auth gating (401) ---------------------------------------


async def _post_mcp(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=True
    ) as client:
        return await client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", **(headers or {})},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )


async def test_http_requires_token(ingested):
    app = build_asgi_app(ingested)
    resp = await _post_mcp(app)
    assert resp.status_code == 401


async def test_http_rejects_bad_token(ingested):
    app = build_asgi_app(ingested)
    resp = await _post_mcp(app, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
