"""Validate the JSON Schema subset used by our task contracts, offline."""

from __future__ import annotations

import math
import unicodedata


def validate(value, schema: dict, path: str = "response") -> None:
    typ = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "null": type(None),
    }
    if typ not in types:
        raise ValueError(f"{path}: unsupported schema type {typ!r}")
    if not isinstance(value, types[typ]) or (typ in ("integer", "number") and isinstance(value, bool)):
        raise ValueError(f"{path}: expected {typ}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unexpected value {value!r}")
    if typ in ("integer", "number"):
        if (
            not math.isfinite(value)
            or value < schema.get("minimum", -math.inf)
            or value > schema.get("maximum", math.inf)
        ):
            raise ValueError(f"{path}: number out of range")
    if typ == "string":
        if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in value):
            raise ValueError(f"{path}: control character")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ValueError(f"{path}: string length out of range")
    if typ == "object":
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ValueError(f"{path}: unexpected fields {sorted(set(value) - set(props))}")
        for key, child in value.items():
            if key in props:
                validate(child, props[key], f"{path}.{key}")
    if typ == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ValueError(f"{path}: array length out of range")
        for i, child in enumerate(value):
            validate(child, schema["items"], f"{path}[{i}]")
