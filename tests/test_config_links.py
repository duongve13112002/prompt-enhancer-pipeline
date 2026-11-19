"""P3-SWEEP-2 item 7: the loader refuses a link on every path the project owns or writes (AGENTS.md section 6) and still
resolves the read-only knowledge root through one. Real links, generated path shapes, no mocks."""

import os
import shutil
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from link_fixtures import drop_link, make_link
from prompt_enhancer.config import ConfigurationError, LinkedPath, _configured_path, _inside, _resolve_within, validate_sync_config

LINK = 'is a link or another spelling of a stored name'
SEGMENT = st.sampled_from(['a', 'b', 'c', 'd'])


@pytest.fixture
def area(tmp_path):
    """A scratch project root with one real directory `real/` that links point at (inside the root, so a refusal can
    only come from the link rule and not from containment)."""
    root = (tmp_path / 'project').resolve()
    (root / 'real' / 'deep').mkdir(parents=True)
    return root


def build(root, segments, link_at):
    """Create `segments` below a fresh directory of `root`; component `link_at` (or none) is a link to `real/`."""
    base = root / uuid.uuid4().hex[:8]
    base.mkdir()
    current = base
    links = []
    for index, name in enumerate(segments):
        if index == link_at:
            make_link(current / name, root / 'real')
            links.append(current / name)
            current = root / 'real'  # what the link shows: later segments exist inside its target
            continue
        (current / name).mkdir(exist_ok=True)
        current = current / name
    return base, links


def run_kind(kind, root, candidate):
    if kind == 'inside':
        return _inside(candidate, root)
    if kind == 'owned':
        return _configured_path(str(candidate.relative_to(root)), root, 'config.yaml', 'storage.data_root')
    _configured_path(str(candidate.relative_to(root)), root, 'config.yaml', 'knowledge.root', links=True)
    return _resolve_within(candidate, root, set(), 'config.yaml', 'knowledge.root', links=True)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(segments=st.lists(SEGMENT, min_size=1, max_size=3, unique=True), where=st.integers(0, 2),
       kind=st.sampled_from(['inside', 'owned', 'knowledge']))
def test_a_link_at_any_component_is_refused_on_owned_paths_and_followed_for_the_knowledge_root(
        area, segments, where, kind):
    """Property over generated shapes: whatever the depth and wherever the link sits, an owned path is refused and the
    knowledge root resolves through it; a path with no link is accepted by every kind."""
    link_at = where if where < len(segments) else None
    base, links = build(area, segments, link_at)
    try:
        candidate = base.joinpath(*segments)
        if link_at is None:
            run_kind(kind, area, candidate)  # a plain path is never refused
        elif kind == 'knowledge':
            assert run_kind(kind, area, candidate) is not None  # followed: the target is inside the root
        else:
            with pytest.raises(ConfigurationError, match=LINK):
                run_kind(kind, area, candidate)
    finally:
        for link in reversed(links):
            drop_link(link)
        shutil.rmtree(base, ignore_errors=True)


def test_a_link_through_a_directory_that_does_not_exist_yet_is_still_refused(area):
    """The written path `data/new` below a linked `data`: the missing last component must not hide the link above it."""
    make_link(area / 'data', area / 'real')
    try:
        for kind in ('inside', 'owned'):
            with pytest.raises(ConfigurationError, match=LINK):
                run_kind(kind, area, area / 'data' / 'not-yet' / 'file')
    finally:
        drop_link(area / 'data')


def test_the_knowledge_root_link_is_resolved_and_its_target_still_has_to_be_a_directory_inside_the_project(area):
    make_link(area / 'kb', area / 'real')
    try:
        resolved = _resolve_within(area / 'kb', area, set(), 'config.yaml', 'knowledge.root', links=True)
        assert resolved == (area / 'real').resolve()
        with pytest.raises(ConfigurationError, match=LINK):  # the same call without the allowance
            _resolve_within(area / 'kb', area, set(), 'config.yaml', 'knowledge.root')
    finally:
        drop_link(area / 'kb')
    outside = area.parent / 'outside'
    outside.mkdir()
    make_link(area / 'away', outside)
    try:
        with pytest.raises(ConfigurationError, match='resolved outside project root'):
            _resolve_within(area / 'away', area, set(), 'config.yaml', 'knowledge.root', links=True)
    finally:
        drop_link(area / 'away')


