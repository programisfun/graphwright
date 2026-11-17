"""Neo4j access for graphwright: one read path, one unrestricted write path."""

from __future__ import annotations

import os
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from .serialization import to_jsonable


def _counters(summary: Any) -> dict[str, int]:
    counters = summary.counters
    return {
        'nodes_created': counters.nodes_created,
        'nodes_deleted': counters.nodes_deleted,
        'relationships_created': counters.relationships_created,
        'relationships_deleted': counters.relationships_deleted,
        'properties_set': counters.properties_set,
        'labels_added': counters.labels_added,
        'labels_removed': counters.labels_removed,
        'indexes_added': counters.indexes_added,
        'indexes_removed': counters.indexes_removed,
        'constraints_added': counters.constraints_added,
        'constraints_removed': counters.constraints_removed,
    }


def _rows(record: Any) -> dict[str, Any]:
    return {key: to_jsonable(value) for key, value in record.items()}


class GraphClient:
    """Thin async wrapper around the Neo4j driver, with no policy checks."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str = 'neo4j',
    ) -> None:
        self.database = database
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            uri, auth=(user, password)
        )

    @classmethod
    def from_env(cls) -> 'GraphClient':
        return cls(
            uri=os.getenv('NEO4J_URI', 'bolt://localhost:7687'),
            user=os.getenv('NEO4J_USER', 'neo4j'),
            password=os.getenv('NEO4J_PASSWORD', 'graphwright'),
            database=os.getenv('NEO4J_DATABASE', 'neo4j'),
        )

    async def close(self) -> None:
        await self._driver.close()

    async def verify(self) -> None:
        await self._driver.verify_connectivity()

    async def read(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        max_rows: int = 500,
    ) -> dict[str, Any]:
        """Run one statement in READ access mode (writes are rejected by the server)."""
        async with self._driver.session(
            database=self.database, default_access_mode='READ'
        ) as session:
            result = await session.run(cypher, params or {})
            rows: list[dict[str, Any]] = []
            truncated = False
            async for record in result:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(_rows(record))
            if truncated:
                await result.consume()

        return {'rows': rows, 'row_count': len(rows), 'truncated': truncated}

    async def edit(
        self,
        cypher: str | list[str],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one or more statements in one WRITE transaction. No checks applied."""
        statements = [cypher] if isinstance(cypher, str) else list(cypher)

        async def work(tx: Any) -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for statement in statements:
                result = await tx.run(statement, params or {})
                rows = [_rows(record) async for record in result]
                summary = await result.consume()
                results.append(
                    {
                        'rows': rows,
                        'counters': _counters(summary),
                        'query_type': summary.query_type,
                    }
                )
            return results

        async with self._driver.session(
            database=self.database, default_access_mode='WRITE'
        ) as session:
            results = await session.execute_write(work)

        counters: dict[str, int] = {}
        for item in results:
            for key, value in item['counters'].items():
                counters[key] = counters.get(key, 0) + value

        return {
            'statements': len(results),
            'counters': counters,
            'results': results,
        }
