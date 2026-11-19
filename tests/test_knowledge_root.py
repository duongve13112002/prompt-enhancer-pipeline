"""KB-EXTERNAL (decision 175): `resolve_knowledge_root`, the one place that turns `knowledge.root` into a physical path.

Real directories and real links (a junction on Windows, a symlink elsewhere), no mocks except the labelled spies that
prove `.secrets` is never resolved. The root may lie outside the project; it is refused when it equals or contains the
project root or equals, lies inside or contains `models/`, `.secrets/`, `.git`, any `.venv*`, `storage.data_root` or
`storage.kb_store`, each side resolved first."""

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from link_fixtures import drop_link, make_link
from prompt_enhancer import config as module
from prompt_enhancer.config import ConfigurationError, resolve_knowledge_root, same_physical_path

WINDOWS = os.name == 'nt'
REFUSED = r'knowledge\.root|overlaps knowledge\.root'


@pytest.fixture
def area(tmp_path):
    """`project/` (the fake project root, with the protected directories) next to `external/` (outside it)."""
    project = (tmp_path / 'project').resolve()
    for name in ('models', '.git', '.venv', '.venv-gpu', 'data', 'database', 'kb', 'models2'):
        (project / name).mkdir(parents=True)
    (project / '.secrets').mkdir()  # an empty directory: no test ever writes, lists or opens inside it
    external = (tmp_path / 'external').resolve()
    (external / 'kb' / 'sub').mkdir(parents=True)
    return project, external


def config_for(root, data_root='data', kb_store='data/kb'):
    return {'knowledge': {'root': str(root)}, 'storage': {'data_root': data_root, 'kb_store': kb_store}}


def resolve(project, root, **options):
    return resolve_knowledge_root(config_for(root), project, 'config.yaml', **options)


@pytest.mark.parametrize('spelled', ['kb', 'database', 'a/b', 'models2', 'datax', 'venv', 'kb/../database'])
def test_a_root_inside_the_project_outside_the_protected_directories_stays_valid(area, spelled):
    project, _ = area
    assert resolve(project, spelled) == Path(os.path.realpath(project / spelled))


def test_a_root_outside_the_project_is_valid_in_every_spelling_and_is_returned_physically(area):
    project, external = area
    expected = Path(os.path.realpath(external / 'kb'))
    assert resolve(project, external / 'kb', require_dir=True) == expected  # absolute
    assert resolve(project, os.path.relpath(external / 'kb', project).replace('\\', '/'), require_dir=True) == expected
    assert resolve(project, str(external / 'kb') + '/', require_dir=True) == expected
    assert resolve(project, str(external / 'kb' / 'sub' / '..'), require_dir=True) == expected


@pytest.mark.parametrize('spelled', [
    '.', '..', '.secrets', '.secrets/x', '.git', '.git/x', 'models', 'models/x', '.venv', '.venv/x', '.venv-gpu',
    '.venv-gpu/x', '.venvother/x', 'data', 'data/x', 'data/kb', 'data/kb/x'])
def test_the_project_root_and_the_protected_directories_are_refused(area, spelled):
    project, _ = area
    with pytest.raises(ConfigurationError, match=REFUSED):
        resolve(project, spelled)


def test_a_parent_of_the_project_root_is_refused(area):
    project, external = area
    for parent in (project.parent, project.parent.parent):
        with pytest.raises(ConfigurationError, match='project root'):
            resolve(project, parent)
    assert project.parent != external  # the sibling itself is not a parent: it is valid
    assert resolve(project, external) == Path(os.path.realpath(external))


def test_a_link_to_a_parent_of_the_project_root_is_refused_with_the_root_message(area):
    """Past the lexical pre-check only the physical comparison names the cause; otherwise the message would blame
    `models`, a directory the user did not name."""
    project, external = area
    for name, target in (('up', project.parent), ('up2', project.parent.parent)):
        make_link(external / name, target)
        try:
            with pytest.raises(ConfigurationError, match='must not be the project root or contain it'):
                resolve(project, external / name)
        finally:
            drop_link(external / name)


