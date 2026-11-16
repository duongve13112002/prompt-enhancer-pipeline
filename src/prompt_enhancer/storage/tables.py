"""Physical Arrow schemas and validation for the 16 contract tables (contracts/README.md:28-45, R-016).

Every column the contract lists has an explicit type and nullability. Object or array fields of a JSON
artifact are canonical JSON text, validated against the contract's JSON schema on write and on read, so a
draft v0 schema does not freeze a nested Arrow layout. Enums come from the contract's schema files where
one defines them, otherwise from the contract's enum table (protocol-bound, cited per constant).
"""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator
import pyarrow as pa
from referencing import Registry, Resource

from ..config import ConfigurationError, _schema
from ._fs import StorageError
from .identity import PHYSICAL_VERSION, check_artifact_key, check_folder_id, concept_id, is_concept_id, is_image_id, normalise_name

# A fixed offset, not the zone name 'UTC': converting a named zone back to Python needs tzdata on Windows.
S, I, F, TS = pa.large_string(), pa.int64(), pa.float64(), pa.timestamp('us', tz='+00:00')
LS = pa.list_(pa.field('element', pa.large_string()))  # Parquet's compliant item name, so both formats agree
JSON = 'json'  # marker: canonical JSON text in a large_string column

# protocol-bound enums from contracts/README.md (tables and enums sections); no schema file defines them.
NAME_SOURCE = ('info_file', 'folder_name', 'model', 'override', 'folder_override')  # decisions 125, 131
CONCEPT_KIND = ('character', 'clothing', 'place', 'object', 'style', 'other')  # README.md:60
KIND_ORIGIN = ('override', 'generated')  # README.md:31
FACT_ORIGIN = ('info_file', 'override', 'generated')  # README.md:68
TEXT_KIND = ('name', 'description', 'alias', 'card_summary')  # README.md:36
SYNC_STATES = ('queued', 'done', 'failed')  # README.md commit protocol (T-006, PM-approved)
# 'deleting': GC committed to removing it under the store lock; holds and rollback refuse it (fix round 1, F3).
SNAPSHOT_STATES = ('building', 'validated', 'published', 'retired', 'deleting', 'collected', 'failed')


@dataclass(frozen=True)
class Ref:
    """A JSON pointer into a contract schema file."""

    schema_id: str
    pointer: str


@dataclass(frozen=True)
class Col:
    name: str
    type: object
    nullable: bool = False
    enum: object = None  # tuple of values, or (schema_id, JSON pointer to an enum list)
    subschema: object = None  # for JSON columns: (schema_id, JSON pointer) or None for any JSON value


@dataclass(frozen=True)
class Spec:
    name: str
    key: tuple
    columns: tuple = ()
    document: str = None  # schema id: one column per top-level field, rows validated as whole documents
    vector: bool = False  # Arrow-only embedding table with a fixed_size_list<float32, dim> column
    references: tuple = field(default=())  # ((columns), table, (columns))


def _c(name, type_, nullable=False, enum=None, subschema=None):
    return Col(name, type_, nullable, enum, subschema)


OBS = 'image_observation.v0'
OBS_ITEM = '/properties/observations/items/properties'
ALIAS_ITEM = '/properties/aliases/items/properties'
ENV = 'response_envelope.v0'
SCEN = 'eval_scenario.v0'

