"""K5 immutable, derived snapshot index. CPU only; no source KB or model access (R-016/R-017)."""

from collections import Counter, defaultdict
import hashlib
import inspect
import json
from pathlib import Path
import sys
import unicodedata

from jsonschema import Draft202012Validator
import numpy as np
import pyarrow as pa

from .config import ConfigurationError, _EXACT_INTEGERS, validate_index_reads
from .k1_embed import text_hash
from .storage import SPECS, StorageError, artifact_key, check_references, normalise_name
from .storage._fs import read_table_file, sha256_file
from .storage.identity import canonical_json
from .storage.tables import check_embedding_space


# Artifact/protocol-bound layout and file names; no content text selects a path.
LAYOUT = 1
SCHEMA = 'serving_index.v0'
MANIFEST = 'index/manifest.arrow'
INPUTS = ('concepts', 'folders', 'folder_concepts', 'images', 'aliases', 'cards',
          'concept_text_embeddings', 'image_embeddings')
VECTOR_TABLES = ('concept_text_embeddings', 'image_embeddings')
# Safety-bound accumulation: finite float32 inputs can overflow float32 dot products/norms.
SCORE_DTYPE = np.float64
LOOKUP_SCHEMA = pa.schema([
    pa.field('token', pa.large_string(), False), pa.field('concept_id', pa.large_string(), False),
    pa.field('source_table', pa.large_string(), False), pa.field('source_key', pa.large_string(), False),
], metadata={'pe.index_layout': str(LAYOUT)})
DOCUMENT_SCHEMA = pa.schema([pa.field('document', pa.large_string(), False)],
                            metadata={'pe.index_layout': str(LAYOUT)})


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _encode(table):
    with pa.BufferOutputStream() as stream:
        with pa.ipc.new_file(stream, table.schema) as writer:
            writer.write_table(table)
        return stream.getvalue().to_pybytes()


def _decode(data):
    try:
        with pa.BufferReader(data) as source, pa.ipc.open_file(source) as reader:
            return reader.read_all()
    except (pa.ArrowException, ValueError, OSError) as error:
        raise StorageError('K5: invalid Arrow index file') from error


def _document(value):
    return _encode(pa.Table.from_pylist([{'document': canonical_json(value)}], schema=DOCUMENT_SCHEMA))


def _parse_document(data, catalog):
    table = _decode(data)
    if not table.schema.equals(DOCUMENT_SCHEMA, check_metadata=True) or table.num_rows != 1:
        raise StorageError('K5: invalid metadata Arrow schema')
    try:
        value = json.loads(table.column('document')[0].as_py())
        # Strict serialization also refuses nonfinite numbers and invalid Unicode.
        canonical_json(value).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as error:
        raise StorageError('K5: invalid metadata document') from error
    if (not _EXACT_INTEGERS(catalog.schema(SCHEMA)).is_valid(value)
            or type(value['layout']) is not int):
        raise StorageError('K5: invalid metadata contract')
    return value


def _order(table, name):
    columns = [table.column(key).to_pylist() for key in SPECS[name].key]
    return sorted(range(table.num_rows), key=lambda i: tuple(column[i] for column in columns))


def _batches(table, indices, block_rows):
    for start in range(0, len(indices), block_rows):
        yield table.take(pa.array(indices[start:start + block_rows], type=pa.int64()))


def _source_hash(table, name, block_rows):
    result = hashlib.sha256()
    result.update(str(table.schema).encode('utf-8'))
    for batch in _batches(table, _order(table, name), block_rows):
        for row in batch.to_pylist():
            result.update(canonical_json(row).encode('utf-8'))
            result.update(b'\n')  # protocol-bound record separator after complete JSON objects
    return result.hexdigest()


def _tools(catalog):
    return {'numpy': np.__version__, 'arrow': pa.__version__,
            'unicode': unicodedata.unidata_version,
            'code': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'normalization': hashlib.sha256(inspect.getsource(normalise_name).encode('utf-8')).hexdigest(),
            'schema': _digest(catalog.schema(SCHEMA))}


