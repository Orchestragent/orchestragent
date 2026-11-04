"""The single canonical JSON encoder for persistence, hashes and HTTP."""

import json
import math
from collections.abc import Mapping


def normalize(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite numbers cannot be persisted")
        return round(value, 6)
    if isinstance(value, Mapping):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    return value


def dumps(value: object) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def object_schema(properties: dict[str, object]) -> dict[str, object]:
    """A closed JSON-schema object whose every property is required."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
