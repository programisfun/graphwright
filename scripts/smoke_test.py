"""Smoke test: call both tools over Streamable HTTP.

    uv run python scripts/smoke_test.py            # expects the server on :8000
"""

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = 'http://127.0.0.1:8000/mcp'


async def main() -> None:
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print('tools:', [tool.name for tool in (await session.list_tools()).tools])

            created = await session.call_tool(
                'edit',
                {
                    'cypher': [
                        'MERGE (n:Smoke {id: $id}) SET n.seen_at = datetime()',
                        'MATCH (n:Smoke {id: $id}) RETURN n',
                    ],
                    'params': {'id': 'smoke-1'},
                },
            )
            print('edit ->', created.content[0].text[:300])

            fetched = await session.call_tool(
                'read',
                {
                    'cypher': 'MATCH (n:Smoke) RETURN n.id AS id, n.seen_at AS seen_at',
                },
            )
            print('read ->', fetched.content[0].text[:300])

            blocked = await session.call_tool(
                'read', {'cypher': 'CREATE (n:ShouldNotExist)'}
            )
            print('write via read ->', blocked.content[0].text[:300])

            cleaned = await session.call_tool(
                'edit',
                {
                    'cypher': 'MATCH (n:Smoke {id: $id}) DETACH DELETE n',
                    'params': {'id': 'smoke-1'},
                },
            )
            print('cleanup ->', cleaned.content[0].text[:200])


asyncio.run(main())
