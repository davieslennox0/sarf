"""Provider extension point.

A provider is a protocol integration (Current Finance today; Aftermath swaps/
perps later). Each provider registers its own MCP tools against the shared
FastMCP instance and receives the shared infra (db, txbuilder client,
registry). Adding Aftermath means adding providers/aftermath.py with its own
tx-builder endpoints — no changes to the tools below or the security layer.
"""

from __future__ import annotations

from typing import Protocol

from mcp.server.fastmcp import FastMCP


class Provider(Protocol):
    name: str

    def register_tools(self, mcp: FastMCP) -> None: ...
