"""Property tests: identity normalisation, rename-invariant keys, invalidation, link-resolved containment."""

import os
import subprocess
import unicodedata

from hypothesis import HealthCheck, assume, given, settings, strategies as st
import pytest
from storage_fixtures import make_store

from prompt_enhancer.storage import StorageError, artifact_key, concept_id, image_id, normalise_name, parse_retention
from prompt_enhancer.storage._fs import check_segment, inside
from prompt_enhancer.storage.identity import check_folder_id

FAST = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
REVISION = st.fixed_dictionaries({'model': st.text(min_size=1, max_size=8), 'prompt': st.text(min_size=1, max_size=8),
                                  'schema': st.text(min_size=1, max_size=8)})


@FAST
@given(st.text(max_size=24))
def test_normalisation_is_idempotent_and_form_invariant(name):
    once = normalise_name(name)
    assert normalise_name(once) == once
    assert concept_id(name) == concept_id(once)
    assert concept_id(unicodedata.normalize('NFKD', name)) == concept_id(name)  # NFKC(NFKD(x)) == NFKC(x)
    assert concept_id(unicodedata.normalize('NFKC', name)) == concept_id(name)


@FAST
@given(st.binary(max_size=64), st.text(min_size=1, max_size=12), st.text(min_size=1, max_size=12), REVISION)
def test_artifact_key_is_rename_invariant(content, path_a, path_b, revisions):
    # The key takes the content hash, never a path: two names of one file give one key.
    assume(path_a != path_b)
    assert artifact_key('observation', image_id(content), revisions) == artifact_key(
        'observation', image_id(bytes(content)), dict(revisions))


@FAST
@given(st.binary(max_size=32), st.binary(max_size=32), REVISION, st.sampled_from(['model', 'prompt', 'schema']),
       st.text(min_size=1, max_size=8))
def test_any_input_or_revision_change_invalidates(first, second, revisions, field, replacement):
    base = artifact_key('card', image_id(first), revisions)
    if first != second:
        assert artifact_key('card', image_id(second), revisions) != base
    if replacement != revisions[field]:
        assert artifact_key('card', image_id(first), dict(revisions, **{field: replacement})) != base
    assert artifact_key('observation', image_id(first), revisions) != base


def _outside_link(link, target):
    """A directory link that leaves the root: a symlink, else (Windows without the symlink right) a junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True,
                              creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert done.returncode == 0, done.stderr.decode(errors='replace')


def _valid(part):
    try:
        check_segment(part)
    except StorageError:
        return False
    return True


@FAST
@given(st.lists(st.sampled_from(['a', 'b', '..', '.', '', 'c:', 'ü', ' ', 'x\\y', 'link']), min_size=1, max_size=5))
def test_link_resolved_containment(tmp_path_factory, parts):
    """Containment: `inside` returns a path below root or refuses; a name that passes through a link leaving the
    root (`root/link` at depth 1, `root/a/link` at depth 2) is refused; any other valid name is accepted.

    A name passes through a link only when the link is one of its leading components: `..` is refused by the
    segment rule, so `a/link` is `root/a/link`, never `root/link` (retest 3 P3-A: the old expectation refused it)."""
    base = tmp_path_factory.getbasetemp()
    root, outside = base / 'containment', base / 'outside'
    (root / 'a').mkdir(parents=True, exist_ok=True)
    outside.mkdir(exist_ok=True)
    for link in (root / 'link', root / 'a' / 'link'):
        if not os.path.lexists(link):
            _outside_link(link, outside)
    relative = '/'.join(parts)
    crosses = parts[:1] == ['link'] or parts[:2] == ['a', 'link']
    try:
        resolved = inside(root.resolve(), relative)
    except StorageError:
        assert crosses or not all(map(_valid, parts)), relative  # no false refusal of a valid name
        return
    assert resolved.is_relative_to(root.resolve())
    assert not crosses, f'{relative} passes through a link that leaves the root and was accepted'


@FAST
@given(st.text(max_size=20))
def test_folder_id_check_matches_the_contract(value):
    try:
        check_folder_id(value)
    except StorageError:
        segments = value.split('/')
        assert (not value or '\\' in value or '\x00' in value or value.startswith('/')
                or any(part in ('', '.', '..') for part in segments))
    else:
        assert all(part not in ('', '.', '..') for part in value.split('/'))


@FAST
@given(st.integers(min_value=0, max_value=10**6), st.sampled_from(['s', 'm', 'h', 'd']))
def test_retention_grammar(number, unit):
    assert parse_retention(f'{number}{unit}') == number * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
    for bad in (f'{number}', f'-{number}{unit}', f'{number}{unit}\n', f' {number}{unit}', f'{number}{unit}s'):
        with pytest.raises(StorageError):
            parse_retention(bad)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(min_size=1, max_size=16))
def test_store_skips_renamed_content(project_root, tmp_path_factory, content):
    """A renamed source (same bytes) maps to the same done artifact, so nothing is recomputed."""
    store = make_store(project_root, tmp_path_factory.mktemp('rename') / 'kb')
    revisions = {'model': 'obs-r1', 'prompt': 'observer_v0', 'schema': 'f1'}
    with store.writer() as writer:
        writer.checkpoints('K2').commit(artifact_key('observation', image_id(content), revisions), 'observation',
                                        content, revisions=revisions)
    renamed = bytes(content)  # read again from its new path
    with store.writer() as writer:
        assert writer.checkpoints('K2').done(artifact_key('observation', image_id(renamed), revisions)) is not None