SPECS = {spec.name: spec for spec in (
    Spec('folders', ('folder_id',), (
        _c('folder_id', S), _c('info_file_hash', S, True), _c('declared_names', LS),
        _c('declared_descriptions', S, True), _c('name_source', S, True, NAME_SOURCE), _c('error', S))),
    Spec('concepts', ('concept_id',), (
        _c('concept_id', S), _c('normalised_name', S), _c('display_name', S),
        _c('kind', S, True, CONCEPT_KIND), _c('kind_origin', S, True, KIND_ORIGIN))),
    Spec('folder_concepts', ('folder_id', 'concept_id'), (
        _c('folder_id', S), _c('concept_id', S), _c('position', I), _c('name_as_written', S),
        _c('description', S, True), _c('description_origin', S, True, FACT_ORIGIN),
        _c('description_revision', S, True)),
        references=((('folder_id',), 'folders', ('folder_id',)), (('concept_id',), 'concepts', ('concept_id',)))),
    Spec('images', ('image_id',), (
        _c('image_id', S), _c('size_bytes', I), _c('format', S, True), _c('width', I, True),
        _c('height', I, True), _c('decode_status', S, True), _c('near_duplicate_group', S, True))),
    Spec('folder_images', ('folder_id', 'relative_path'), (
        _c('folder_id', S), _c('relative_path', S), _c('image_id', S), _c('modified_at', TS)),
        references=((('folder_id',), 'folders', ('folder_id',)), (('image_id',), 'images', ('image_id',)))),
    Spec('image_embeddings', ('image_id', 'embedder_revision'), (
        _c('image_id', S), _c('embedder_revision', S), _c('dim', I)), vector=True,
        references=((('image_id',), 'images', ('image_id',)),)),
    # text_hash joins the key: several aliases of one concept share (concept_id, 'alias', revision); T-006 Noticed.
    Spec('concept_text_embeddings', ('concept_id', 'text_kind', 'embedder_revision', 'text_hash'), (
        _c('concept_id', S), _c('text_kind', S, False, TEXT_KIND), _c('embedder_revision', S),
        _c('text_hash', S), _c('dim', I)), vector=True,
        references=((('concept_id',), 'concepts', ('concept_id',)),)),
    Spec('observations', ('context_key', 'image_id', 'concept_id', 'facet', 'model_revision', 'prompt_id', 'facet_schema_version'), (
        _c('context_key', S), _c('image_id', S), _c('concept_id', S), _c('facet', S), _c('model_revision', S), _c('prompt_id', S),
        _c('facet_schema_version', S), _c('facet_group', S, False, Ref(OBS, OBS_ITEM + '/facet_group/enum')),
        _c('value', S), _c('visibility', S, False, Ref(OBS, OBS_ITEM + '/visibility/enum')),
        _c('region', JSON, subschema=Ref(OBS, OBS_ITEM + '/region')), _c('view', S), _c('quality', F),
        _c('origin', S)),
        references=((('image_id',), 'images', ('image_id',)), (('concept_id',), 'concepts', ('concept_id',)))),
    Spec('cards', ('concept_id',), document='concept_card.v0',
         references=((('concept_id',), 'concepts', ('concept_id',)),)),
    Spec('aliases', ('concept_id', 'alias_text'), (
        _c('concept_id', S), _c('alias_text', S), _c('origin', S, False, Ref('alias_set.v0', ALIAS_ITEM + '/origin/enum')),
        _c('status', S, False, Ref('alias_set.v0', ALIAS_ITEM + '/status/enum')), _c('collides_with', LS),
        _c('language', S, True), _c('revisions', JSON, subschema=Ref('alias_set.v0', '/properties/revisions'))),
        references=((('concept_id',), 'concepts', ('concept_id',)),)),
    Spec('sync_state', ('stage', 'input_key'), (
        _c('stage', S), _c('input_key', S), _c('output_key', S, True), _c('output_size', I, True),
        _c('output_sha256', S, True), _c('state', S, False, SYNC_STATES), _c('model_revision', S, True),
        _c('prompt_revision', S, True), _c('schema_revision', S, True), _c('call_records', JSON),
        _c('error', S, True), _c('run_id', S), _c('recorded_at', TS))),
    Spec('snapshots', ('snapshot_id',), (
        _c('snapshot_id', S), _c('parent_snapshot_id', S, True), _c('state', S, False, SNAPSHOT_STATES),
        _c('created_at', TS), _c('published_at', TS, True), _c('manifest_sha256', S, True),
        _c('source_scan_summary', JSON))),
    Spec('eval_scenarios', ('scenario_id',), document=SCEN),
    Spec('eval_labels', ('scenario_id', 'label_id'), (
        _c('scenario_id', S), _c('label_id', S), _c('answer', JSON),
        _c('tier', S, False, Ref(SCEN, '/properties/tier/enum')), _c('split', S, False, Ref(SCEN, '/properties/split/enum')))),
    Spec('eval_results', ('run_id', 'scenario_id'), (
        _c('run_id', S), _c('scenario_id', S), _c('metrics', JSON), _c('status', S, False, Ref(ENV, '/properties/status/enum')),
        _c('config_hash', S))),
    Spec('request_traces', ('request_id',), (
        _c('request_id', S), _c('profile_id', S), _c('snapshot_id', S, True), _c('effective_config_hash', S),
        _c('status', S, False, Ref(ENV, '/properties/status/enum')),
        _c('retrieval_state', S, False, Ref(ENV, '/properties/retrieval/properties/state/enum')),
        _c('seeds', JSON), _c('warnings', LS), _c('calls', JSON), _c('latency_ms', F, True), _c('body', JSON),
        _c('created_at', TS))),
)}