def _metadata(tables, settings, catalog):
    sources = {name: _source_hash(tables[name], name, settings['build']['block_rows']) for name in INPUTS}
    revisions = set()
    for name in VECTOR_TABLES:
        revisions.update(tables[name].column('embedder_revision').to_pylist())
    embedding = {'revision': next(iter(revisions), None),
                 'dimension': tables[VECTOR_TABLES[0]].schema.field('vector').type.list_size}
    value = {'layout': LAYOUT, 'settings': settings, 'sources': sources, 'tools': _tools(catalog),
             'embedding': embedding}
    return dict(value, key=_digest(value), parts=[])


def _checked_tables(tables, catalog):
    if set(tables) != set(INPUTS):
        raise StorageError('K5: source table inventory differs')
    for name, table in tables.items():
        catalog.validate(name, table)
    check_references(tables)
    check_embedding_space(tables)
    for row in catalog.rows('concepts', tables['concepts']):
        if normalise_name(row['display_name']) != row['normalised_name']:
            raise StorageError('K5: display name disagrees with concept identity')
    validator = Draft202012Validator(catalog.schema('alias_set.v0'))
    for row in catalog.rows('aliases', tables['aliases']):
        document = {'concept_id': row['concept_id'], 'revisions': row['revisions'], 'aliases': [{
            'text': row['alias_text'], **{field: row[field] for field in
                                        ('origin', 'status', 'language', 'collides_with')}}]}
        if not validator.is_valid(document):
            raise StorageError('K5: invalid singleton alias provenance')
        if row['status'] == 'accepted' and row['collides_with']:
            raise StorageError('K5: accepted alias has collisions')


def character_ngrams(text, n):
    """Distinct code-point windows, shared normalization; no padding or shorter fallback."""
    if type(n) is not int or not 0 < n <= sys.maxsize:
        raise StorageError('K5: ngram_size must be a positive native integer')
    try:
        text.encode('utf-8')
        value = normalise_name(text)
    except (AttributeError, TypeError, UnicodeError) as error:
        raise StorageError('K5: text must be a valid Unicode string') from error
    return {value[i:i + n] for i in range(len(value) - n + 1)}


def _lookups(tables, settings, catalog):
    exact, lexical, accepted = set(), set(), set()

    def add(text, concept, table, key, is_exact):
        # Text stays data, including instruction-like strings and path delimiters.
        text.encode('utf-8')
        source = (concept, table, canonical_json(key))
        if is_exact:
            exact.add((normalise_name(text), *source))
        for gram in character_ngrams(text, settings['lexical']['ngram_size']):
            lexical.add((gram, *source))

    for row in catalog.rows('concepts', tables['concepts']):
        add(row['display_name'], row['concept_id'], 'concepts', [row['concept_id']], True)
    for row in catalog.rows('folder_concepts', tables['folder_concepts']):
        if row['description'] is not None:
            add(row['description'], row['concept_id'], 'folder_concepts',
                [row['folder_id'], row['concept_id']], False)
    for row in catalog.rows('aliases', tables['aliases']):
        if row['status'] == 'accepted':
            add(row['alias_text'], row['concept_id'], 'aliases', [row['concept_id'], row['alias_text']], True)
            accepted.add((row['concept_id'], text_hash(row['alias_text'])))

    def table(values):
        return pa.Table.from_pylist([dict(zip(LOOKUP_SCHEMA.names, row)) for row in sorted(values)],
                                   schema=LOOKUP_SCHEMA)
    return table(exact), table(lexical), accepted


def _eligible(table, name, accepted):
    indices = _order(table, name)
    if name == 'concept_text_embeddings':
        kinds, concepts, hashes = [table.column(key).to_pylist() for key in ('text_kind', 'concept_id', 'text_hash')]
        indices = [i for i in indices if kinds[i] != 'alias' or (concepts[i], hashes[i]) in accepted]
    return indices


