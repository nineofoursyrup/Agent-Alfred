"""Isolated, terminable JSON-only validator. No credentials or resource fetching."""

import json
import sys
from urllib.parse import urldefrag

from jsonschema import Draft7Validator, Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7, DRAFT202012


def refuse(uri):
    raise ValueError("external_reference_unsupported")


def dialect(schema):
    uri = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    if uri in (
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    ):
        return Draft202012Validator, DRAFT202012
    if uri in (
        "http://json-schema.org/draft-07/schema",
        "http://json-schema.org/draft-07/schema#",
    ):
        return Draft7Validator, DRAFT7
    raise ValueError("schema_dialect_unsupported")


def check(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("schema_root_object_required")
    validator_type, specification = dialect(schema)
    validator_type.check_schema(schema)
    resource = Resource.from_contents(schema, default_specification=specification)
    registry = (
        Registry(retrieve=refuse)
        .with_resource("urn:alfred:mcp:schema", resource)
        .crawl()
    )
    resolver = registry.resolver("urn:alfred:mcp:schema").in_subresource(resource)
    visited = set()

    def walk(current, scope, stack):
        node = current.contents
        if not isinstance(node, dict):
            return
        identity = id(node)
        if identity in stack:
            raise ValueError("cyclic_reference_unsupported")
        if identity in visited:
            return
        stack = stack | {identity}
        if any(k in node for k in ("$dynamicRef", "$dynamicAnchor", "$recursiveRef")):
            raise ValueError("dynamic_reference_unsupported")
        if any(
            key not in Draft202012Validator.META_SCHEMA["$vocabulary"]
            for key in node.get("$vocabulary", {})
        ):
            raise ValueError("vocabulary_unsupported")
        if "$schema" in node and dialect(node)[0] is not validator_type:
            raise ValueError("schema_dialect_unsupported")
        if "$ref" in node:
            ref = node["$ref"]
            _, fragment = urldefrag(ref)
            if fragment and not fragment.startswith("/"):
                raise ValueError("pointer_reference_required")
            resolved = scope.lookup(ref)
            target = Resource.from_contents(
                resolved.contents, default_specification=specification
            )
            walk(target, resolved.resolver, stack)
        for child in current.subresources():
            walk(child, scope.in_subresource(child), stack)
        visited.add(identity)

    walk(resource, resolver, set())
    return registry, validator_type


def main():
    raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("validation_input_limit")
    value = json.loads(raw)
    registry, validator_type = check(value["schema"])
    if "instance" in value:
        validator = validator_type(value["schema"], registry=registry)
        if next(validator.iter_errors(value["instance"]), None) is not None:
            raise ValueError("output_schema_mismatch")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Exception strings may contain instance secrets and schema literals.
        reason = (
            str(exc)
            if type(exc) is ValueError
            and str(exc)
            in (
                "schema_root_object_required",
                "schema_dialect_unsupported",
                "external_reference_unsupported",
                "cyclic_reference_unsupported",
                "dynamic_reference_unsupported",
                "vocabulary_unsupported",
                "pointer_reference_required",
                "validation_input_limit",
                "output_schema_mismatch",
            )
            else "schema_invalid_or_unsupported"
        )
        print(json.dumps({"error": reason}))
    else:
        print("{}")