SNAPSHOT_TABLES = ('folders', 'concepts', 'folder_concepts', 'images', 'folder_images', 'image_embeddings',
                   'concept_text_embeddings', 'observations', 'cards', 'aliases')  # docs/execution.md:70
MANIFEST_SCHEMA = pa.schema([
    pa.field('kind', S, False), pa.field('path', S, False), pa.field('table', S, True), pa.field('format', S, True),
    pa.field('size_bytes', I, True), pa.field('sha256', S, True), pa.field('rows', I, True),
    pa.field('physical_version', I, False)], metadata={'pe.table': 'manifest', 'pe.physical_version': str(PHYSICAL_VERSION)})
MANIFEST_KINDS = ('table', 'file', 'artifact')


def _pointer(document, pointer):
    node = document
    for part in pointer.strip('/').split('/'):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


class Catalog:
    """Resolves contract schemas under one project root and validates tables against the specs."""

    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self._validators = {}
        self._resources = None

    def schema(self, schema_id):
        try:
            return _schema(self.root, schema_id)
        except ConfigurationError as error:
            raise StorageError(f'contract schema {schema_id}: {error}') from error

    def _registry(self):
        """Every contract schema by its $id, so a cross-file $ref (eval_scenario -> constraint_ledger) resolves."""
        if self._resources is None:
            resources = []
            for path in sorted((self.root / 'contracts' / 'schemas').glob('*.schema.json')):
                document = self.schema(path.name.removesuffix('.schema.json'))
                resources.append((document.get('$id', path.name), Resource.from_contents(document)))
            self._resources = Registry().with_resources(resources)
        return self._resources

    def _validator(self, schema_id, pointer=''):
        key = (schema_id, pointer)
        if key not in self._validators:
            document = self.schema(schema_id)
            node = _pointer(document, pointer) if pointer else document
            # Subschemas resolve local $defs against the whole document.
            if pointer and '$defs' in document:
                node = {**node, '$defs': document['$defs']}
            self._validators[key] = Draft202012Validator(node, registry=self._registry())
        return self._validators[key]

    def enum(self, source):
        if isinstance(source, Ref):
            return tuple(_pointer(self.schema(source.schema_id), source.pointer))
        return source

    def columns(self, spec):
        if spec.document is None:
            return spec.columns
        document = self.schema(spec.document)
        required = set(document.get('required', ()))
        result = []
        for name, node in document['properties'].items():
            scalar = node.get('type') == 'string' or ('enum' in node and all(isinstance(v, str) for v in node['enum']))
            result.append(Col(name, S if scalar else JSON, name not in required))
        return tuple(result)

    def arrow_schema(self, name, dim=None):
        spec = _spec(name)
        fields = [pa.field(col.name, S if col.type == JSON else col.type, col.nullable) for col in self.columns(spec)]
        if spec.vector:
            if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
                raise StorageError(f'{name}: vector tables need a positive integer dim, got {dim!r}')
            fields.append(pa.field('vector', pa.list_(pa.field('element', pa.float32()), dim), False))
        return pa.schema(fields, metadata={'pe.table': name, 'pe.physical_version': str(PHYSICAL_VERSION)})

    def build(self, name, rows, dim=None):
        """Build a validated table from Python rows; JSON columns take Python values."""
        spec = _spec(name)
        columns = self.columns(spec)
        names = {col.name for col in columns} | ({'vector'} if spec.vector else set())
        prepared = []
        for index, row in enumerate(rows):
            unknown = set(row) - names
            if unknown:
                raise StorageError(f'{name}: row {index}: unknown column {sorted(unknown)[0]!r}')
            out = dict(row)
            for col in columns:
                if col.type == JSON and col.name in out and not (col.nullable and out[col.name] is None):
                    out[col.name] = json.dumps(out[col.name], sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=False, allow_nan=False)
            prepared.append(out)
        try:
            table = pa.Table.from_pylist(prepared, schema=self.arrow_schema(name, dim))
        except (pa.ArrowException, TypeError, ValueError) as error:
            raise StorageError(f'{name}: rows do not match the physical schema: {error}') from error
        self.validate(name, table)
        return table

    def rows(self, name, table):
        """Python rows with JSON columns decoded."""
        json_columns = [col.name for col in self.columns(_spec(name)) if col.type == JSON]
        result = table.to_pylist()
        for row in result:
            for column in json_columns:
                if row[column] is not None:
                    row[column] = json.loads(row[column])
        return result

    def validate(self, name, table):
        """Refuse a malformed schema, nulls in required columns, bad enums or JSON, and duplicate keys."""
        spec = _spec(name)
        dim = None
        if spec.vector:
            vector = table.schema.field('vector').type if 'vector' in table.schema.names else None
            if not (isinstance(vector, pa.FixedSizeListType) and vector.value_type == pa.float32()):
                raise StorageError(f'{name}: malformed schema: vector must be fixed_size_list<float32>')
            dim = vector.list_size
        expected = self.arrow_schema(name, dim)
        if not table.schema.equals(expected, check_metadata=False):
            raise StorageError(f'{name}: malformed schema: expected {expected.remove_metadata()}, got '
                               f'{table.schema.remove_metadata()}')
        check_tags(table.schema, name)
        for column in expected:
            if not column.nullable and table.column(column.name).null_count:
                raise StorageError(f'{name}: column {column.name!r} holds a null')
        columns = self.columns(spec)
        rows = table.to_pylist()
        enums = {col.name: set(self.enum(col.enum)) for col in columns if col.enum is not None}
        seen = set()
        for index, row in enumerate(rows):
            where = f'{name}: row {index}'
            for column, allowed in enums.items():
                if row[column] is not None and row[column] not in allowed:
                    raise StorageError(f'{where}: {column}={row[column]!r} is not one of {sorted(allowed)}')
            decoded = {}
            for col in columns:
                if col.type == JSON and row[col.name] is not None:
                    try:
                        decoded[col.name] = json.loads(row[col.name])
                    except ValueError as error:
                        raise StorageError(f'{where}: {col.name} is not JSON: {error}') from error
                    if col.subschema is not None:
                        self._check(self._validator(col.subschema.schema_id, col.subschema.pointer), decoded[col.name], f'{where}: {col.name}')
            if spec.document is not None:
                document = {key: decoded.get(key, value) for key, value in row.items() if value is not None}
                self._check(self._validator(spec.document), document, where)
            key = tuple(row[column] for column in spec.key)
            if key in seen:
                raise StorageError(f'{where}: duplicate key {dict(zip(spec.key, key))}')
            seen.add(key)
            _ROW_CHECKS.get(name, _no_check)(row, where, dim)
        if spec.vector and len({row['embedder_revision'] for row in rows}) > 1:
            raise StorageError(f'{name}: one file holds exactly one embedder_revision (revision isolation)')

    @staticmethod
    def _check(validator, value, where):
        error = next(iter(validator.iter_errors(value)), None)
        if error is not None:
            path = '.'.join(str(part) for part in error.absolute_path) or 'root'
            raise StorageError(f'{where}: {path}: {error.message}')


