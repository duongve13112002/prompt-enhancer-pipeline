"""Versioned response schemas, resolved locally without network retrieval."""

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


class SnapshotCache:
    """Thread-safe LRU of values built from one byte snapshot, keyed by the digest of those same bytes (RES-1).

    `fetch(read, build)` reads once, looks the digest up and, on a miss, builds from exactly the bytes it read: the
    key and the value never come from different reads. The entry is published only when a second read after the
    build returns the same bytes, so a snapshot taken while a writer was changing the files is used once and never
    kept. A failed build publishes nothing, so a refusal is raised again on every call.
    """

    def __init__(self, capacity):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('cache capacity must be a positive integer')
        self._capacity = capacity
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.builds = self.published = 0

    @staticmethod
    def digest(snapshot):
        """Digest of bytes, or of a sequence of (name, bytes) pairs with every length framed."""
        hasher = hashlib.sha256()
        for name, data in [('', snapshot)] if isinstance(snapshot, bytes) else snapshot:
            for part in (name.encode('utf-8'), data):
                hasher.update(len(part).to_bytes(8, 'big'))
                hasher.update(part)
        return hasher.hexdigest()

    def fetch(self, read, build):
        snapshot = read()
        key = self.digest(snapshot)
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self.hits += 1
                return self._entries[key]
            self.builds += 1
        value = build(snapshot)
        if self.digest(read()) == key:
            with self._lock:
                self._entries[key] = value
                self._entries.move_to_end(key)
                self.published += 1
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._entries.clear()
            self.hits = self.builds = self.published = 0


def _read_directory(directory):
    """One snapshot of every `*.schema.json` file: (name, bytes) pairs in name order."""
    return tuple((path.name, path.read_bytes()) for path in sorted(Path(directory).glob('*.schema.json')))


class SchemaRegistry:
    def __init__(self, directory):
        self._build(_read_directory(directory), directory)

    @classmethod
    def from_snapshot(cls, snapshot, directory):
        registry = cls.__new__(cls)
        registry._build(snapshot, directory)
        return registry

    def _build(self, snapshot, directory):
        self._schemas = {}
        resources = []
        for name, raw in snapshot:
            schema = json.loads(raw)
            Draft202012Validator.check_schema(schema)
            schema_id = schema.get('$id')
            if schema_id != name.removesuffix('.schema.json') or schema_id in self._schemas:
                raise ValueError(f'invalid or duplicate local schema id: {Path(directory) / name}')
            self._schemas[schema_id] = (schema, hashlib.sha256(raw).hexdigest())
            resources.append((schema_id, Resource.from_contents(schema)))
        self._registry = Registry().with_resources(resources)

    def with_schema(self, schema_id, schema, digest):
        """A new registry: these schemas plus one checked caller schema (T017-FIX3: the admin bundle's output schema,
        `profiles.output_schema`). The receiver is never changed (cached registries are shared); an id that is already
        registered is refused, so a bundle schema can never shadow a project contract."""
        if schema_id in self._schemas:
            raise ValueError(f'schema id {schema_id!r} is already registered')
        Draft202012Validator.check_schema(schema)
        registry = SchemaRegistry.__new__(SchemaRegistry)
        registry._schemas = dict(self._schemas)
        registry._schemas[schema_id] = (deepcopy(schema), digest)
        registry._registry = self._registry.with_resource(schema_id, Resource.from_contents(deepcopy(schema)))
        return registry

    def validate(self, schema_id, value):
        schema, digest = self._schemas[schema_id]
        Draft202012Validator(schema, registry=self._registry).validate(value)
        return digest

    def validate_with(self, schema, value):
        """Validate against a caller-derived schema (T017-FIX2), resolving any `$ref` through the registered ones."""
        Draft202012Validator(schema, registry=self._registry).validate(value)

    def get(self, schema_id):
        """A private copy, so no caller can change a registry other runs share."""
        schema, digest = self._schemas[schema_id]
        return deepcopy(schema), digest


# Safety-bound: distinct schema directories kept in one process (the project has one; tests use temporary ones).
REGISTRY_CACHE_CAPACITY = 16
_REGISTRIES = SnapshotCache(REGISTRY_CACHE_CAPACITY)


def with_output_schema(registry, output_schema):
    """`registry` with the profile's bundle output schema `(schema_id, schema, sha256 hex)` added, or `registry` itself
    when the profile has none (`output_schema_id: none`, or a project contract id)."""
    return registry if output_schema is None else registry.with_schema(*output_schema)


def cached_registry(directory):
    """The registry of `directory`, built once while every schema file keeps its bytes (RES-1 row 10).

    Every call reads the files (about 0.3 MB here) and hashes them; only a changed, added or removed file costs a
    rebuild, which checks every schema again.
    """
    return _REGISTRIES.fetch(lambda: _read_directory(directory),
                             lambda snapshot: SchemaRegistry.from_snapshot(snapshot, directory))