def test_a_store_is_refused_in_both_directions_whatever_its_key(area):
    project, _ = area
    for root in ('data', 'data/kb/x', 'data/kb', project.parent):
        with pytest.raises(ConfigurationError, match=REFUSED):
            resolve_knowledge_root(config_for(root), project, 'config.yaml')
    # a store elsewhere moves the protection with it: `kb` is the store now, `database` is not
    moved = config_for('kb/x', data_root='other', kb_store='kb')
    with pytest.raises(ConfigurationError, match=r'storage\.kb_store overlaps knowledge\.root'):
        resolve_knowledge_root(moved, project, 'config.yaml')
    assert resolve_knowledge_root(dict(moved, knowledge={'root': 'database'}), project, 'config.yaml')
    # a parent of the store is refused through the other direction
    parent = config_for('database', data_root='database/store', kb_store='database/store/kb')
    with pytest.raises(ConfigurationError, match=r'storage\.data_root overlaps knowledge\.root'):
        resolve_knowledge_root(parent, project, 'config.yaml')


def test_a_sentinel_store_is_skipped_and_a_sentinel_root_is_refused(area):
    project, _ = area
    config = config_for('kb', data_root='deploy_required', kb_store='deploy_required')
    assert resolve_knowledge_root(config, project, 'config.yaml') == Path(os.path.realpath(project / 'kb'))
    with pytest.raises(ConfigurationError, match='unresolved sentinel deploy_required'):
        resolve(project, 'deploy_required')


def test_the_directory_requirement_comes_last_and_names_the_key(area):
    project, external = area
    with pytest.raises(ConfigurationError, match=r'knowledge\.root: not an existing directory'):
        resolve(project, external / 'nope', require_dir=True)
    resolve(project, external / 'nope')  # shape only: a loaded configuration may name a base that does not exist yet
    (external / 'file').write_bytes(b'x')
    with pytest.raises(ConfigurationError, match='not an existing directory'):
        resolve(project, external / 'file', require_dir=True)
    with pytest.raises(ConfigurationError, match='protected project directory models'):
        resolve(project, 'models', require_dir=True)  # a refusal outranks the missing-directory message


@pytest.mark.parametrize('bad', ['', '\x00', 5, None])
def test_a_value_that_is_not_a_path_is_refused(area, bad):
    project, _ = area
    with pytest.raises(ConfigurationError, match='expected a non-empty path'):
        resolve_knowledge_root({'knowledge': {'root': bad}, 'storage': {}}, project, 'config.yaml')


@pytest.mark.skipif(os.name == 'nt', reason='a Windows drive spelling is refused only where it cannot be one')
def test_a_windows_drive_spelling_is_refused_on_other_platforms(area):
    project, _ = area
    with pytest.raises(ConfigurationError, match='Windows drive path'):
        resolve(project, 'C:\\kb')


@pytest.mark.skipif(not WINDOWS, reason='case-insensitive names and extended-length prefixes are Windows facts')
@pytest.mark.parametrize('spelled_with', ['upper', 'prefix', 'prefix_upper'])
def test_another_case_or_an_extended_length_prefix_gives_the_same_verdict(area, spelled_with):
    project, external = area

    def spell(path):
        text = str(path)
        text = text.upper() if 'upper' in spelled_with else text
        return '\\\\?\\' + text if 'prefix' in spelled_with else text

    for protected in (project, project / 'models', project / '.secrets' / 'x', project / '.venv-gpu', project / 'data' / 'kb',
                      project.parent):
        with pytest.raises(ConfigurationError, match=REFUSED):
            resolve(project, spell(protected))
    assert resolve(project, spell(external / 'kb')) == Path(os.path.realpath(external / 'kb'))
    assert resolve(project, spell(project / 'database')) == Path(os.path.realpath(project / 'database'))


@pytest.mark.skipif(not WINDOWS, reason='the extended-length prefix is stripped only on Windows')
def test_a_volume_form_without_a_drive_is_refused_as_not_absolute(area, monkeypatch):
    project, _ = area
    # labelled double: the resolving step of a volume mount point returns `\\?\Volume{GUID}\...`, which this machine
    # cannot create; after the prefix is stripped it is no absolute path
    guid = '\\\\?\\Volume{0f0f0f0f-0000-0000-0000-000000000000}\\kb'
    monkeypatch.setattr(module, '_physical', lambda path: Path(module._strip_extended_prefix(guid)))
    with pytest.raises(ConfigurationError, match='not absolute'):
        resolve(project, 'anything')


