"""
TigerGraph MCP server: exposes the graph capabilities in
agent/graph_client.py as MCP tools, so an MCP-compatible agent (this
one, Claude Desktop, or any other MCP client) can query/write the
FraudGraph without importing this codebase directly.

This is a purpose-built, minimal server over our own schema and
installed queries -- not the general-purpose community tigergraph-mcp
server (https://github.com/tigergraph/tigergraph-mcp), which exposes
GSQL/schema/admin operations generically. That one is a better fit for
ad-hoc schema exploration; this one exposes exactly the operations this
investigation pipeline needs, typed and validated.

Run standalone:
    python -m agent.mcp_server

Or point any MCP client (Claude Desktop's config, `mcp dev`, etc.) at
this module. agent/investigate.py's default path still calls
GraphClient directly (a Python import, not a network hop, is the
right choice for a tight local loop over 20 cases) -- this server is
the integration point for external agents/tools that want the same
graph capabilities via MCP instead.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from agent.graph_client import get_graph_client

mcp = MCPServer("tigergraph-fraud-graph")


@mcp.tool()
def similar_closed_cases(card_id: str) -> list[dict]:
    """Graph traversal (GSQL query similar_closed_cases_graph): closed
    fraud investigations sitting directly on this card, or naming it as
    a connected card in a ring. Returns case_id, outcome, pattern,
    exposure_usd, analyst_notes for each match."""
    return get_graph_client().similar_closed_cases_graph(card_id)


@mcp.tool()
def write_investigation_case(answer_json: dict) -> str:
    """Writes one investigation's answer (the exact case-pack answer
    format: {case_id, case: {...}, ...}) into TigerGraph as an
    InvestigationCase vertex, linked to every transaction, card, device
    and prior closed case it references. Returns the case_id written."""
    get_graph_client().write_investigation(answer_json)
    return answer_json["case_id"]


if __name__ == "__main__":
    mcp.run()
