"""Convert Neo4j values into JSON-serializable Python structures."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from neo4j.graph import Node, Path, Relationship


def _properties(value: Any) -> dict[str, Any]:
    return {key: to_jsonable(item) for key, item in dict(value).items()}


def to_jsonable(value: Any) -> Any:
    """Recursively convert nodes, relationships, paths and temporal types."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, Node):
        return {
            'type': 'node',
            'element_id': value.element_id,
            'labels': list(value.labels),
            'properties': _properties(value),
        }

    if isinstance(value, Relationship):
        return {
            'type': 'relationship',
            'element_id': value.element_id,
            'rel_type': value.type,
            'start_node': value.start_node.element_id,
            'end_node': value.end_node.element_id,
            'properties': _properties(value),
        }

    if isinstance(value, Path):
        return {
            'type': 'path',
            'nodes': [to_jsonable(node) for node in value.nodes],
            'relationships': [to_jsonable(rel) for rel in value.relationships],
        }

    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()

    # neo4j.time.DateTime / Date / Time / Duration
    iso_format = getattr(value, 'iso_format', None)
    if callable(iso_format):
        try:
            return iso_format()
        except Exception:  # noqa: BLE001 - fall through to str()
            pass

    return str(value)