def check_tags(schema, name):
    """Both tags are required: a file without them is not assumed to be this table or this version (F10)."""
    metadata = schema.metadata or {}
    tagged = metadata.get(b'pe.table')
    if tagged != name.encode():
        raise StorageError(f'{name}: file is tagged as table {tagged.decode() if tagged else None!r}')
    version = metadata.get(b'pe.physical_version')
    if version != str(PHYSICAL_VERSION).encode():
        raise StorageError(f'{name}: unsupported physical version {version.decode() if version else None!r}')


def check_embedding_space(tables):
    """One embedding space per snapshot (docs/stages/K1-embed.md:9): one revision and one dimension across both
    vector tables, so text and image vectors are comparable (F6)."""
    revisions, dims = set(), set()
    for name in ('image_embeddings', 'concept_text_embeddings'):
        if name in tables:
            dims.add(tables[name].schema.field('vector').type.list_size)
            revisions.update(tables[name].column('embedder_revision').to_pylist())
    if len(revisions) > 1 or len(dims) > 1:
        raise StorageError(f'snapshot mixes embedding spaces: revisions {sorted(revisions)}, dims {sorted(dims)}')


def _spec(name):
    if name not in SPECS:
        raise StorageError(f'unknown table {name!r}')
    return SPECS[name]