@pytest.mark.skipif(os.name != 'nt', reason='case-insensitive file names are a Windows fact')
def test_another_case_of_an_existing_name_is_not_taken_for_a_link(area):
    (area / 'Data').mkdir()
    resolved = _inside(area / 'data' / 'x', area)
    assert os.path.normcase(str(resolved)) == os.path.normcase(str(area / 'Data' / 'x'))
    _configured_path('DATA/x', area, 'config.yaml', 'storage.data_root')


def test_a_component_spelled_differently_from_its_real_name_is_refused_unless_links_are_followed(area, monkeypatch):
    """Fix round 1 (F3): the non-link branch compares the real path with the spelled one (an 8.3 short name or a swap
    since the check); short names are off on many volumes, so `realpath` is the labelled double here."""
    (area / 'stores').mkdir()
    spelled, other = area / 'stores', area / 'stores-long-name'
    real = os.path.realpath

    def another_spelling(path, *args, **kwargs):  # labelled double: one component resolves to another spelling in the root
        return str(other) if Path(path) == spelled else real(path, *args, **kwargs)
    monkeypatch.setattr(os.path, 'realpath', another_spelling)
    with pytest.raises(LinkedPath, match=LINK):
        _resolve_within(spelled / 'x', area, set(), 'config.yaml', 'storage.kb_store')
    assert _resolve_within(spelled, area, set(), 'config.yaml', 'knowledge.root', links=True) == other  # read only: followed


@pytest.mark.parametrize('key', ['vlm.backends.main.model.file', 'validation.verifier.calibration_ref'])
def test_a_model_artifact_path_and_a_verifier_path_refuse_a_link(area, key):
    """Fix round 1 (F7): the two owned key classes that load through `_configured_path(...)` with their own arguments
    (`protected=False` for a model file); the loader passes no `links=`, so a link on the way is refused."""
    make_link(area / 'models-link', area / 'real')
    try:
        protected = key.startswith('validation')
        with pytest.raises(LinkedPath, match=LINK):
            _configured_path('models-link/deep/artifact.gguf', area, 'config.yaml', key, protected=protected)
    finally:
        drop_link(area / 'models-link')


def test_the_loader_refuses_a_linked_model_file_through_the_real_validator(area, model_test_profile):
    """The same refusal through `_validate_configured_paths`, the function that walks `vlm.backends.*.model.file`."""
    from prompt_enhancer.config import _validate_configured_paths
    make_link(area / 'models-link', area / 'real')
    try:
        model_test_profile['vlm']['backends']['main']['model']['file'] = 'models-link/deep/m.gguf'
        with pytest.raises(LinkedPath, match=LINK):
            _validate_configured_paths(model_test_profile, area, 'config.yaml', {})
    finally:
        drop_link(area / 'models-link')


def test_the_sync_configuration_refuses_a_linked_store_and_accepts_a_linked_knowledge_root(k2_env):
    """The same rule through the real validator that `kb sync` runs before it opens anything."""
    env = k2_env
    validate_sync_config(env.config, project_root=env.root)  # the unlinked baseline passes
    scratch = env.source.parent
    make_link(scratch / 'kb-link', env.source)
    make_link(scratch / 'store-link', env.store.root)
    try:
        linked_kb = dict(env.config, knowledge=dict(env.config['knowledge'],
                                                    root=(scratch / 'kb-link').relative_to(env.root).as_posix()))
        validate_sync_config(linked_kb, project_root=env.root)  # read only input: resolved, not refused
        linked_store = dict(env.config, storage=dict(env.config['storage'],
                                                     kb_store=(scratch / 'store-link').relative_to(env.root).as_posix()))
        with pytest.raises(ConfigurationError, match=LINK):
            validate_sync_config(linked_store, project_root=env.root)
    finally:
        drop_link(scratch / 'kb-link')
        drop_link(scratch / 'store-link')


def test_the_knowledge_root_must_be_an_existing_directory(k2_env):
    env = k2_env
    missing = dict(env.config, knowledge=dict(env.config['knowledge'], root='no-such-folder'))
    with pytest.raises(ConfigurationError, match='knowledge.root: not an existing directory'):
        validate_sync_config(missing, project_root=env.root)
    a_file = env.source / 'a-file'
    a_file.write_bytes(b'x')
    as_file = dict(env.config, knowledge=dict(env.config['knowledge'], root=a_file.relative_to(env.root).as_posix()))
    with pytest.raises(ConfigurationError, match='knowledge.root: not an existing directory'):
        validate_sync_config(as_file, project_root=env.root)
