"""Bounded CPU properties: independent scalar ranking, snapshot references and code-point windows."""

import math
from pathlib import Path
import unicodedata

from hypothesis import example, given, settings, strategies as st
import numpy as np
import pyarrow as pa
import pytest

from k5_fixtures import config
from prompt_enhancer import k5_index
from prompt_enhancer.k5_index import SCORE_DTYPE, ServingIndex, _decode, _encode, _lookups, _matrix, character_ngrams
from prompt_enhancer.k5_index import _document, _parse_document
from prompt_enhancer.storage import Catalog, SPECS, StorageError, concept_id, image_id
from storage_fixtures import dataset


ROOT = Path(__file__).resolve().parents[1]


@settings(max_examples=45, deadline=None)
# K5-TIE: the example Hypothesis found; a 2-row shard scored [0, 2, 2] as -2.2e-17 while another shard scored 0.0.
@example(vectors=[[0, 0, 0]] * 4 + [[0, 0, 1], [0, 2, 2], [0, 2, 2]], query=[0, 1, -1], block=2, k=5,
         metric='cosine', name='image_embeddings')
@given(st.one_of(st.lists(st.lists(st.integers(-4, 4), min_size=3, max_size=3), max_size=12),
                 st.lists(st.sampled_from([[1, 0, 0], [0, 1, 0]]), min_size=3, max_size=12)),
       st.lists(st.integers(-4, 4), min_size=3, max_size=3), st.integers(1, 7), st.integers(1, 18),
       st.sampled_from(['cosine', 'inner_product']), st.sampled_from(['image_embeddings', 'concept_text_embeddings']))
def test_exact_search_matches_scalar_reference(vectors, query, block, k, metric, name):
    if metric == 'cosine':
        vectors = [v if any(v) else [1, 0, 0] for v in vectors]
        query = query if any(query) else [1, 0, 0]
    rows = []
    for i, vector in enumerate(vectors):
        row = {'embedder_revision': 'test-space', 'dim': 3, 'vector': vector}
        if name == 'image_embeddings':
            row['image_id'] = image_id(str(i).encode())
        else:
            row.update(concept_id=concept_id('test'), text_kind='name', text_hash=f'{i:03}')
        rows.append(row)
    rows.sort(key=lambda row: tuple(row[key] for key in SPECS[name].key))
    table = Catalog(ROOT).build(name, rows, 3)
    files = {str(i): _encode(table.slice(i, block)) for i in range(0, len(rows), block)}
    reader = ServingIndex.__new__(ServingIndex)
    reader.settings = config(block=block, metric=metric)['index']
    reader.metadata = {'embedding': {'revision': 'test-space', 'dimension': 3},
                       'parts': [{'kind': name, 'path': path} for path in files]}
    reader._read_file = files.__getitem__  # labelled in-memory shard double, not a pinned snapshot
    expected = []
    for row in rows:
        vector = row['vector']
        score = sum(float(a) * float(b) for a, b in zip(vector, query))
        if metric == 'cosine':
            score /= math.sqrt(sum(a*a for a in vector)) * math.sqrt(sum(b*b for b in query))
        expected.append((score, tuple(row[key] for key in SPECS[name].key)))
    expected.sort(key=lambda item: (-item[0], item[1]))
    actual = reader.search(name, query, k=k, embedder_revision='test-space', dimension=3)
    keys = [tuple(hit['key'][key] for key in SPECS[name].key) for hit in actual]
    assert len(keys) == len(set(keys)) == min(k, len(expected))
    by_key = {key: score for score, key in expected}
    assert [hit['score'] for hit in actual] == pytest.approx([by_key[key] for key in keys], abs=1e-12)
    assert [hit['score'] for hit in actual] == pytest.approx([score for score, _ in expected[:k]], abs=1e-12)
    if metric == 'inner_product':  # integer inputs make the scalar dot products exactly representable
        assert keys == [key for _, key in expected[:k]]
    # Identical stored vectors have identical computed scores, including at the cutoff.
    for vector in {tuple(row['vector']) for row in rows}:
        tied_keys = [tuple(row[key] for key in SPECS[name].key) for row in rows
                     if tuple(row['vector']) == vector]
        selected_keys = [key for key in keys if key in tied_keys]
        assert selected_keys == tied_keys[:len(selected_keys)]
    for left, right in zip(actual, actual[1:]):
        if left['score'] == right['score']:
            assert tuple(left['key'].values()) < tuple(right['key'].values())


