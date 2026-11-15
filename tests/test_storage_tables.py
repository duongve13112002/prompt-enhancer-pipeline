"""Round trip and refusals of the 16 physical tables (R-016)."""

from copy import deepcopy

import pyarrow as pa
import pytest
from storage_fixtures import DIM, T0, data_rows, dataset

from prompt_enhancer.storage import SPECS, Catalog, StorageError, artifact_key, check_references, concept_id, image_id
from prompt_enhancer.storage._anchored import write_table_file
from prompt_enhancer.storage._fs import SUFFIX, read_table_file


@pytest.fixture
def catalog(project_root):
    return Catalog(project_root)


def all_rows():
    rows = dataset('t')
    rows.update(data_rows())
    rows['sync_state'] = [{
        'stage': 'K2', 'input_key': 'a' * 64, 'output_key': 'artifacts/observation/aa/' + 'a' * 64 + '.parquet',
        'output_size': 10, 'output_sha256': 'b' * 64, 'state': 'done', 'model_revision': 'obs-r1',
        'prompt_revision': 'observer_v0', 'schema_revision': 'f1',
        'call_records': [{'role': 'observer', 'request_hash': 'c' * 64, 'latency_ms': 9.5}], 'error': None,
        'run_id': 'run', 'recorded_at': T0}]
    rows['snapshots'] = [{'snapshot_id': 's20261115T120000000000Z-0123abcd', 'parent_snapshot_id': None,
                          'state': 'published', 'created_at': T0, 'published_at': T0, 'manifest_sha256': 'd' * 64,
                          'source_scan_summary': {'folders': 3, 'note': 'quét'}}]
    return rows


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
def test_round_trip_every_table(catalog, tmp_path, fmt):
    rows = all_rows()
    assert set(rows) == set(SPECS) and len(SPECS) == 16
    for name, expected in rows.items():
        table = catalog.build(name, expected, DIM if SPECS[name].vector else None)
        path = tmp_path.resolve() / f'{name}{SUFFIX[fmt]}'
        write_table_file(path.parent, path.name, table, fmt)
        back = read_table_file(path)
        catalog.validate(name, back)
        assert back.schema.equals(table.schema, check_metadata=True), name
        decoded = catalog.rows(name, back)
        for row in expected:  # every column, Unicode and provenance included, comes back unchanged
            for column in catalog.arrow_schema(name, DIM if SPECS[name].vector else None).names:
                row.setdefault(column, None)
        assert decoded == expected, name


def test_shared_image_and_one_concept_across_folders(catalog):
    rows = dataset('t')
    shared = image_id(b'shared image bytes')
    assert [row['image_id'] for row in rows['images']].count(shared) == 1
    assert [row['image_id'] for row in rows['folder_images']].count(shared) == 2
    kimono = concept_id('kimono')
    assert concept_id('Kimono') == kimono and [row['concept_id'] for row in rows['concepts']].count(kimono) == 1
    tables = {name: catalog.build(name, value, DIM if SPECS[name].vector else None) for name, value in rows.items()}
    check_references(tables)


def test_vector_dimension_and_revision_isolation(catalog):
    four = dataset('t', dim=4, revision='emb-a')['image_embeddings']
    eight = dataset('t', dim=8, revision='emb-b')['image_embeddings']
    assert catalog.build('image_embeddings', four, 4).schema.field('vector').type.list_size == 4
    assert catalog.build('image_embeddings', eight, 8).schema.field('vector').type.list_size == 8
    with pytest.raises(StorageError, match='revision isolation'):
        catalog.build('image_embeddings', four + [dict(eight[0], vector=eight[0]['vector'][:4], dim=4)], 4)
    with pytest.raises(StorageError, match='dim=8 but the vector holds 4'):
        catalog.build('image_embeddings', [dict(four[0], dim=8)], 4)
    with pytest.raises(StorageError, match='positive integer dim'):
        catalog.build('image_embeddings', four, None)
    revisions = {'model': 'emb-a', 'prompt': 'none', 'schema': 'v1'}
    assert artifact_key('image_embedding', four[0]['image_id'], revisions) != artifact_key(
        'image_embedding', four[0]['image_id'], dict(revisions, model='emb-b'))