def _no_check(row, where, dim):
    return None


def _concept(row, where, dim=None):
    if not is_concept_id(row['concept_id']):
        raise StorageError(f'{where}: invalid concept_id {row["concept_id"]!r}')


def _image(row, where, dim=None):
    if not is_image_id(row['image_id']):
        raise StorageError(f'{where}: invalid image_id {row["image_id"]!r}')


def _folder(row, where, dim=None):
    try:
        check_folder_id(row['folder_id'])
    except StorageError as error:
        raise StorageError(f'{where}: {error}') from error


def _concepts(row, where, dim):
    name = row['normalised_name']
    if normalise_name(name) != name or concept_id(name) != row['concept_id']:
        raise StorageError(f'{where}: concept_id and normalised_name disagree with the identity contract')


def _vector(row, where, dim):
    if row['dim'] != dim:
        raise StorageError(f'{where}: dim={row["dim"]} but the vector holds {dim} values')
    if any(not math.isfinite(value) for value in row['vector']):
        raise StorageError(f'{where}: vector holds a non-finite value')


def _non_negative(*columns):
    def check(row, where, dim):
        for column in columns:
            if row[column] is not None and row[column] < 0:
                raise StorageError(f'{where}: {column} must not be negative')
    return check


def _facet(row, where, dim):
    try:
        check_artifact_key(row['context_key'])
    except StorageError as error:
        raise StorageError(f'{where}: context_key: {error}') from error
    if not math.isfinite(row['quality']):
        raise StorageError(f'{where}: quality must be finite')
    if not row['facet'].startswith(row['facet_group'] + '.') or row['facet'] == row['facet_group'] + '.':
        raise StorageError(f'{where}: facet {row["facet"]!r} is not <facet_group>.<facet>')


def _all(*checks):
    def check(row, where, dim):
        for item in checks:
            item(row, where, dim)
    return check


_ROW_CHECKS = {
    'folders': _folder,
    'concepts': _all(_concept, _concepts),
    'folder_concepts': _all(_folder, _concept, _non_negative('position')),
    'images': _all(_image, _non_negative('size_bytes', 'width', 'height')),
    'folder_images': _all(_folder, _image),
    'image_embeddings': _all(_image, _vector),
    'concept_text_embeddings': _all(_concept, _vector),
    'observations': _all(_image, _concept, _facet),
    'cards': _concept,
    'aliases': _concept,
}


def check_references(tables):
    """Refuse a dangling reference between tables of one snapshot (or one eval set)."""
    for name, table in tables.items():
        for columns, target, target_columns in _spec(name).references:
            if target not in tables:
                raise StorageError(f'{name}: references table {target!r}, which the set does not hold')
            known = set(zip(*(tables[target].column(column).to_pylist() for column in target_columns)))
            values = zip(*(table.column(column).to_pylist() for column in columns))
            for index, value in enumerate(values):
                if value not in known:
                    raise StorageError(f'{name}: row {index}: dangling reference {dict(zip(columns, value))} '
                                       f'to {target}')