def _reader(name, vectors, block, metric):
    """Labelled in-memory shard double: returns the reader, its shard files and the vector stored under each key."""
    dimension = len(vectors[0])
    rows = []
    for i, vector in enumerate(vectors):
        row = {'embedder_revision': 'test-space', 'dim': dimension, 'vector': vector}
        if name == 'image_embeddings':
            row['image_id'] = image_id(str(i).encode())
        else:
            row.update(concept_id=concept_id('test'), text_kind='name', text_hash=f'{i:03}')
        rows.append(row)
    rows.sort(key=lambda row: tuple(row[key] for key in SPECS[name].key))
    table = Catalog(ROOT).build(name, rows, dimension)
    files = {str(i): _encode(table.slice(i, block)) for i in range(0, len(rows), block)}
    reader = ServingIndex.__new__(ServingIndex)
    reader.settings = config(block=block, metric=metric)['index']
    reader.metadata = {'embedding': {'revision': 'test-space', 'dimension': dimension},
                       'parts': [{'kind': name, 'path': path} for path in files]}
    reader._read_file = files.__getitem__
    owners = {tuple(row[key] for key in SPECS[name].key): tuple(row['vector']) for row in rows}
    return reader, files, owners


_COORDINATE = st.floats(-1e3, 1e3, allow_nan=False, allow_infinity=False).map(lambda x: x if abs(x) > 1e-6 else 0.0)
# Float vectors of one generated dimension (1 to 300): 1 to 9 rows, a query and how often row 0 is repeated (a content tie).
_FLOAT_CASE = st.integers(1, 300).flatmap(lambda dim: st.tuples(
    st.lists(st.lists(_COORDINATE, min_size=dim, max_size=dim), min_size=1, max_size=9),
    st.lists(_COORDINATE, min_size=dim, max_size=dim), st.integers(0, 3)))
_FLOAT_TABLES = st.sampled_from(['image_embeddings', 'concept_text_embeddings'])
_METRICS = st.sampled_from(['cosine', 'inner_product'])


def _usable(vectors, query, repeat, metric):
    if metric == 'cosine':  # cosine refuses a zero vector; the refusal has its own tests
        unit = [1.0] + [0.0] * (len(query) - 1)
        vectors = [v if any(v) else unit for v in vectors]
        query = query if any(query) else unit
    return vectors + vectors[:1] * repeat, query


@settings(max_examples=30, deadline=None)
@given(_FLOAT_CASE, _METRICS, _FLOAT_TABLES)
def test_score_of_a_vector_is_bitwise_the_same_in_any_shard_position_and_count(case, metric, name):
    """K5-TIE: a stored vector scores the same bits for every block size, shard count and row position."""
    vectors, query = _usable(*case, metric)
    count = len(vectors)
    scores = {}
    # Rotating and reversing the vectors moves each one to another shard and another row within it.
    for order in (vectors, vectors[::-1], vectors[1:] + vectors[:1]):
        for block in sorted({1, 2, 3, 5, count}):
            reader, _, owners = _reader(name, order, block, metric)
            for hit in reader.search(name, query, k=count, embedder_revision='test-space', dimension=len(query)):
                scores.setdefault(owners[tuple(hit['key'].values())], set()).add(hit['score'])
    assert all(len(values) == 1 for values in scores.values())


