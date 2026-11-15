"""Versioned response schemas, resolved locally without network retrieval."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


class SchemaRegistry:
    def __init__(self, directory):
        self._schemas = {}
        resources = []
        for path in sorted(Path(directory).glob('*.schema.json')):
            raw = path.read_bytes()
            schema = json.loads(raw)
            Draft202012Validator.check_schema(schema)
            schema_id = schema.get('$id')
            if schema_id != path.name.removesuffix('.schema.json') or schema_id in self._schemas:
                raise ValueError(f'invalid or duplicate local schema id: {path}')
            self._schemas[schema_id] = (schema, hashlib.sha256(raw).hexdigest())
            resources.append((schema_id, Resource.from_contents(schema)))
        self._registry = Registry().with_resources(resources)

    def validate(self, schema_id, value):
        schema, digest = self._schemas[schema_id]
        Draft202012Validator(schema, registry=self._registry).validate(value)
        return digest

    def get(self, schema_id):
        return self._schemas[schema_id]
