"""Path-prefix RBAC is enforced uniformly across every tool."""

from __future__ import annotations

import pytest

from docmcp import rbac
from docmcp.tools import DocTools
from fastmcp.exceptions import ToolError


@pytest.mark.parametrize(
    "path,prefixes,expected",
    [
        ("/public/a.md", ["/"], True),
        ("/public/a.md", ["/public"], True),
        ("/public", ["/public"], True),
        ("/team-fw/x.md", ["/public"], False),
        ("/publicate/x.md", ["/public"], False),  # segment-aware: no /pub* false match
        ("/public/a.md", ["/team-fw", "/public"], True),
        ("/anything", [], False),  # no prefixes => nothing allowed
        ("/anything", [""], True),  # "" behaves like "/"
    ],
)
def test_is_allowed_is_segment_aware(path, prefixes, expected):
    assert rbac.is_allowed(path, prefixes) is expected


def test_scoped_token_blocked_from_restricted_tree_every_tool(ingested):
    tools = DocTools(ingested)
    public_only = ["/public"]

    # list_docs: only /public entries, never /team-fw
    listed = {e.path for e in tools.do_list("", public_only)}
    assert listed and all(p.startswith("/public") for p in listed)
    assert tools.do_list("/team-fw", public_only) == []

    # search_docs: retry_backoff lives only under /team-fw -> no hits for /public
    assert tools.do_search("retry_backoff", 10, public_only) == []

    # read_doc: denied (raises), not silently empty
    with pytest.raises(ToolError):
        tools.do_read("/team-fw/design.md", None, None, public_only)


async def test_http_search_is_prefix_scoped(ingested, live_server):
    async with live_server(ingested) as srv:
        async with srv.client("tok_bob_public") as client:
            hits = (await client.call_tool("search_docs", {"query": "retry_backoff"})).data
            assert all(h.path.startswith("/public") for h in hits)  # i.e. none from /team-fw