# -- links ------------------------------------------------------------------------------------------------------------
def test_a_link_from_outside_to_a_protected_directory_is_refused_at_its_target(area):
    project, external = area
    targets = {'tomodels': 'models', 'toroot': '.', 'todata': 'data', 'tostore': 'data/kb', 'togit': '.git',
               'tovenv': '.venv-gpu', 'tosecrets': '.secrets', 'tonested': 'models/x'}
    (project / 'models' / 'x').mkdir()
    made = []
    try:
        for name, target in targets.items():
            make_link(external / name, (project / target).resolve())
            made.append(external / name)
        for name, target in targets.items():
            with pytest.raises(ConfigurationError, match=REFUSED):
                resolve(project, external / name)
            if target != '.':  # below a link to the project root is a plain directory of the project: valid
                with pytest.raises(ConfigurationError, match=REFUSED):
                    resolve(project, external / name / 'sub')  # below the link: the same physical place
    finally:
        for link in made:
            drop_link(link)


def test_a_link_to_an_outside_directory_is_followed_from_inside_and_through_a_chain(area):
    project, external = area
    make_link(project / 'kb-link', external / 'kb')
    make_link(external / 'hop', project / 'kb-link')  # a link to a link
    try:
        expected = Path(os.path.realpath(external / 'kb'))
        assert resolve(project, 'kb-link', require_dir=True) == expected
        assert resolve(project, external / 'hop', require_dir=True) == expected
        assert same_physical_path(project / 'kb-link', external / 'hop')
        assert not same_physical_path(project / 'kb-link', project / 'kb')
    finally:
        drop_link(external / 'hop')
        drop_link(project / 'kb-link')


def test_a_protected_directory_that_is_itself_a_link_protects_its_target(area):
    """`models/` is often a junction to another drive: the target is protected, not only the name."""
    project, external = area
    (external / 'weights' / 'sub').mkdir(parents=True)
    os.rmdir(project / 'models')
    make_link(project / 'models', external / 'weights')
    try:
        for spelled in (external / 'weights', external / 'weights' / 'sub', external, project / 'models' / 'sub'):
            with pytest.raises(ConfigurationError, match=REFUSED):  # the target, below it, and above it
                resolve(project, spelled)
        # a sibling of the target is not protected, once the parent that contains the target is not the root
        assert resolve(project, external / 'kb')
    finally:
        drop_link(project / 'models')
        (project / 'models').mkdir()


def test_the_secrets_directory_is_compared_by_name_and_never_resolved(area, monkeypatch):
    project, external = area
    seen = []
    real = module._physical

    def spy(path):
        seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(module, '_physical', spy)  # labelled spy on the resolving step
    for spelled in ('.secrets', '.secrets/x', str(project / '.secrets' / 'x'), '.SECRETS' if WINDOWS else '.secrets/y'):
        with pytest.raises(ConfigurationError, match=REFUSED):
            resolve(project, spelled)
    assert seen == [], 'a spelled path into .secrets must be refused before any resolution'
    if WINDOWS:  # the extended-length spellings (review P3-4); a junction or an 8.3 spelling stays a documented limit
        for prefixed in ('\\\\?\\' + str(project / '.secrets'), '\\\\?\\' + str(project / '.secrets' / 'x'),
                         '\\\\?\\' + str(project / '.SECRETS' / 'x'), '//?/' + str(project / '.secrets').replace('\\', '/')):
            with pytest.raises(ConfigurationError, match=REFUSED):
                resolve(project, prefixed)
        assert seen == [], 'an extended-length spelling into .secrets must be refused before any resolution'
    resolve(project, external / 'kb')
    assert not [path for path in seen if '.secrets' in {part.lower() for part in path.parts}]


@pytest.mark.parametrize('failure', [ValueError('embedded null byte'), OSError(5, 'access denied'),
                                     OverflowError('path too deep')])