def _allocation_bound(rows, dimension):
    # Safety-bound byte arithmetic before native allocation; no configured size is silently reduced.
    if rows > sys.maxsize // SCORE_DTYPE().itemsize // dimension:
        raise StorageError('K5: block_rows * dimension exceeds native allocation size')


def _matrix(table, metric):
    dimension = table.schema.field('vector').type.list_size
    _allocation_bound(table.num_rows, dimension)
    values = np.asarray(table.column('vector').to_pylist(), dtype=SCORE_DTYPE).reshape((-1, dimension))
    if not np.isfinite(values).all():
        raise StorageError('K5: nonfinite vector')
    if metric == 'cosine':
        norms = np.sqrt(np.sum(values * values, axis=1))
        if np.any(norms == 0):
            raise StorageError('K5: cosine requires nonzero vectors')
        values /= norms[:, None]
    return values


def _parts(tables, settings, catalog):
    exact, lexical, accepted = _lookups(tables, settings, catalog)
    yield 'index/exact.arrow', 'exact', exact
    yield 'index/lexical.arrow', 'lexical', lexical
    block_rows = settings['build']['block_rows']
    for name, prefix in zip(VECTOR_TABLES, ('text', 'image')):
        table = tables[name]
        _allocation_bound(block_rows, table.schema.field('vector').type.list_size)
        for ordinal, batch in enumerate(_batches(table, _eligible(table, name, accepted), block_rows)):
            _matrix(batch, settings['metric'])  # validate zero-norm policy before committing a shard
            yield f'index/{prefix}-{ordinal}.arrow', name, batch


def _building_tables(builder):
    builder.writer.require()
    if builder not in builder.writer._builders:
        raise StorageError('K5: builder is no longer active')
    entries = {row['table']: row for row in builder.entries if row['kind'] == 'table'}
    if any(name not in entries for name in INPUTS):
        raise StorageError('K5: required snapshot tables are missing')
    tables = {}
    for name in INPUTS:
        entry = entries[name]
        path = builder.store.path(f'{builder.relative}/{entry["path"]}')
        if path.stat().st_size != entry['size_bytes'] or sha256_file(path) != entry['sha256']:
            raise StorageError('K5: source snapshot checksum differs')
        tables[name] = read_table_file(path)
    return tables


