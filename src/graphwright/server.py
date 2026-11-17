"""graphwright: an MCP server that lets a client read and edit a Neo4j graph.

Two tools only:
    read(cypher, params, max_rows)  -> READ access mode; the server rejects writes
    edit(cypher, params)            -> WRITE access mode; any Cypher, destructive allowed

There is no authentication and no query inspection. The client (an LLM) is trusted
to run whatever it sends, including DETACH DELETE and DROP INDEX. Bind to loopback
only, or put a real auth layer in front.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from neo4j.exceptions import Neo4jError

from .neo4j_client import GraphClient

DEFAULT_MAX_ROWS = 500

server = MCPServer(
    name='graphwright',
    instructions=(
        'You can read and write a Neo4j graph. Use `read` for MATCH/RETURN queries; '
        'writes fail there. Use `edit` for CREATE, MERGE, SET, DELETE, REMOVE, indexes '
        'and constraints -- it is unrestricted, so prefer MERGE over CREATE when you '
        'want idempotent writes, and read before deleting. Before introducing a node '
        'label or a relationship type, run db.labels() and db.relationshipTypes() and '
        'reuse the existing vocabulary instead of inventing a near-duplicate.'
    ),
)

_client: GraphClient | None = None
_lock = asyncio.Lock()


async def get_client() -> GraphClient:
    global _client
    async with _lock:
        if _client is None:
            client = GraphClient.from_env()
            await client.verify()
            _client = client
    return _client


@server.tool(
    description=(
        'Run a read-only Cypher query and return rows as JSON. Writes are rejected '
        'because the session uses READ access mode. Use $params for values. '
        'Vocabulary introspection: CALL db.labels(), CALL db.relationshipTypes(), '
        'CALL db.propertyKeys().'
    )
)
async def read(
    cypher: str,
    params: dict[str, Any] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict[str, Any]:
    client = await get_client()
    try:
        return await client.read(cypher, params, max_rows)
    except Neo4jError as error:
        # e.g. writing in READ mode, syntax error, unknown function
        return {'error': str(error), 'code': getattr(error, 'code', None)}


@server.tool(
    description=(
        'Run arbitrary Cypher with full write access: CREATE, MERGE, SET, DELETE, '
        'DETACH DELETE, index and constraint DDL. Pass a list to run several '
        'statements atomically in one transaction. No checks are applied. '
        'Statements in the list do NOT share variables: re-MATCH what you need in each '
        'statement, or use one statement with multiple clauses. '
        'Before introducing a node label or a relationship type, check the existing '
        'vocabulary with read("CALL db.labels()") and '
        'read("CALL db.relationshipTypes()"), then reuse those exact names '
        '(Person not People; WORKS_AT not EMPLOYED_BY). When you create a node with a '
        'label that already exists, inspect that label before writing: '
        'read("MATCH (n:Person) UNWIND keys(n) AS k RETURN k, count(*) AS nodes, '
        'collect(DISTINCT valueType(n[k])) AS types ORDER BY nodes DESC") gives every '
        'property name with its value types across all nodes, and '
        'read("MATCH (n:Person) RETURN n LIMIT 25") shows representative values. '
        'Follow the same property names, casing and value types.'
    )
)
async def edit(
    cypher: str | list[str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = await get_client()
    try:
        return await client.edit(cypher, params)
    except Neo4jError as error:
        # The transaction is rolled back, so nothing was applied.
        return {'error': str(error), 'code': getattr(error, 'code', None)}


def main() -> None:
    transport = os.getenv('MCP_TRANSPORT', 'streamable-http')
    if transport == 'stdio':
        server.run('stdio')
    else:
        server.run(
            'streamable-http',
            host=os.getenv('MCP_HOST', '127.0.0.1'),
            port=int(os.getenv('MCP_PORT', '8000')),
        )


if __name__ == '__main__':
    main()
