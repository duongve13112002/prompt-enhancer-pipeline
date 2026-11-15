from datetime import datetime, timezone

from hypothesis import given, strategies as st

import pytest

from prompt_enhancer.k0 import diff_folders, diff_rows, guard_removals, parse_info, resolve_workers, rows_from_scan
from prompt_enhancer.storage import Catalog, check_references, concept_id


def test_names_and_mismatch_are_preserved():
    parsed = parse_info('Ｋimono|Donut\n  original paragraph  ', '|')
    assert parsed['concept_ids'][0] == concept_id('kimono')
    assert parsed['names'][0] == 'Ｋimono'
    assert parsed['line2'] == '  original paragraph  '
    assert parsed['status'] == 'description_mismatch'


def test_empty_and_duplicate_names():
    assert parse_info('a| ', '|')['status'] == 'pending_naming'
    assert parse_info('A|ａ', '|')['status'] == 'duplicate_names'


@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',),
                                     blacklist_characters='|\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029'), min_size=1))
def test_single_name_does_not_change_identity(name):
    parsed = parse_info(name, '|')
    if name.strip():
        assert parsed['concept_ids'] == [concept_id(name.strip())]


@given(st.dictionaries(st.text(min_size=1), st.integers(), max_size=20))
def test_unchanged_folders_have_empty_plan(folders):
    assert diff_folders(folders, folders) == {
        'added': [], 'removed': [], 'renamed': [], 'changed': [], 'ambiguous': []}


@given(st.text(min_size=1), st.text(min_size=1), st.integers())
def test_unique_folder_rename_preserves_signature(old, new, signature):
    if old == new:
        return
    plan = diff_folders({old: signature}, {new: signature})
    assert plan['renamed'] == [(old, new)]
    assert plan['added'] == plan['removed'] == plan['changed'] == []


@given(st.integers(min_value=0, max_value=100), st.floats(min_value=0, max_value=1,
                                                          allow_nan=False, allow_infinity=False))
def test_guard_uses_strict_fraction_boundary(count, threshold):
    previous = 100
    plan = {'removed': list(range(count))}
    if count / previous > threshold:
        with pytest.raises(ValueError, match='mass deletion'):
            guard_removals(plan, previous, threshold)
    else:
        assert guard_removals(plan, previous, threshold) == count / previous
    assert guard_removals(plan, previous, threshold, force=True) == count / previous


def test_unique_and_ambiguous_renames():
    assert diff_folders({'a': 'hash'}, {'b': 'hash'})['renamed'] == [('a', 'b')]
    plan = diff_folders({'a': 'hash', 'c': 'hash'}, {'b': 'hash'})
    assert plan['removed'] == ['a', 'c']
    assert plan['ambiguous'] == ['a', 'c']


def test_guard_strict_threshold_and_force():
    plan = {'removed': ['a']}
    assert guard_removals(plan, 2, 0.5) == 0.5
    with pytest.raises(ValueError, match='mass deletion'):
        guard_removals(plan, 2, 0.4)
    assert guard_removals(plan, 2, 0.4, force=True) == 0.5
    assert guard_removals({'removed': []}, 0, 0) == 0


def test_resolve_workers(monkeypatch):
    monkeypatch.setattr('prompt_enhancer.k0.os.sched_getaffinity', lambda _: {1, 2}, raising=False)
    assert resolve_workers('auto') == 2
    assert resolve_workers(3) == 3
    with pytest.raises(ValueError):
        resolve_workers(0)


def test_auto_workers_falls_back_when_affinity_is_unavailable(monkeypatch):
    def unavailable(_):
        raise OSError('affinity unavailable')
    monkeypatch.setattr('prompt_enhancer.k0.os.sched_getaffinity', unavailable, raising=False)
    monkeypatch.setattr('prompt_enhancer.k0.os.cpu_count', lambda: 3)
    assert resolve_workers('auto') == 3


def test_row_diff_tracks_rename_without_changing_image_identity():
    old = {'folders': [{'folder_id': 'old', 'info_file_hash': 'info'}],
           'folder_images': [{'folder_id': 'old', 'relative_path': 'a.jpg', 'image_id': 'sha256:one'}],
           'images': [{'image_id': 'sha256:one'}], 'concepts': []}
    new = {'folders': [{'folder_id': 'new', 'info_file_hash': 'info'}],
           'folder_images': [{'folder_id': 'new', 'relative_path': 'a.jpg', 'image_id': 'sha256:one'}],
           'images': [{'image_id': 'sha256:one'}], 'concepts': []}
    plan = diff_rows(old, new)
    assert plan['folders']['renamed'] == [('old', 'new')]
    assert plan['images'] == {'added': [], 'removed': [], 'changed': []}
    assert plan['folder_images']['moved'] == [(('old', 'a.jpg'), ('new', 'a.jpg'), 'sha256:one')]
    assert diff_rows(new, new)['folder_images']['moved'] == []


def test_rows_deduplicate_and_retain_original_names(project_root):
    image = {'image_id': 'sha256:' + 'a' * 64, 'size_bytes': 3,
             'modified_at': datetime(2026, 1, 1, tzinfo=timezone.utc)}
    rows = rows_from_scan({'one': {'info': {'hash': 'hash1', 'text': 'Ｋimono\nfirst'},
                                   'images': {'a.jpg': image}},
                           'two': {'info': {'hash': 'hash2', 'text': 'kimono\nsecond'},
                                   'images': {'b.jpg': image}},
                           'pending': {'info': None, 'images': {}}}, '|')
    assert len(rows['images']) == len(rows['concepts']) == 1
    assert len(rows['folder_images']) == len(rows['folder_concepts']) == 2
    assert [row['name_as_written'] for row in rows['folder_concepts']] == ['Ｋimono', 'kimono']
    pending = next(row for row in rows['folders'] if row['folder_id'] == 'pending')
    assert (pending['declared_names'], pending['name_source'], pending['error']) == ([], None, 'pending_naming')
    catalog = Catalog(project_root)
    tables = {name: catalog.build(name, values) for name, values in rows.items()}
    for name, table in tables.items():
        catalog.validate(name, table)
    check_references(tables)