def _building_reader(builder):
    # Index once per validation pass, rather than scan all snapshot entries for every shard.
    entries = {row['path']: row for row in builder.entries if row['kind'] == 'file'}

    def read(relative):
        entry = entries.get(relative)
        if entry is None:
            raise StorageError('K5: index file is missing from snapshot')
        data = builder.store.path(f'{builder.relative}/{relative}').read_bytes()
        if len(data) != entry['size_bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise StorageError('K5: index snapshot checksum differs')
        return data
    return read


def _validate(metadata, read_file, tables, catalog, expected):
    if {key: metadata[key] for key in expected if key != 'parts'} != {
            key: expected[key] for key in expected if key != 'parts'}:
        raise StorageError('K5: index input/settings/tool fingerprint differs')
    expected_parts = _parts(tables, metadata['settings'], catalog)
    count = 0
    exact = None
    for path, kind, expected_table in expected_parts:
        if count >= len(metadata['parts']):
            raise StorageError('K5: index coverage is incomplete')
        entry = metadata['parts'][count]
        if (entry['path'], entry['kind'], entry['rows']) != (path, kind, expected_table.num_rows):
            raise StorageError('K5: index part inventory differs')
        data = read_file(path)
        if len(data) != entry['size_bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise StorageError('K5: index part checksum differs')
        actual = _decode(data)
        if not actual.equals(expected_table, check_metadata=True):
            raise StorageError('K5: index entries/coverage disagree with snapshot rows')
        if kind == 'exact':
            exact = defaultdict(set)
            for row in actual.to_pylist():
                exact[row['token']].add(row['concept_id'])
        count += 1
    if count != len(metadata['parts']):
        raise StorageError('K5: unexpected index part')
    for row in tables['concepts'].to_pylist():
        if row['concept_id'] not in exact[normalise_name(row['display_name'])]:
            raise StorageError('K5: own-name lookup failed')


def build_index(builder, config):
    """Attach a validated component to an active builder; never finish or publish it.

    A partial attachment requires a fresh builder. Per-unit verified checkpoints survive that restart.
    """
    settings = validate_index_reads(config, project_root=builder.store.catalog.root)
    tables = _building_tables(builder)
    catalog = builder.store.catalog
    _checked_tables(tables, catalog)
    for name in VECTOR_TABLES:
        _allocation_bound(settings['build']['block_rows'], tables[name].schema.field('vector').type.list_size)
    metadata = _metadata(tables, settings, catalog)
    existing = [row['path'] for row in builder.entries if row['kind'] == 'file' and row['path'].startswith('index/')]
    if existing:
        if MANIFEST not in existing:
            raise StorageError('K5: partial attachment; retry with a fresh snapshot builder')
        read_file = _building_reader(builder)
        saved = _parse_document(read_file(MANIFEST), catalog)
        _validate(saved, read_file, tables, catalog, metadata)
        artifacts = {part['artifact'] for part in saved['parts']}
        artifacts.update(row['path'] for row in builder.entries if row['kind'] == 'artifact'
                         and row['path'].startswith('artifacts/k5_complete/'))
        return {'key': saved['key'], 'manifest': MANIFEST, 'validated': True, 'reused': len(saved['parts']),
                'built': 0, 'artifacts': sorted(artifacts)}
    checkpoints = builder.writer.checkpoints('k5_index')
    revisions = {'model': _digest(metadata['embedding']),
                 'prompt': _digest({'stage': 'k5', 'model_calls': False}),
                 'schema': _digest({'layout': LAYOUT, 'tools': metadata['tools'], 'settings': settings})}
    built, reused = 0, 0
    # Attach the manifest last. Any failed unit leaves the current pointer untouched.
    for path, kind, table in _parts(tables, settings, catalog):
        data = _encode(table)
        key = artifact_key('k5_index', _digest({'input': metadata['key'], 'path': path}), revisions)
        previous = checkpoints.done(key) or checkpoints.reusable(key)
        if previous:
            cached = checkpoints.load(key)
            if cached != data:
                raise StorageError('K5: cached unit disagrees with current snapshot')
            artifact = previous['output_key']
            reused += 1
        else:
            try:
                artifact = checkpoints.commit(key, 'k5_index', data, revisions=revisions)
            except (StorageError, OSError):
                checkpoints.fail(key, 'K5_UNIT_FAILED', revisions=revisions)
                raise
            built += 1
        builder.reference_artifacts([artifact])
        builder.add_file(path, data)
        metadata['parts'].append({'path': path, 'kind': kind, 'rows': table.num_rows,
                                  'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
                                  'artifact': artifact})
    # Validate saved units before the complete checkpoint exists.
    _validate(metadata, _building_reader(builder), tables, catalog, metadata)
    data = _document(metadata)
    key = artifact_key('k5_complete', metadata['key'], revisions)
    completed = builder.writer.checkpoints('k5_complete')
    previous = completed.done(key) or completed.reusable(key)
    if previous:
        if completed.load(key) != data:
            raise StorageError('K5: cached completion disagrees with current snapshot')
        artifact = previous['output_key']
    else:
        artifact = completed.commit(key, 'k5_complete', data, revisions=revisions)
    builder.reference_artifacts([artifact])
    builder.add_file(MANIFEST, data)
    read_file = _building_reader(builder)
    saved = _parse_document(read_file(MANIFEST), catalog)
    _validate(saved, read_file, tables, catalog, metadata)
    return {'key': metadata['key'], 'manifest': MANIFEST, 'validated': True, 'built': built, 'reused': reused,
            'artifacts': sorted({artifact, *(part['artifact'] for part in metadata['parts'])})}


class ServingIndex:
    """Read-only, explicitly constructed by retrieval callers; the caller owns the snapshot pin."""

    def __init__(self, snapshot, config):
        self.settings = validate_index_reads(config, project_root=snapshot.store.catalog.root)
        catalog = snapshot.store.catalog
        self._read_file = snapshot.read_file
        tables = {name: snapshot.read_table(name) for name in INPUTS}
        _checked_tables(tables, catalog)
        self.metadata = _parse_document(self._read_file(MANIFEST), catalog)
        try:
            saved_settings = validate_index_reads({'index': self.metadata['settings']}, project_root=catalog.root)
        except ConfigurationError as error:
            raise StorageError('K5: invalid saved index settings') from error
        # Layout belongs to the immutable snapshot; other settings must still match the profile.
        self.settings['build'] = saved_settings['build']
        expected = _metadata(tables, self.settings, catalog)
        _validate(self.metadata, self._read_file, tables, catalog, expected)
        self._exact = defaultdict(set)
        self._lexical = defaultdict(set)
        for row in _decode(self._read_file('index/exact.arrow')).to_pylist():
            self._exact[row['token']].add(row['concept_id'])
        for row in _decode(self._read_file('index/lexical.arrow')).to_pylist():
            self._lexical[row['token']].add(row['concept_id'])

    def exact(self, text):
        character_ngrams(text, self.settings['lexical']['ngram_size'])  # validates Unicode, never a content filter
        return sorted(self._exact.get(normalise_name(text), ()))

    def lexical(self, text):
        counts = Counter(concept for gram in character_ngrams(text, self.settings['lexical']['ngram_size'])
                         for concept in self._lexical.get(gram, ()))
        return [{'concept_id': concept, 'matched_grams': count}
                for concept, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    def search(self, table, vector, *, k, embedder_revision, dimension):
        if table not in VECTOR_TABLES:
            raise StorageError('K5: unknown vector table')
        if type(k) is not int or not 0 < k <= sys.maxsize:
            raise StorageError('K5: k must be a positive native integer')
        space = self.metadata['embedding']
        if (type(dimension) is not int or dimension != space['dimension']
                or embedder_revision != space['revision']):
            raise StorageError('K5: query embedding space differs')
        if (not isinstance(vector, (list, tuple)) or len(vector) != dimension
                or any(type(value) not in (int, float) for value in vector)):
            raise StorageError('K5: invalid query vector')
        try:
            query = np.asarray(vector, dtype=SCORE_DTYPE)
        except (OverflowError, ValueError) as error:
            raise StorageError('K5: invalid query numeric value') from error
        if not np.isfinite(query).all():
            raise StorageError('K5: nonfinite query vector')
        # Scale before normalizing to avoid overflow for finite float64 query coordinates.
        if self.settings['metric'] == 'cosine':
            scale = np.max(np.abs(query))
            if scale == 0:
                raise StorageError('K5: cosine requires nonzero query')
            query = query / scale
            query /= np.sqrt(np.sum(query * query))
        retained_scores = np.empty(0, dtype=SCORE_DTYPE)
        retained_keys = []
        for part in self.metadata['parts']:
            if part['kind'] != table:
                continue
            batch = _decode(self._read_file(part['path']))
            keys = list(zip(*(batch.column(key).to_pylist() for key in SPECS[table].key)))
            with np.errstate(over='ignore', invalid='ignore'):
                scores = _matrix(batch, self.settings['metric']) @ query
            if not np.isfinite(scores).all():
                raise StorageError('K5: computed nonfinite score')
            # Rows within each shard are already in stable primary-key order.
            selected = np.lexsort((np.arange(len(keys)), -scores))[:k]
            merged_keys = retained_keys + [keys[i] for i in selected]
            merged_scores = np.concat((retained_scores, scores[selected]))
            ordered = sorted(range(len(merged_keys)), key=lambda i: (-float(merged_scores[i]), merged_keys[i]))[:k]
            retained_keys = [merged_keys[i] for i in ordered]
            retained_scores = merged_scores[ordered]
        return [{'key': dict(zip(SPECS[table].key, key)), 'score': float(score)}
                for key, score in zip(retained_keys, retained_scores)]