def test_a_resolving_failure_is_a_clean_refusal_without_the_exception_text(area, monkeypatch, failure):
    """`realpath` raises `ValueError` for some spellings and `OSError` for an unreadable link; neither escapes as a
    traceback, and the message names the key and the type only (labelled double of the resolving step)."""
    project, external = area

    def failing(path):
        raise failure

    monkeypatch.setattr(module, '_physical', failing)
    if isinstance(failure, OverflowError):  # not a path error: it is not swallowed as one
        with pytest.raises(OverflowError):
            resolve(project, external / 'kb')
        return
    with pytest.raises(ConfigurationError, match=rf'knowledge\.root: cannot be resolved: {type(failure).__name__}$') as error:
        resolve(project, external / 'kb')
    assert 'embedded' not in str(error.value) and 'denied' not in str(error.value)


@pytest.mark.skipif(not WINDOWS, reason='the delete-pending name is an NTFS fact')
def test_a_root_that_resolves_to_a_delete_pending_name_is_refused(area, monkeypatch):
    """Labelled double: `realpath` returns `<drive>\\$Extend\\$Deleted\\...` when the directory is unlinked while it is
    being resolved; that is a race, not a place to read."""
    project, external = area
    drive = Path(external.anchor)
    monkeypatch.setattr(module, '_physical', lambda path: drive / '$Extend' / '$Deleted' / 'kb')
    with pytest.raises(ConfigurationError, match=r'knowledge\.root: changed while it was resolved'):
        resolve(project, external / 'kb')


# -- the property: any generated layout agrees with an independent oracle --------------------------------------------
PROTECTED_TOP = ('.secrets', '.git', 'models', '.venv', 'data')
NAMES = ('kb', 'database', 'datax', 'models2', 'venv', 'a', *PROTECTED_TOP, '.venv-gpu', '.venvx')


def oracle(project, base, segments):
    """Independent verdict from the path parts alone: True when the root must be refused."""
    fold = os.path.normcase
    start = project if base == 'project' else project.parent / 'external'
    final = tuple(fold(p) for p in Path(os.path.normpath(start.joinpath(*segments))).parts)  # lexical collapse of `..`
    root = tuple(fold(p) for p in project.parts)
    if final[:len(final)] == root[:len(final)]:
        return True  # the project root itself or one of its parents
    if final[:len(root)] != root:
        return False  # elsewhere: outside the project and not above it
    below = final[len(root):]
    return below[0] in {fold(n) for n in PROTECTED_TOP} or below[0].startswith(fold('.venv'))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(base=st.sampled_from(['project', 'external']),
       segments=st.lists(st.sampled_from(NAMES + ('..', '.')), max_size=4),
       flip=st.booleans())
def test_the_verdict_over_generated_layouts_matches_the_oracle(area, base, segments, flip):
    project, external = area
    segments = [s for s in segments if s != '.']
    start = project if base == 'project' else external
    text = str(start.joinpath(*segments)) if segments else str(start)
    if flip and WINDOWS:
        text = text.swapcase()  # Windows names are case-insensitive: the verdict must not move
    expected = oracle(project, base, segments)
    try:
        resolve(project, text)
        refused = False
    except ConfigurationError:
        refused = True
    assert refused == expected, (text, expected)


def test_k2_stages_refuse_a_root_argument_that_is_not_the_configured_physical_root(k2_env):
    """A link retargeted between K0 and K2 must fail closed: the stage argument is compared with a fresh result of the
    same resolver, so another directory is never read under the old validation."""
    import asyncio

    from k2_fixtures import scan
    from prompt_enhancer.k2_naming import name_folders
    from prompt_enhancer.k2_observe import observe_images

    env = k2_env
    scanned = scan(env)
    scratch = env.source.parent
    first, second = scratch / 'first-kb', scratch / 'second-kb'
    first.mkdir()
    second.mkdir()
    link = scratch / 'retargeted'
    make_link(link, first)
    try:
        env.config['knowledge']['root'] = str(link)
        with pytest.raises(ConfigurationError, match='stage argument does not match configured root'):
            asyncio.run(name_folders(env.store, env.config, scanned['handoff']['k2'], images=[], folder_images=[],
                                     knowledge_root=second))
        with pytest.raises(ConfigurationError, match='stage argument does not match configured root'):
            asyncio.run(observe_images(env.store, env.config, {}, images=[], folder_images=[], image_embeddings=[],
                                       embedder_revision='r', dim=4, knowledge_root=second))
        # the link spelling and its target are the same physical root: the check passes (nothing else is asked of it here)
        asyncio.run(name_folders(env.store, env.config, scanned['handoff']['k2'], images=[], folder_images=[],
                                 knowledge_root=first))
    finally:
        drop_link(link)