def _replace_column(table, name, array):
    index = table.schema.get_field_index(name)
    return table.set_column(index, pa.field(name, array.type, table.schema.field(name).nullable), array)


def test_malformed_schema_refused(catalog):
    table = catalog.build('folders', dataset('t')['folders'])
    wrong_type = _replace_column(table, 'error', pa.array([1] * table.num_rows, pa.int64()))
    with pytest.raises(StorageError, match='malformed schema'):
        catalog.validate('folders', wrong_type)
    with pytest.raises(StorageError, match='malformed schema'):
        catalog.validate('folders', table.drop_columns(['error']))
    renamed = table.replace_schema_metadata({'pe.table': 'images', 'pe.physical_version': '1'})
    with pytest.raises(StorageError, match="tagged as table 'images'"):
        catalog.validate('folders', renamed)
    with pytest.raises(StorageError, match='unsupported physical version'):
        catalog.validate('folders', table.replace_schema_metadata({'pe.table': 'folders', 'pe.physical_version': '9'}))
    vectors = catalog.build('image_embeddings', dataset('t')['image_embeddings'], DIM)
    as_list = _replace_column(vectors, 'vector', pa.array(vectors.column('vector').to_pylist(), pa.list_(pa.float32())))
    with pytest.raises(StorageError, match='fixed_size_list'):
        catalog.validate('image_embeddings', as_list)


def test_duplicate_key_refused(catalog):
    rows = dataset('t')['folders']
    with pytest.raises(StorageError, match='duplicate key'):
        catalog.build('folders', rows + [rows[0]])
    traces = data_rows()['request_traces']
    with pytest.raises(StorageError, match='duplicate key'):
        catalog.build('request_traces', traces + traces)


def test_dangling_reference_refused(catalog):
    rows = dataset('t')
    rows['folder_images'].append(dict(rows['folder_images'][0], relative_path='x.png', image_id=image_id(b'none')))
    tables = {name: catalog.build(name, value, DIM if SPECS[name].vector else None) for name, value in rows.items()}
    with pytest.raises(StorageError, match='dangling reference'):
        check_references(tables)
    del tables['images']
    with pytest.raises(StorageError, match="does not hold"):
        check_references(tables)


@pytest.mark.parametrize('name, change, message', [
    ('aliases', {'origin': 'curated'}, 'not one of'),
    ('observations', {'region': {'x': 2, 'y': 0, 'width': 0.5, 'height': 0.5}}, 'region'),
    ('observations', {'facet': 'colour'}, 'facet_group'),
    ('concepts', {'normalised_name': 'Kimono'}, 'identity contract'),
    ('concepts', {'concept_id': 'concept:abc'}, 'invalid concept_id'),
    ('images', {'image_id': 'md5:00'}, 'invalid image_id'),
    ('folders', {'folder_id': 'a/../b'}, 'invalid folder_id'),
    ('folders', {'folder_id': 'a\\b'}, 'invalid folder_id'),
    ('folders', {'error': None}, 'holds a null'),
    ('folders', {'surprise': 1}, 'unknown column'),
    ('folder_concepts', {'position': -1}, 'must not be negative'),
    ('cards', {'kind': {'value': 'clothing'}}, 'origin'),
    ('image_embeddings', {'vector': [float('nan')] * DIM}, 'non-finite'),
])
def test_row_refusals(catalog, name, change, message):
    rows = deepcopy(dataset('t')[name])
    rows[0].update(change)
    with pytest.raises(StorageError, match=message):
        catalog.build(name, rows, DIM if SPECS[name].vector else None)


def test_document_and_json_columns_validated_on_read(catalog, tmp_path):
    table = catalog.build('eval_scenarios', data_rows()['eval_scenarios'])
    index = table.schema.get_field_index('generator')
    broken = table.set_column(index, table.schema.field('generator'), pa.array(['{"role":"writer"}'], pa.large_string()))
    path = tmp_path.resolve() / 'eval_scenarios.parquet'
    write_table_file(path.parent, path.name, broken, 'parquet')
    with pytest.raises(StorageError, match='generator'):
        catalog.validate('eval_scenarios', read_table_file(path))
    not_json = table.set_column(index, table.schema.field('generator'), pa.array(['{'], pa.large_string()))
    with pytest.raises(StorageError, match='not JSON'):
        catalog.validate('eval_scenarios', not_json)