@settings(max_examples=30, deadline=None)
@given(_FLOAT_CASE, _METRICS, _FLOAT_TABLES, st.sampled_from([1, 3, 64]))
def test_scores_equal_the_blas_product_within_the_rounding_bound(case, metric, name, block):
    """Differential (S46): the row-wise sum equals the former `matrix @ query` up to the dot-product rounding bound."""
    vectors, query = _usable(*case, metric)
    reader, files, _ = _reader(name, vectors, block, metric)
    actual = {tuple(hit['key'].values()): hit['score']
              for hit in reader.search(name, query, k=len(vectors), embedder_revision='test-space',
                                       dimension=len(query))}
    normal = np.asarray(query, dtype=SCORE_DTYPE)
    if metric == 'cosine':  # the query scaling of `ServingIndex.search`, restated for the reference
        normal = normal / np.max(np.abs(normal))
        normal /= np.sqrt(np.sum(normal * normal))
    for path in reader.metadata['parts']:
        batch = _decode(files[path['path']])
        matrix = _matrix(batch, metric)
        old = matrix @ normal
        # Each dot product errs by at most dim * eps * sum(|a_i b_i|); two orderings differ by at most twice that.
        bound = 2 * len(normal) * np.finfo(SCORE_DTYPE).eps * (np.abs(matrix) @ np.abs(normal))
        keys = list(zip(*(batch.column(key).to_pylist() for key in SPECS[name].key)))
        for key, value, limit in zip(keys, old, bound):
            assert abs(actual[key] - value) <= limit + 1e-300


@pytest.mark.parametrize('block', [1, 3])
@pytest.mark.parametrize('metric, vectors, query, tiny, expected', [
    # Inner product: 1e-10 * 1e-10. A tiny score is built from tiny coordinates.
    ('inner_product', [[1e-10, 0.0, 0.0], [1e-10, 0.0, 0.0], [0.0, 1e-10, 0.0]], [1e-10, 0.0, 0.0],
     (1e-10, 0.0, 0.0), 1e-20),
    # Cosine normalises every stored vector to unit length, so coordinates cannot be tiny; a tiny true score is a
    # near-orthogonal pair instead: the cosine of [1, 1e-17, 0] with [0, 1, 0] is 1e-17.
    ('cosine', [[1.0, 1e-17, 0.0], [1.0, 1e-17, 0.0], [1.0, 0.0, 0.0]], [0.0, 1.0, 0.0], (1.0, 1e-17, 0.0), 1e-17),
])
def test_tiny_true_scores_are_kept_not_rounded_or_zeroed(block, metric, vectors, query, tiny, expected):
    """K5-TIE must not buy tie stability by rounding or zeroing small scores (S46), for both metrics."""
    reader, _, owners = _reader('image_embeddings', vectors, block, metric)
    hits = reader.search('image_embeddings', query, k=3, embedder_revision='test-space', dimension=3)
    by_vector = {}
    for hit in hits:
        by_vector.setdefault(owners[tuple(hit['key'].values())], []).append(hit['score'])
    # The stored vector is float32, so the expected tiny score holds to float32 precision only.
    assert by_vector[tiny] == pytest.approx([expected] * 2, rel=1e-6, abs=0)
    assert sorted(score for vector, scores in by_vector.items() if vector != tiny for score in scores) == [0.0]


@settings(max_examples=30, deadline=None)
@given(_FLOAT_CASE, _METRICS, st.sampled_from(['fortran', 'strided', 'negative_row_stride']))
def test_scores_do_not_depend_on_the_memory_layout_of_the_matrix(case, metric, layout):
    """The row sum is shard-independent only on C-contiguous rows: `search` must enforce that layout itself.

    Labelled double: `_matrix` is replaced by a wrapper that returns the same values in another memory layout, as a
    future zero-copy `_matrix` could. Scores must stay bitwise equal to those of the real, C-contiguous `_matrix`.
    """
    vectors, query = _usable(*case, metric)
    name = 'image_embeddings'
    reader, _, owners = _reader(name, vectors, 3, metric)
    expected = {tuple(h['key'].values()): h['score']
                for h in reader.search(name, query, k=len(vectors), embedder_revision='test-space',
                                       dimension=len(query))}
    real = k5_index._matrix

    def relaid(batch, kind):
        matrix = real(batch, kind)
        if layout == 'fortran':
            return np.asfortranarray(matrix)
        if layout == 'strided':  # every other column of a wider array
            wide = np.zeros((matrix.shape[0], 2 * matrix.shape[1]))
            wide[:, ::2] = matrix
            return wide[:, ::2]
        return np.ascontiguousarray(matrix[::-1])[::-1]  # same rows, walked backwards in memory

    with pytest.MonkeyPatch.context() as patch:  # per example, so a wrapper never wraps an earlier wrapper
        patch.setattr(k5_index, '_matrix', relaid)
        actual = {tuple(h['key'].values()): h['score']
                  for h in reader.search(name, query, k=len(vectors), embedder_revision='test-space',
                                         dimension=len(query))}
    assert actual == expected