def test_kb_sync_hands_k0_the_one_physical_root(k2_env, monkeypatch):
    """K0's low-level `sync` keeps a `Path.resolve()` fallback for direct callers (K0 tests, pinned API); the
    production caller must never rely on it: it passes the resolver's result as both the root and `_resolved_root`."""
    import asyncio

    from prompt_enhancer import kb_sync

    env = k2_env
    seen = {}
    real = kb_sync.sync

    def spy(store, root, **kwargs):  # labelled spy: records the arguments, runs the real scan
        seen.update(root=root, resolved=kwargs.get('_resolved_root'))
        return real(store, root, **kwargs)

    monkeypatch.setattr(kb_sync, 'sync', spy)
    asyncio.run(kb_sync.sync_knowledge(env.config, project_root=env.root, plan=True))
    expected = resolve_knowledge_root(env.config, env.root, require_dir=True)
    assert seen['root'] == expected and seen['resolved'] == expected


def test_dev_compare_hands_k0_the_one_physical_root(k2_env, monkeypatch):
    """The same rule for `embed dev-compare`; the labelled spy stops the run right after K0's call."""
    from prompt_enhancer import k0_sync
    from prompt_enhancer.embed_server import dev_compare

    env = k2_env
    seen = {}

    class Stop(Exception):
        pass

    def spy(store, root, **kwargs):
        seen.update(root=root, resolved=kwargs.get('_resolved_root'))
        raise Stop

    monkeypatch.setattr(k0_sync, 'sync', spy)  # `run_dev_compare` imports it at call time
    out = env.root / 'dev-compare-out'
    with pytest.raises(Stop):
        dev_compare.run_dev_compare(env.config, env.root, out, 2)
    expected = resolve_knowledge_root(env.config, env.root, require_dir=True)
    assert seen['root'] == expected and seen['resolved'] == expected


@pytest.mark.parametrize('stage', ['k3', 'k4'])
@pytest.mark.parametrize('container', ['store', 'data_root'])
def test_k3_and_k4_refuse_a_root_that_overlaps_the_store_or_the_data_root(k2_env, stage, container):
    """K3 and K4 never read the knowledge base; they take the one physical root only to prove that no owned path
    overlaps it (their own overlap loop uses it, and the resolver refuses first)."""
    import asyncio

    from prompt_enhancer.k3_cards import build_cards
    from prompt_enhancer.k4_aliases import _inputs

    env = k2_env
    # a root equal to the configured path, or above it
    env.config['knowledge']['root'] = env.config['storage']['kb_store' if container == 'store' else 'data_root']
    with pytest.raises(ConfigurationError, match=r'knowledge\.root'):
        if stage == 'k3':
            asyncio.run(build_cards(env.store, env.config, {}, observations=[], images=[], folder_images=[],
                                    snapshot_id='snap', overrides={}))
        else:
            _inputs(env.store, env.config, {}, [], {})


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(target=st.sampled_from(['kb', 'database', 'models', 'models/x', 'data/kb', '.venv-gpu', '.', 'models2']),
       side=st.sampled_from(['project', 'external']), below=st.booleans())
def test_a_root_spelled_through_a_link_gets_the_verdict_of_its_target(area, target, side, below):
    project, external = area
    (project / target / 'sub').mkdir(parents=True, exist_ok=True)
    link = (project if side == 'project' else external) / 'generated-link'
    make_link(link, (project / target).resolve())
    try:
        spelled = link / 'sub' if below else link
        physical = Path(os.path.realpath(spelled))

        def verdict(path):
            try:
                resolve(project, path)
                return False
            except ConfigurationError:
                return True

        assert verdict(spelled) == verdict(physical)
    finally:
        drop_link(link)
