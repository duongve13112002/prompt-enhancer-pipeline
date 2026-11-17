"""All response contracts are checked and references resolve locally."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from jsonschema import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from prompt_enhancer import ConfigurationError
from prompt_enhancer.config import _SCHEMA_CACHE, _schema
from prompt_enhancer.model_schemas import _REGISTRIES, SchemaRegistry, SnapshotCache, cached_registry

SCHEMAS = Path(__file__).resolve().parents[1] / 'contracts' / 'schemas'
BASE = {'$schema': 'https://json-schema.org/draft/2020-12/schema'}


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


# RES-1 row 10: one registry per unchanged directory; key and value always from one snapshot.

# Two schemas of equal length, so neither size nor (within timestamp granularity) mtime tells them apart.
DRAFT = b'"$schema": "https://json-schema.org/draft/2020-12/schema", '
VERSION_A = b'{' + DRAFT + b'"$id": "x", "type": "string" }'
VERSION_B = b'{' + DRAFT + b'"$id": "x", "type": "integer"}'
assert len(VERSION_A) == len(VERSION_B)


def write_schema(directory, schema_id, schema):
    (directory / f'{schema_id}.schema.json').write_text(json.dumps({**BASE, '$id': schema_id, **schema}))


def test_cached_registry_equals_uncached_and_is_built_once():
    _REGISTRIES.clear()
    fresh = SchemaRegistry(SCHEMAS)
    cached = cached_registry(SCHEMAS)
    assert cached_registry(SCHEMAS) is cached
    assert (_REGISTRIES.builds, _REGISTRIES.hits, _REGISTRIES.published) == (1, 1, 1)
    assert cached._schemas == fresh._schemas
    for schema_id in fresh._schemas:
        assert cached.get(schema_id) == fresh.get(schema_id)
    with pytest.raises(ValidationError):
        cached.validate('intent_ir.v0', {})


def test_cached_registry_rebuilds_on_change_addition_and_removal(tmp_path):
    _REGISTRIES.clear()
    write_schema(tmp_path, 'child', {'type': 'integer'})
    write_schema(tmp_path, 'parent', {'$ref': 'child'})
    first = cached_registry(tmp_path)
    first.validate('parent', 1)
    write_schema(tmp_path, 'child', {'type': 'string'})
    changed = cached_registry(tmp_path)
    assert changed is not first
    changed.validate('parent', 'text')
    with pytest.raises(ValidationError):
        changed.validate('parent', 1)
    write_schema(tmp_path, 'extra', {'type': 'null'})
    added = cached_registry(tmp_path)
    assert added is not changed and added.validate('extra', None)
    (tmp_path / 'extra.schema.json').unlink()
    removed = cached_registry(tmp_path)
    with pytest.raises(KeyError):
        removed.get('extra')
    assert removed._schemas == changed._schemas  # the same bytes again give the same registry content


def test_cached_registry_refuses_invalid_schema_on_every_call(tmp_path):
    _REGISTRIES.clear()
    write_schema(tmp_path, 'local', {'type': 'not-a-json-type'})
    for _ in range(2):
        with pytest.raises(SchemaError):
            cached_registry(tmp_path)
    assert _REGISTRIES.builds == 2 and _REGISTRIES.published == 0


def test_shared_registry_hands_out_copies(tmp_path):
    _REGISTRIES.clear()
    write_schema(tmp_path, 'local', {'type': 'string'})
    schema, _ = cached_registry(tmp_path).get('local')
    schema['type'] = 'integer'
    assert cached_registry(tmp_path).get('local')[0]['type'] == 'string'
    cached_registry(tmp_path).validate('local', 'still a string')


def test_registry_changed_during_build_is_used_once_never_published(tmp_path, monkeypatch):
    _REGISTRIES.clear()
    path = tmp_path / 'x.schema.json'
    path.write_bytes(VERSION_A)
    original = SchemaRegistry.from_snapshot.__func__

    def racing(cls, snapshot, directory):
        path.write_bytes(VERSION_B)  # labelled race double: a writer lands while the registry is being built
        return original(cls, snapshot, directory)

    monkeypatch.setattr(SchemaRegistry, 'from_snapshot', classmethod(racing))
    built = cached_registry(tmp_path)
    assert built.get('x') == (json.loads(VERSION_A), hashlib.sha256(VERSION_A).hexdigest())
    assert _REGISTRIES.published == 0
    monkeypatch.undo()
    assert cached_registry(tmp_path).get('x')[0]['type'] == 'integer'
    assert _REGISTRIES.published == 1


def test_config_schema_same_size_same_mtime_rewrite_is_seen(tmp_path):
    """The T-014 P2 split: a stat key with a later read let equal-size, equal-mtime contents share an entry."""
    _SCHEMA_CACHE.clear()
    path = tmp_path / 'contracts/schemas/sample.v0.schema.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(VERSION_A)
    stat = path.stat()
    assert _schema(tmp_path, 'sample.v0')['type'] == 'string'
    path.write_bytes(VERSION_B)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns
    assert _schema(tmp_path, 'sample.v0')['type'] == 'integer'


def test_config_schema_changed_during_build_is_not_published(tmp_path, monkeypatch):
    import prompt_enhancer.config as config

    _SCHEMA_CACHE.clear()
    path = tmp_path / 'contracts/schemas/sample.v0.schema.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(VERSION_A)
    original = config._parsed_schema

    def racing(raw):
        path.write_bytes(VERSION_B)  # labelled race double
        return original(raw)

    monkeypatch.setattr(config, '_parsed_schema', racing)
    assert _schema(tmp_path, 'sample.v0')['type'] == 'string'
    assert _SCHEMA_CACHE.published == 0
    monkeypatch.undo()
    assert _schema(tmp_path, 'sample.v0')['type'] == 'integer'


def check_entries(cache, expected):
    """Every published entry's value is the one its key's bytes give."""
    with cache._lock:
        entries = dict(cache._entries)
    for key, value in entries.items():
        assert key in expected, 'an entry was published for bytes no writer produced'
        assert expected[key](value)


def hammer(read, alive, deadline_s=60):
    """Read while a writer runs; each read is a valid version or a refusal, never a value of other bytes."""
    seen, refused = set(), 0
    deadline = time.monotonic() + deadline_s
    while alive() and time.monotonic() < deadline:
        try:
            seen.add(read())
        except (ValueError, SchemaError, OSError, ConfigurationError):
            refused += 1  # a half-written file is refused, as any undecodable schema is
    assert time.monotonic() < deadline, 'writer did not finish in time'
    assert seen <= {'string', 'integer'}
    return seen, refused


def readers(tmp_path):
    config_path = tmp_path / 'contracts/schemas/x.schema.json'
    config_path.parent.mkdir(parents=True)
    registry_dir = tmp_path / 'registry'
    registry_dir.mkdir()
    for path in (config_path, registry_dir / 'x.schema.json'):
        path.write_bytes(VERSION_A)
    targets = (config_path, registry_dir / 'x.schema.json')
    reads = (lambda: _schema(tmp_path, 'x')['type'], lambda: cached_registry(registry_dir).get('x')[0]['type'])
    expect_config = {SnapshotCache.digest(raw): (lambda raw: lambda value: value == json.loads(raw))(raw)
                     for raw in (VERSION_A, VERSION_B)}
    expect_registry = {SnapshotCache.digest((('x.schema.json', raw),)):
                       (lambda raw: lambda value: value.get('x')[1] == hashlib.sha256(raw).hexdigest())(raw)
                       for raw in (VERSION_A, VERSION_B)}
    return targets, reads, (expect_config, expect_registry)


REWRITES = 400  # even: the last write is VERSION_B


@pytest.mark.parametrize('which', [0, 1], ids=['config_schema', 'registry'])
def test_concurrent_writer_thread_never_poisons_the_cache(tmp_path, which):
    _SCHEMA_CACHE.clear()
    _REGISTRIES.clear()
    targets, reads, expected = readers(tmp_path)

    def write():
        for index in range(REWRITES):
            targets[which].write_bytes(VERSION_B if index % 2 else VERSION_A)

    writer = threading.Thread(target=write)
    writer.start()
    hammer(reads[which], writer.is_alive)
    writer.join(timeout=30)
    assert not writer.is_alive()
    check_entries((_SCHEMA_CACHE, _REGISTRIES)[which], expected[which])
    assert reads[which]() == 'integer'


WRITER = '''
import faulthandler, pathlib, sys
faulthandler.enable()
path, a, b, count = pathlib.Path(sys.argv[1]), bytes.fromhex(sys.argv[2]), bytes.fromhex(sys.argv[3]), int(sys.argv[4])
for index in range(count):
    path.write_bytes(b if index % 2 else a)
'''


@pytest.mark.parametrize('which', [0, 1], ids=['config_schema', 'registry'])
def test_concurrent_writer_process_never_poisons_the_cache(tmp_path, which):
    _SCHEMA_CACHE.clear()
    _REGISTRIES.clear()
    targets, reads, expected = readers(tmp_path)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    child = subprocess.Popen([sys.executable, '-X', 'faulthandler', '-I', '-c', WRITER, str(targets[which]),
                              VERSION_A.hex(), VERSION_B.hex(), str(REWRITES * 5)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    try:
        hammer(reads[which], lambda: child.poll() is None)
        _, stderr = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    assert child.returncode == 0, stderr.decode(errors='replace')
    check_entries((_SCHEMA_CACHE, _REGISTRIES)[which], expected[which])
    assert reads[which]() == 'integer'