@pytest.mark.parametrize('name', ['image_embeddings', 'concept_text_embeddings'])
@pytest.mark.parametrize('metric', ['cosine', 'inner_product'])
@pytest.mark.parametrize('block', [1, 2, 6])
def test_equal_scores_at_cutoff_use_complete_key(name, metric, block):
    rows = []
    for i in range(6):
        row = {'embedder_revision': 'test-space', 'dim': 3, 'vector': [1, 0, 0]}
        if name == 'image_embeddings':
            row['image_id'] = image_id(str(i).encode())
        else:
            row.update(concept_id=concept_id('test'), text_kind='name', text_hash=f'{i:03}')
        rows.append(row)
    keys = SPECS[name].key
    rows.sort(key=lambda row: tuple(row[key] for key in keys))
    table = Catalog(ROOT).build(name, rows, 3)
    files = {str(i): _encode(table.slice(i, block)) for i in range(0, len(rows), block)}
    reader = ServingIndex.__new__(ServingIndex)
    reader.settings = config(block=block, metric=metric)['index']
    reader.metadata = {'embedding': {'revision': 'test-space', 'dimension': 3},
                       'parts': [{'kind': name, 'path': path} for path in files]}
    reader._read_file = files.__getitem__  # labelled in-memory shard double
    for k in (1, 3):
        hits = reader.search(name, [1, 0, 0], k=k, embedder_revision='test-space', dimension=3)
        assert [tuple(hit['key'][key] for key in keys) for hit in hits] == [
            tuple(row[key] for key in keys) for row in rows[:k]]
        assert [hit['score'] for hit in hits] == [1.0] * k


@settings(max_examples=45, deadline=None)
@given(st.text(max_size=45), st.integers(1, 7))
def test_ngram_posting_roundtrip(text, n):
    normalized = unicodedata.normalize('NFKC', text).casefold()
    expected = set(''.join(normalized[i:i+n]) for i in range(max(0, len(normalized)-n+1)))
    assert character_ngrams(text, n) == expected
    table = pa.table({'gram': sorted(expected)}, schema=pa.schema([pa.field('gram', pa.string())]))
    assert set(_decode(_encode(table)).column('gram').to_pylist()) == expected


@settings(max_examples=25, deadline=None)
@given(st.text(min_size=1, max_size=20), st.integers(1, 6))
def test_every_index_entry_and_coverage_match_snapshot(text, n):
    catalog = Catalog(ROOT)
    rows = dataset()
    row = rows['aliases'][0]
    row['alias_text'] = text
    tables = {name: catalog.build(name, values, 4 if SPECS[name].vector else None) for name, values in rows.items()}
    exact, lexical, _ = _lookups(tables, config(n=n)['index'], catalog)
    for item in exact.to_pylist() + lexical.to_pylist():
        assert item['concept_id'] in {row['concept_id'] for row in rows['concepts']}
        assert item['source_table'] in ('concepts', 'aliases', 'folder_concepts')
    assert any(item['concept_id'] == row['concept_id'] and item['token'] == unicodedata.normalize('NFKC', text).casefold()
               for item in exact.to_pylist())


@settings(max_examples=30, deadline=None)
@given(st.one_of(st.none(), st.booleans(), st.integers(), st.text(),
                 st.lists(st.integers(), max_size=5), st.dictionaries(st.text(max_size=8), st.integers(), max_size=5)))
def test_metadata_parser_refuses_malformed_documents(value):
    with pytest.raises(StorageError):
        _parse_document(_document(value), Catalog(ROOT))
