"""Bounded CPU properties: independent scalar ranking, snapshot references and code-point windows."""

import math
from pathlib import Path
import unicodedata

from hypothesis import given, settings, strategies as st
import pyarrow as pa
import pytest

from k5_fixtures import config
from prompt_enhancer.k5_index import ServingIndex, _decode, _encode, _lookups, character_ngrams
from prompt_enhancer.k5_index import _document, _parse_document
from prompt_enhancer.storage import Catalog, SPECS, StorageError, concept_id, image_id
from storage_fixtures import dataset


ROOT = Path(__file__).resolve().parents[1]


@settings(max_examples=45, deadline=None)
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
