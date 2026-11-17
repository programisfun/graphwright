"""graphwright: an MCP server that lets a client read and edit a Neo4j graph.

Two tools only:
    read(cypher, params, max_rows)  -> READ access mode; the server rejects writes
    edit(cypher, params)            -> WRITE access mode; any Cypher, destructive allowed

There is no authentication and no query inspection unless MCP_AUTH_TOKEN is set, in
which case every request must carry `Authorization: Bearer <token>`. The client (an
LLM) is trusted to run whatever it sends, including DETACH DELETE and DROP INDEX.
Bind to loopback, or set MCP_AUTH_TOKEN before exposing this to a network.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any

from dotenv import load_dotenv

# Read .env for local runs and docker-compose. On a PaaS (Render, Fly, ...) the
# variables come from the dashboard instead and this is a no-op.
load_dotenv()

from mcp.server.mcpserver import MCPServer
from neo4j.exceptions import Neo4jError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .neo4j_client import GraphClient

DEFAULT_MAX_ROWS = 500
PUBLIC_PATHS = frozenset({'/health'})

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


@server.custom_route('/health', methods=['GET'])
async def health(_: Request) -> JSONResponse:
    """Liveness probe for Docker/Render. Never requires the bearer token."""
    return JSONResponse({'status': 'ok'})


class BearerTokenMiddleware:
    """Reject HTTP requests that do not carry the configured bearer token."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope['type'] != 'http' or scope.get('path') in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get('authorization', '')
        if not hmac.compare_digest(supplied, f'Bearer {self.token}'):
            response = PlainTextResponse('Unauthorized', status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def bind_address() -> tuple[str, int]:
    """MCP_PORT/MCP_HOST, or the PORT convention used by PaaS platforms."""
    paas_port = os.getenv('PORT')
    port = int(os.getenv('MCP_PORT') or paas_port or '8000')
    host = os.getenv('MCP_HOST') or ('0.0.0.0' if paas_port else '127.0.0.1')
    return host, port


def check_token(token: str | None) -> None:
    """Refuse to serve HTTP without an admin token.

    One rule: no token, no server. No host-based exception and no opt-out, because an
    unauthenticated Cypher shell -- even briefly, even on loopback -- is the one
    failure mode worth a hard stop.
    """
    if not token:
        raise SystemExit(
            'graphwright: MCP_AUTH_TOKEN is not set. Every request must carry '
            'Authorization: Bearer <token>; generate one with: '
            'python -c "import secrets; print(secrets.token_urlsafe(32))" '
            'and put it in .env.'
        )

    if len(token) < 16:
        print(
            f'graphwright: warning: MCP_AUTH_TOKEN is only {len(token)} chars; '
            'generate one with: python -c "import secrets;'
            ' print(secrets.token_urlsafe(32))"',
            flush=True,
        )


def build_app() -> Any:
    host, _ = bind_address()
    token = os.getenv('MCP_AUTH_TOKEN')
    check_token(token)

    # Stateless mode makes each request self-contained: no in-memory session, no
    # instance affinity. Required for scale-to-zero hosts (Render, Cloud Run) where a
    # redeploy or spin-down would otherwise drop live sessions.
    stateless = os.getenv('MCP_STATELESS') == '1'
    app: Any = server.streamable_http_app(host=host, stateless_http=stateless)
    return BearerTokenMiddleware(app, token)


def main() -> None:
    transport = os.getenv('MCP_TRANSPORT', 'streamable-http')
    if transport == 'stdio':
        server.run('stdio')
        return

    import uvicorn

    host, port = bind_address()
    app = build_app()
    auth = 'bearer token' if os.getenv('MCP_AUTH_TOKEN') else 'NO AUTH'
    mode = 'stateless' if os.getenv('MCP_STATELESS') == '1' else 'stateful'
    print(f'graphwright: http://{host}:{port}/mcp ({auth}, {mode})', flush=True)
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main()
