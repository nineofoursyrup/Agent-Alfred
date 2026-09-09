"""Tool input schema subset v1; unsupported declarations fail at startup."""

import math
from collections.abc import Mapping
from types import MappingProxyType

SCHEMA_VERSION = 1
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_KEYS = {
    "type",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
}


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    return value


def validate_schema(schema):
    if not isinstance(schema, Mapping):
        raise ValueError("schema must be an object")
    if set(schema) - _KEYS:
        raise ValueError("unsupported schema keyword")
    if schema.get("type") not in _TYPES:
        raise ValueError("invalid schema type")
    allowed = {"type", "description", "enum"} | {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"},
        "string": {"minLength", "maxLength"},
        "number": {"minimum", "maximum"},
        "integer": {"minimum", "maximum"},
        "boolean": set(),
        "null": set(),
    }[schema["type"]]
    if set(schema) - allowed:
        raise ValueError("schema keyword does not apply to this type")
    if "enum" in schema and (
        not isinstance(schema["enum"], (list, tuple)) or not schema["enum"]
    ):
        raise ValueError("enum must be a nonempty array")
    for bound in ("minimum", "maximum"):
        if bound in schema and (
            type(schema[bound]) not in (int, float) or not math.isfinite(schema[bound])
        ):
            raise ValueError("invalid numeric bound")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("properties must be an object")
    for child in properties.values():
        validate_schema(child)
    required = schema.get("required", ())
    if not isinstance(required, (list, tuple)) or any(
        not isinstance(key, str) or key not in properties for key in required
    ):
        raise ValueError("invalid required fields")
    if (
        "additionalProperties" in schema
        and type(schema["additionalProperties"]) is not bool
    ):
        raise ValueError("additionalProperties must be boolean")
    if schema["type"] == "array":
        validate_schema(schema.get("items"))
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("invalid schema bound")


def validate_input(schema, value, path="arguments"):
    kind = schema["type"]
    matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kind == "number" and matches[kind] and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if not matches[kind]:
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        for key, item in value.items():
            if key in properties:
                validate_input(properties[key], item, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path} contains an undeclared field")
    if kind == "array":
        for index, item in enumerate(value):
            validate_input(schema["items"], item, f"{path}[{index}]")
    bounds = (
        ("minLength", "maxLength")
        if kind == "string"
        else ("minItems", "maxItems")
        if kind == "array"
        else ("minimum", "maximum")
        if kind in ("integer", "number")
        else None
    )
    if bounds:
        measured = len(value) if kind in ("string", "array") else value
        low, high = bounds
        if low in schema and measured < schema[low]:
            raise ValueError(f"{path} is below the minimum")
        if high in schema and measured > schema[high]:
            raise ValueError(f"{path} exceeds the maximum")
