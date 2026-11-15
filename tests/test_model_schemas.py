"""All response contracts are checked and references resolve locally."""

import json
from pathlib import Path

import pytest
from jsonschema import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from prompt_enhancer.model_schemas import SchemaRegistry


def test_all_project_schemas_load():
    registry = SchemaRegistry(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas')
    with pytest.raises(ValidationError):
        registry.validate('intent_ir.v0', {})


def test_local_ref_and_no_remote_retrieval(tmp_path):
    base = {'$schema': 'https://json-schema.org/draft/2020-12/schema'}
    (tmp_path / 'child.schema.json').write_text(json.dumps({**base, '$id': 'child', 'type': 'integer'}))
    (tmp_path / 'parent.schema.json').write_text(json.dumps({**base, '$id': 'parent', '$ref': 'child'}))
    (tmp_path / 'remote.schema.json').write_text(json.dumps({**base, '$id': 'remote', '$ref': 'https://example.org/schema'}))
    registry = SchemaRegistry(tmp_path)
    assert len(registry.validate('parent', 1)) == 64
    with pytest.raises(ValidationError):
        registry.validate('parent', 'wrong')
    with pytest.raises(Unresolvable):
        registry.validate('remote', 1)


def test_invalid_schema_and_mismatched_id_rejected(tmp_path):
    path = tmp_path / 'local.schema.json'
    path.write_text(json.dumps({'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                '$id': 'local', 'type': 'not-a-json-type'}))
    with pytest.raises(SchemaError):
        SchemaRegistry(tmp_path)
    path.write_text(json.dumps({'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                '$id': 'other', 'type': 'string'}))
    with pytest.raises(ValueError):
        SchemaRegistry(tmp_path)
