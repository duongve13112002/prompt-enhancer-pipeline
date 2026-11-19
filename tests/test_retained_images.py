"""Retained image contract with tiny synthetic sources and labelled doubles."""
import hashlib
import os
import subprocess

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from k2_fixtures import folder, image_bytes
from kb_sync_fixtures import SyncServer, invoke
from prompt_enhancer.kb_sync import retain_images
from prompt_enhancer.storage import StorageError
from prompt_enhancer.config import ConfigurationError, validate_sync_config


class BuilderDouble:
    def __init__(self):
        self.files = {}

    def add_file(self, name, data):
        assert name not in self.files
        self.files[name] = data


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.binary(max_size=200), duplicates=st.integers(min_value=1, max_value=6))
def test_duplicates_copy_once_and_exact_bytes(tmp_path, data, duplicates):
    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
    (tmp_path / 'a').mkdir(exist_ok=True)
    (tmp_path / 'a' / 'x.png').write_bytes(data)
    rows = {'images': [{'image_id': image_id, 'size_bytes': len(data)}],
            'folder_images': [{'image_id': image_id, 'folder_id': 'a', 'relative_path': 'x.png'}] * duplicates}
    builder = BuilderDouble()
    retain_images(builder, tmp_path, rows, {'max_image_bytes': 201, 'max_total_bytes': 201})
    assert builder.files['images/' + image_id[7:]] == data
    assert len(builder.files) == 2


def test_publish_pins_original_after_source_changes(k2_env):
    source = folder(k2_env, 'f', 'Name\ndescription') / '0.png'
    original = source.read_bytes()
    result = invoke(k2_env, SyncServer())
    with k2_env.store.pin() as pin:
        source.write_bytes(image_bytes((2, 3, 4)))
        image = pin.read_rows('images')[0]
        assert pin.read_image(image['image_id']) == original
        assert result['snapshot_id'] == pin.snapshot_id


@pytest.mark.parametrize('limit', ['max_image_bytes', 'max_total_bytes'])
def test_limits_refuse_without_changing_current(k2_env, limit):
    folder(k2_env, 'f', 'Name\ndescription')
    invoke(k2_env, SyncServer())
    current = k2_env.store.current()
    k2_env.config['sync']['retained_images'][limit] = 1
    with pytest.raises(StorageError, match='byte limit'):
        invoke(k2_env, SyncServer())
    assert k2_env.store.current() == current


@pytest.mark.parametrize('limit', ['max_image_bytes', 'max_total_bytes'])
@pytest.mark.parametrize('value', [None, 'deploy_required', True, 0, -1, 1.5, 2**80])
def test_invalid_limits_refused(k2_env, limit, value):
    if value is None:
        del k2_env.config['sync']['retained_images'][limit]
    else:
        k2_env.config['sync']['retained_images'][limit] = value
    with pytest.raises(ConfigurationError):
        validate_sync_config(k2_env.config, project_root=k2_env.root)


def test_mutated_source_fails_copy(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'x').write_bytes(b'other')
    image_id = 'sha256:' + hashlib.sha256(b'hello').hexdigest()
    rows = {'images': [{'image_id': image_id, 'size_bytes': 5}],
            'folder_images': [{'image_id': image_id, 'folder_id': 'a', 'relative_path': 'x'}]}
    with pytest.raises(StorageError, match='source missing, changed, or unsafe'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': 5, 'max_total_bytes': 5})


def test_incorrect_scanned_size_refused(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'x').write_bytes(b'hello')
    image_id = 'sha256:' + hashlib.sha256(b'hello').hexdigest()
    rows = {'images': [{'image_id': image_id, 'size_bytes': 6}],
            'folder_images': [{'image_id': image_id, 'folder_id': 'a', 'relative_path': 'x'}]}
    with pytest.raises(StorageError, match='source missing, changed, or unsafe'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': 6, 'max_total_bytes': 6})


def test_tampered_snapshot_refused(k2_env):
    folder(k2_env, 'f', 'Name\ndescription')
    invoke(k2_env, SyncServer())
    with k2_env.store.pin() as pin:
        image_id = pin.read_rows('images')[0]['image_id']
        path = k2_env.store.path(f'{pin._directory}/images/{image_id[7:]}')
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with pytest.raises(StorageError):
            pin.read_image(image_id)


@pytest.mark.parametrize('bad', ['', 'sha256:', 'sha256:' + 'g' * 64, 'sha256:' + 'A' * 64,
                                 'sha256:' + '0' * 63, '../escape'])
def test_invalid_image_identity_refused(k2_env, bad):
    folder(k2_env, 'f', 'Name\ndescription')
    invoke(k2_env, SyncServer())
    with k2_env.store.pin() as pin, pytest.raises(StorageError, match='invalid retained image identity'):
        pin.read_image(bad)


def test_source_escape_refused(tmp_path):
    rows = {'images': [{'image_id': 'sha256:' + hashlib.sha256(b'outside').hexdigest(), 'size_bytes': 7}],
            'folder_images': [{'image_id': 'sha256:' + hashlib.sha256(b'outside').hexdigest(),
                               'folder_id': '..', 'relative_path': 'outside'}]}
    with pytest.raises(StorageError, match='unsafe'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': 7, 'max_total_bytes': 7})


def dir_link(link, target):
    """A directory link without privilege: a junction on Windows, a symlink on POSIX; skip only if impossible."""
    if os.name == 'nt':
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True,
                              creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if done.returncode != 0:
            pytest.skip('junction creation unavailable: ' + done.stderr.decode(errors='replace'))
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            pytest.skip('symbolic link creation unavailable')


def file_link(link, target):
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip('file symbolic link creation unavailable')


def drop_link(link):
    """Remove a directory link as a link, never through it."""
    os.rmdir(link) if os.name == 'nt' else os.unlink(link)


def rows_for(image_id, size, folder_id, relative_path):
    return {'images': [{'image_id': image_id, 'size_bytes': size}],
            'folder_images': [{'image_id': image_id, 'folder_id': folder_id, 'relative_path': relative_path}]}


def limits_for(size):
    return {'max_image_bytes': size, 'max_total_bytes': size}


@pytest.fixture
def outside(tmp_path):
    """Bytes outside the source root that a followed link would copy; the rows below claim them as images."""
    folder_ = tmp_path / 'outside'
    (folder_ / 'b').mkdir(parents=True)
    (folder_ / 'b' / 'x').write_bytes(b'outside')
    (folder_ / 'file').write_bytes(b'outside')
    return folder_, 'sha256:' + hashlib.sha256(b'outside').hexdigest()


@pytest.mark.parametrize('where', ['folder', 'nested_ancestor', 'source_root_ancestor'])
def test_directory_link_refused_on_every_source_component(tmp_path, outside, where):
    """Junction on Windows, symlink on POSIX: a link on the folder, an ancestor inside the source or above it."""
    target, image_id = outside
    source = tmp_path / 'source'
    source.mkdir()
    if where == 'folder':
        dir_link(source / 'a', target / 'b')
        rows, root = rows_for(image_id, 7, 'a', 'x'), source
    elif where == 'nested_ancestor':
        dir_link(source / 'a', target)
        rows, root = rows_for(image_id, 7, 'a/b', 'x'), source
    else:
        dir_link(tmp_path / 'viaroot', tmp_path)
        (tmp_path / 'real').mkdir()
        (tmp_path / 'real' / 'a').mkdir()
        (tmp_path / 'real' / 'a' / 'x').write_bytes(b'outside')
        rows, root = rows_for(image_id, 7, 'a', 'x'), tmp_path / 'viaroot' / 'real'
    builder = BuilderDouble()
    with pytest.raises(StorageError, match='unsafe'):
        retain_images(builder, root, rows, limits_for(7))
    assert builder.files == {}


def test_file_symlink_refused(tmp_path, outside):
    target, image_id = outside
    source = tmp_path / 'source'
    (source / 'a').mkdir(parents=True)
    file_link(source / 'a' / 'x', target / 'file')
    with pytest.raises(StorageError, match='unsafe'):
        retain_images(BuilderDouble(), source, rows_for(image_id, 7, 'a', 'x'), limits_for(7))


def test_publish_refuses_link_and_keeps_current(k2_env, tmp_path):
    """Publish path: a source folder replaced by a link after a good publish refuses and CURRENT does not move."""
    import shutil
    folder(k2_env, 'f', 'Name\ndescription', count=2)
    invoke(k2_env, SyncServer())
    current = k2_env.store.current()
    real = k2_env.source / 'f'
    moved = tmp_path / 'moved'
    shutil.move(real, moved)
    dir_link(real, moved)
    try:
        with pytest.raises(StorageError):
            invoke(k2_env, SyncServer())
    finally:
        drop_link(real)
    assert k2_env.store.current() == current


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.binary(min_size=1, max_size=128), extra=st.integers(min_value=0, max_value=2))
def test_exact_limit_boundaries(tmp_path, data, extra):
    (tmp_path / '猫').mkdir(exist_ok=True)
    (tmp_path / '猫' / 'ảnh.png').write_bytes(data)
    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
    rows = {'images': [{'image_id': image_id, 'size_bytes': len(data)}],
            'folder_images': [{'image_id': image_id, 'folder_id': '猫', 'relative_path': 'ảnh.png'}]}
    builder = BuilderDouble()
    retain_images(builder, tmp_path, rows,
                  {'max_image_bytes': len(data) + extra, 'max_total_bytes': len(data) + extra})
    assert builder.files['images/' + image_id[7:]] == data
    if len(data) > 1:
        with pytest.raises(StorageError, match='byte limit'):
            retain_images(BuilderDouble(), tmp_path, rows,
                          {'max_image_bytes': len(data) - 1, 'max_total_bytes': len(data)})
        with pytest.raises(StorageError, match='byte limit'):
            retain_images(BuilderDouble(), tmp_path, rows,
                          {'max_image_bytes': len(data), 'max_total_bytes': len(data) - 1})


def test_publish_select_attach_and_hold_after_source_removal(k2_env, model_test_profile, tmp_path, project_root, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from assembly_fixtures import assembly_config, entity, intake_view, ir, link, plan
    from prompt_enhancer.assembly import build_assembly
    from prompt_enhancer.pipeline import retain_attachments

    source = folder(k2_env, 'f', 'Name\ndescription') / '0.png'
    original = source.read_bytes()
    invoke(k2_env, SyncServer())
    source.unlink()
    config = assembly_config(model_test_profile, tmp_path)
    config['edit']['attach_kb_reference_images'] = True
    config['assembly']['default_outfit'] = 'include'
    with k2_env.store.pin() as pin:
        concept = pin.read_rows('concepts')[0]['concept_id']
        read_rows = pin.read_rows

        def labelled_stable_card(name):
            rows = read_rows(name)
            if name == 'cards':
                for row in rows:
                    for facet in row['facets']:
                        facet['stability'] = 'stable'  # Labelled K3 double for a one-image fixture.
            return rows

        monkeypatch.setattr(pin, 'read_rows', labelled_stable_card)
        view = intake_view(config, pin, task='edit')
        view.knowledge.store = k2_env.store
        view.profile = SimpleNamespace(manifest={'renderer': {'accepts_reference_images': True}})
        intent = ir([link(concept, 'subject_identity', 'e1')], [(concept, ['apparel.color'])],
                    [entity('e1', 'Name')], task='edit')
        assembled = build_assembly(view, plan(intent), project_root=project_root)
        assert len(assembled['pack']['images']) == 1
        attachments = retain_attachments(view, assembled, datetime.now(timezone.utc) + timedelta(days=1))
        assert list(attachments.values()) == [original]
        k2_env.store.release_hold(pin.snapshot_id, view.payload['request_id'])


def test_sync_identity_ignores_scan_order_and_includes_limits(k2_env):
    from copy import deepcopy
    from k2_fixtures import scan
    from prompt_enhancer.kb_sync import sync_identity

    folder(k2_env, 'a', 'Name\ndescription', count=2)
    folder(k2_env, 'b', 'Other\ndescription')
    scanned = scan(k2_env)
    first = sync_identity(k2_env.config, scanned, {}, k2_env.store)
    shuffled = deepcopy(scanned)
    for name in ('folders', 'images', 'folder_images'):
        shuffled['rows'][name].reverse()
    assert sync_identity(k2_env.config, shuffled, {}, k2_env.store) == first
    k2_env.config['sync']['retained_images']['max_total_bytes'] += 1
    assert sync_identity(k2_env.config, shuffled, {}, k2_env.store) != first


def test_published_copies_pass_attachment_intake(k2_env, model_test_profile, tmp_path, project_root):
    import base64
    from assembly_fixtures import assembly_config
    from intake_fixtures import activate
    from prompt_enhancer.intake import intake

    folder(k2_env, 'f', 'Name\ndescription')
    invoke(k2_env, SyncServer())
    config = assembly_config(model_test_profile, tmp_path)
    config['storage']['kb_store'] = str(k2_env.store.root)
    config['edit']['attach_kb_reference_images'] = True
    activate(config, project_root)
    request = {'task': 'edit', 'prompt': 'edit', 'source_image': base64.b64encode(image_bytes()).decode()}
    with intake(request, config, project_root=project_root) as result:
        assert result.payload['outcome'] == 'ready', result.payload['error']
        assert result.snapshot.snapshot_id == k2_env.store.current()[0]


def test_extension_override_controls_retained_images(k2_env):
    source = folder(k2_env, 'f', 'Name\ndescription') / '0.png'
    source.rename(source.with_suffix('.other'))
    k2_env.config['knowledge']['image_extensions'] = ['.other']
    invoke(k2_env, SyncServer())
    with k2_env.store.pin() as pin:
        rows = pin.read_rows('images')
        assert len(rows) == 1
        assert pin.read_image(rows[0]['image_id']) == image_bytes((0, 30, 50))


def test_vision_transport_does_not_change_retained_original(k2_env):
    source = folder(k2_env, 'f', 'Name\ndescription') / '0.png'
    original = source.read_bytes()
    k2_env.config['vision'].update(format='jpeg', max_image_side=2)
    invoke(k2_env, SyncServer())
    with k2_env.store.pin() as pin:
        assert pin.read_image(pin.read_rows('images')[0]['image_id']) == original


@pytest.mark.parametrize('bad', [None, [], ['png'], ['../png'], 'deploy_required'])
def test_invalid_extensions_refused(k2_env, bad):
    if bad is None:
        del k2_env.config['knowledge']['image_extensions']
    else:
        k2_env.config['knowledge']['image_extensions'] = bad
    with pytest.raises(ConfigurationError):
        validate_sync_config(k2_env.config, project_root=k2_env.root)


# ----------------------------------------------------------------------------- unique-total accounting (decision 161)


def source_with(tmp_path, payloads, multiplicity):
    """Each distinct payload stored `multiplicity[i]` times in different folders; rows as the K0 scan gives them."""
    rows = {'images': [], 'folder_images': []}
    for index, data in enumerate(payloads):
        image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
        rows['images'].append({'image_id': image_id, 'size_bytes': len(data)})
        for copy in range(multiplicity[index]):
            (tmp_path / f'f{index}_{copy}').mkdir(exist_ok=True)
            (tmp_path / f'f{index}_{copy}' / 'x.png').write_bytes(data)
            rows['folder_images'].append({'image_id': image_id, 'folder_id': f'f{index}_{copy}',
                                          'relative_path': 'x.png'})
    return rows


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(payloads=st.lists(st.binary(min_size=1, max_size=40), min_size=1, max_size=4, unique=True),
       multiplicity=st.lists(st.integers(min_value=1, max_value=3), min_size=4, max_size=4))
def test_unique_total_is_sum_of_distinct_sizes(tmp_path, payloads, multiplicity):
    rows = source_with(tmp_path, payloads, multiplicity)
    total = sum(len(data) for data in payloads)
    biggest = max(len(data) for data in payloads)
    builder = BuilderDouble()
    retain_images(builder, tmp_path, rows, {'max_image_bytes': biggest, 'max_total_bytes': total})
    assert len(builder.files) == len(payloads) + 1  # one copy per distinct image plus the marker
    with pytest.raises(StorageError, match='byte limit'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': biggest, 'max_total_bytes': total - 1})


def test_publish_unique_total_boundary_keeps_current_and_cleans_up(k2_env):
    """Two distinct images plus a byte-identical copy in another folder; sum of distinct sizes is the exact limit."""
    first = folder(k2_env, 'a', 'Name\ndescription', count=2)
    folder(k2_env, 'b', 'Other\ndescription', count=1, payload=(first / '0.png').read_bytes())
    distinct = {(first / '0.png').read_bytes(), (first / '1.png').read_bytes()}
    assert len(distinct) == 2
    total = sum(len(data) for data in distinct)
    k2_env.config['sync']['retained_images'].update(max_image_bytes=total, max_total_bytes=total)
    invoke(k2_env, SyncServer())
    with k2_env.store.pin() as pin:
        assert sum(row['size_bytes'] for row in pin.read_rows('images')) == total
        assert len(pin.read_rows('images')) == 2
    current = k2_env.store.current()
    k2_env.config['sync']['retained_images']['max_total_bytes'] = total - 1
    with pytest.raises(StorageError, match='byte limit'):
        invoke(k2_env, SyncServer())
    assert k2_env.store.current() == current
    k2_env.config['sync']['retained_images']['max_total_bytes'] = total + 1
    assert invoke(k2_env, SyncServer())['status'] == 'published'
    assert not list(k2_env.store.root.rglob('.building-*'))  # the failed build's partial copies are gone


# ----------------------------------------------------------------------------- validators


def valid_rows(tmp_path, data=b'hello world'):
    (tmp_path / 'a').mkdir(exist_ok=True)
    (tmp_path / 'a' / 'x').write_bytes(data)
    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
    return rows_for(image_id, len(data), 'a', 'x'), image_id


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=60)
@given(data=st.binary(min_size=1, max_size=64),
       bad=st.sampled_from(['', 'sha256:', 'sha256:' + '0' * 63, 'sha256:' + '0' * 65, 'md5:' + '0' * 64,
                            'sha256:' + 'g' * 64, None, 5, b'sha256:' + b'0' * 64]))
def test_malformed_ids_refused(tmp_path, data, bad):
    rows, _ = valid_rows(tmp_path, data)
    rows['images'][0]['image_id'] = bad
    with pytest.raises(StorageError, match='invalid scan identity or size'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': 64, 'max_total_bytes': 64})


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=60)
@given(data=st.binary(min_size=1, max_size=64))
def test_uppercase_hex_id_refused(tmp_path, data):
    rows, image_id = valid_rows(tmp_path, data)
    upper = 'sha256:' + image_id[7:].upper()
    assume(upper != image_id)  # a digest of digits only is its own uppercase
    rows['images'][0]['image_id'] = upper
    with pytest.raises(StorageError, match='invalid scan identity or size'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': 64, 'max_total_bytes': 64})


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=60)
@given(data=st.binary(min_size=1, max_size=64),
       bad=st.one_of(st.integers(max_value=-1), st.sampled_from([True, False, 1.0, 2.5, '5', None, b'5'])))
def test_negative_bool_and_non_integer_sizes_refused(tmp_path, data, bad):
    rows, _ = valid_rows(tmp_path, data)
    rows['images'][0]['size_bytes'] = bad
    with pytest.raises(StorageError, match='invalid scan identity or size'):
        retain_images(BuilderDouble(), tmp_path, rows, {'max_image_bytes': 64, 'max_total_bytes': 64})


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@given(data=st.binary(min_size=1, max_size=64))
def test_duplicate_ids_and_empty_membership_refused(tmp_path, data):
    rows, _ = valid_rows(tmp_path, data)
    duplicated = dict(rows, images=rows['images'] * 2)
    with pytest.raises(StorageError, match='invalid scan identity or size'):
        retain_images(BuilderDouble(), tmp_path, duplicated, {'max_image_bytes': 64, 'max_total_bytes': 128})
    unlocated = dict(rows, folder_images=[])
    with pytest.raises(StorageError, match='source missing, changed, or unsafe'):
        retain_images(BuilderDouble(), tmp_path, unlocated, {'max_image_bytes': 64, 'max_total_bytes': 64})


# ----------------------------------------------------------------------------- intake and pre-S4 (3 images)


def attach_setup(k2_env, model_test_profile, tmp_path, project_root, *, count=3):
    """A really published knowledge base of `count` distinct images and a ready edit request with attachments on."""
    import base64
    from assembly_fixtures import assembly_config
    from intake_fixtures import activate

    folder(k2_env, 'f', 'Name\ndescription', count=count)
    invoke(k2_env, SyncServer())
    config = assembly_config(model_test_profile, tmp_path)
    config['storage']['kb_store'] = str(k2_env.store.root)
    config['edit']['attach_kb_reference_images'] = True
    activate(config, project_root)
    request = {'task': 'edit', 'prompt': 'edit', 'source_image': base64.b64encode(image_bytes()).decode()}
    with k2_env.store.pin() as pin:
        ids = sorted(row['image_id'] for row in pin.read_rows('images'))
        directory = pin._directory
    assert len(ids) == count
    return config, request, ids, directory


def run_intake(config, request, project_root):
    from prompt_enhancer.intake import intake

    with intake(request, config, project_root=project_root) as result:
        return result.payload, result.snapshot is not None


def assert_kb_sync_refusal(payload, store):
    from prompt_enhancer.intake import RETENTION_MESSAGE

    assert payload['outcome'] == 'refused'
    assert payload['error'] == {'code': 'KNOWLEDGE_FAILED', 'message': RETENTION_MESSAGE}
    assert 'kb sync' in payload['error']['message']
    assert not list((store.root / 'leases').glob('*/*.lease'))


@pytest.mark.parametrize('damage', ['last_copy_removed', 'marker_removed', 'other_layout', 'unlisted_row',
                                    'row_size_differs'])
def test_intake_refuses_damaged_retention_before_s4(k2_env, model_test_profile, tmp_path, project_root, monkeypatch,
                                                   damage):
    """Three images; the damaged one is last in image_id order, so a loop that stops at the first row fails here."""
    from prompt_enhancer.storage.store import PinnedSnapshot

    if damage == 'other_layout':
        monkeypatch.setattr('prompt_enhancer.kb_sync.RETAINED_IMAGE_MARKER', ('images/retention-v1', b'original-sha256-v9'))
    config, request, ids, directory = attach_setup(k2_env, model_test_profile, tmp_path, project_root)
    if damage == 'last_copy_removed':
        k2_env.store.path(f'{directory}/images/{ids[-1][7:]}').unlink()
    elif damage == 'marker_removed':
        k2_env.store.path(f'{directory}/images/retention-v1').unlink()
    elif damage in ('unlisted_row', 'row_size_differs'):
        read_rows = PinnedSnapshot.read_rows

        def labelled_damaged_table(self, name):  # test-only double of a table that disagrees with the manifest
            rows = read_rows(self, name)
            if name == 'images':
                rows = sorted(rows, key=lambda row: row['image_id'])
                if damage == 'unlisted_row':
                    rows.append(dict(rows[-1], image_id='sha256:' + 'e' * 64))
                else:
                    rows[-1] = dict(rows[-1], size_bytes=rows[-1]['size_bytes'] + 1)
            return rows

        monkeypatch.setattr(PinnedSnapshot, 'read_rows', labelled_damaged_table)
    payload, pinned = run_intake(config, request, project_root)
    assert_kb_sync_refusal(payload, k2_env.store)
    assert not pinned


def test_manifest_copy_entry_whose_hash_differs_from_its_id_is_refused_at_intake(k2_env, model_test_profile, tmp_path,
                                                                                project_root, monkeypatch):
    """KBS-F3-F3: a listed `images/<hex>` file is a copy of image <hex> only if the manifest's own hash says so."""
    from prompt_enhancer.storage.store import KnowledgeStore

    config, request, ids, directory = attach_setup(k2_env, model_test_profile, tmp_path, project_root)
    real = KnowledgeStore._manifest

    def forged_manifest(self, relative, expected_sha=None):  # test-only double: one entry carries another hash
        rows = real(self, relative, expected_sha)
        for row in rows:
            if row['path'] == 'images/' + ids[-1][7:]:
                row['sha256'] = '0' * 64
        return rows

    monkeypatch.setattr(KnowledgeStore, '_manifest', forged_manifest)
    with k2_env.store.pin() as pin, pytest.raises(StorageError, match='not retained in this snapshot'):
        pin.retained_image_entry(ids[-1])
    payload, pinned = run_intake(config, request, project_root)
    assert_kb_sync_refusal(payload, k2_env.store)
    assert not pinned


def grown_copy_setup(k2_env, tmp_path):
    folder(k2_env, 'f', 'Name\ndescription')
    invoke(k2_env, SyncServer())
    pin = k2_env.store.pin()
    image_id = pin.read_rows('images')[0]['image_id']
    return pin, image_id, k2_env.store.path(f'{pin._directory}/images/{image_id[7:]}')


def test_retained_copy_larger_than_its_manifest_size_is_refused_without_reading_it(k2_env, tmp_path, monkeypatch):
    """KBS-F3-F3: the copy read is bounded by the manifest size; a larger file is refused before any byte is read."""
    pin, image_id, path = grown_copy_setup(k2_env, tmp_path)
    with pin:
        size = path.stat().st_size
        path.write_bytes(path.read_bytes() + b'more')
        real, reads = os.read, []
        monkeypatch.setattr(os, 'read', lambda fd, count: reads.append(count) or real(fd, count))
        with pytest.raises(StorageError, match='retained file unavailable') as refused:
            pin.read_image(image_id)
        assert path.stat().st_size == size + 4 and reads == []  # refused on the open handle's size, nothing read
        assert str(path) not in str(refused.value)  # R-014: no machine path in the diagnostic


def test_retained_copy_growing_during_the_read_is_never_read_past_its_bound(k2_env, tmp_path, monkeypatch):
    from bounded_read_fixtures import grow_after_first_read

    pin, image_id, path = grown_copy_setup(k2_env, tmp_path)
    with pin:
        size = path.stat().st_size
        received = grow_after_first_read(monkeypatch, path, 4096)
        with pytest.raises(StorageError, match='retained file unavailable'):
            pin.read_image(image_id)
        assert received[0] == size and sum(received) <= size + 1  # at most one byte beyond the manifest size


def test_intake_accepts_three_published_images_and_never_reads_image_bytes(k2_env, model_test_profile, tmp_path,
                                                                           project_root, monkeypatch):
    from prompt_enhancer.storage.store import PinnedSnapshot

    config, request, ids, directory = attach_setup(k2_env, model_test_profile, tmp_path, project_root)
    k2_env.store.path(f'{directory}/images/{ids[-1][7:]}')  # exists
    reads = []
    read_file = PinnedSnapshot.read_file
    monkeypatch.setattr(PinnedSnapshot, 'read_file', lambda self, name: reads.append(name) or read_file(self, name))
    payload, pinned = run_intake(config, request, project_root)
    assert payload['outcome'] == 'ready' and pinned
    assert reads == ['images/retention-v1']  # D1: intake reads the marker only, never an image


def test_same_size_corruption_is_caught_after_s3_for_selected_images_only(k2_env, model_test_profile, tmp_path,
                                                                          project_root):
    from prompt_enhancer.intake import AttachmentRetentionUnavailable, intake
    from prompt_enhancer.pipeline import require_attachment_copies

    config, request, ids, directory = attach_setup(k2_env, model_test_profile, tmp_path, project_root)
    path = k2_env.store.path(f'{directory}/images/{ids[-1][7:]}')
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    pack = lambda *wanted: {'pack': {'images': [{'channel': 'attach', 'image_id': i} for i in wanted]
                                     + [{'channel': 'verbalize', 'image_id': ids[-1]}]}}
    with intake(request, config, project_root=project_root) as s0:
        assert s0.payload['outcome'] == 'ready'  # manifest-only intake cannot see same-size damage
        require_attachment_copies(s0, pack(ids[0], ids[1]))  # unselected damaged copy: no read, no refusal
        require_attachment_copies(s0, None)
        with pytest.raises(AttachmentRetentionUnavailable, match='kb sync'):
            require_attachment_copies(s0, pack(ids[0], ids[-1]))


@pytest.mark.parametrize('damage', ['missing', 'wrong_size'])
def test_damaged_k5_index_file_is_reported_as_an_index_problem_not_as_missing_image_copies(
        k2_env, model_test_profile, tmp_path, project_root, damage):
    """KBS-F3-F3: a K5 index file the manifest lists has its own fixed message; it must not tell the operator that image
    copies are missing, and it must not carry the machine path."""
    from prompt_enhancer.intake import INDEX_DAMAGE_MESSAGE, RETENTION_MESSAGE

    config, request, ids, directory = attach_setup(k2_env, model_test_profile, tmp_path, project_root)
    index_file = k2_env.store.path(f'{directory}/index/exact.arrow')
    if damage == 'missing':
        index_file.unlink()
    else:
        index_file.write_bytes(index_file.read_bytes() + b'x')
    payload, pinned = run_intake(config, request, project_root)
    assert payload['outcome'] == 'refused' and not pinned
    assert payload['error'] == {'code': 'KNOWLEDGE_FAILED', 'message': INDEX_DAMAGE_MESSAGE}
    assert INDEX_DAMAGE_MESSAGE != RETENTION_MESSAGE and 'image' not in INDEX_DAMAGE_MESSAGE
    # `effective_config` is the operator's own resolved profile (intake_result.v0), so its configured paths belong there;
    # every other field, the refusal and its envelope included, must hold no machine path in either separator form.
    outside_config = {key: value for key, value in payload.items() if key != 'effective_config'}
    assert payload['effective_config'] is not None
    assert [hit for form in {str(tmp_path), tmp_path.as_posix()} for hit in _keys_holding(outside_config, form)] == []
    assert not list((k2_env.store.root / 'leases').glob('*/*.lease'))


def _keys_holding(value, text, where=()):
    """Every key path of `value` whose string holds `text`. Compares the strings themselves, not `repr`, which doubles
    Windows backslashes and so never matched a Windows path (CI window 29: the check passed only on Windows)."""
    if isinstance(value, (str, os.PathLike)):
        return [where] if text in os.fspath(value) else []
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in _keys_holding(item, text, where + (key,))]
    if isinstance(value, (list, tuple)):
        return [hit for index, item in enumerate(value) for hit in _keys_holding(item, text, where + (index,))]
    return []


def test_the_path_walk_finds_a_path_in_every_container_and_form(tmp_path):
    """The leak check above asserts an empty walk, so the walk itself must find a planted path wherever it sits."""
    planted = {'error': {'message': f'at {tmp_path}'}, 'warnings': [{'detail': tmp_path.as_posix()}],
               'pair': ('clean', tmp_path / 'index'), 'count': 3, 'none': None}
    native = _keys_holding(planted, str(tmp_path))
    assert {('error', 'message'), ('pair', 1)} <= set(native)
    found = {hit for form in {str(tmp_path), tmp_path.as_posix()} for hit in _keys_holding(planted, form)}
    assert found == {('error', 'message'), ('warnings', 0, 'detail'), ('pair', 1)}


def test_damaged_k5_index_file_is_reported_without_attachments_as_before(k2_env, model_test_profile, tmp_path,
                                                                         project_root):
    """The request that attaches nothing keeps its generic refusal label (the index message is the attach path's)."""
    config, request, ids, directory = attach_setup(k2_env, model_test_profile, tmp_path, project_root)
    config['edit']['attach_kb_reference_images'] = False
    k2_env.store.path(f'{directory}/index/exact.arrow').unlink()
    payload, pinned = run_intake(config, request, project_root)
    assert payload['error'] == {'code': 'KNOWLEDGE_FAILED', 'message': 'intake refused (RetainedFileError)'} and not pinned


def test_no_snapshot_with_attachments_on_stays_degraded(project_root, tmp_path, model_test_profile):
    """D2: with nothing published S3 selects nothing, so the request degrades instead of refusing."""
    import base64
    from assembly_fixtures import assembly_config
    from intake_fixtures import activate

    config = assembly_config(model_test_profile, tmp_path)
    config['edit']['attach_kb_reference_images'] = True
    activate(config, project_root)
    request = {'task': 'edit', 'prompt': 'edit', 'source_image': base64.b64encode(image_bytes()).decode()}
    payload, pinned = run_intake(config, request, project_root)
    assert payload['outcome'] == 'degraded' and 'SNAPSHOT_MISSING' in payload['warnings'] and not pinned


def test_retrieval_off_with_attachments_on_opens_no_store(model_test_profile, tmp_path, project_root, monkeypatch):
    """R-009: retrieval off means no knowledge-base access at all, whatever the attachment setting says."""
    import base64
    from intake_fixtures import activate, configuration

    config = configuration(model_test_profile, tmp_path)
    config['edit']['attach_kb_reference_images'] = True
    activate(config, project_root)

    def forbidden(*args, **kwargs):
        raise AssertionError('retrieval off must not touch the knowledge base')

    monkeypatch.setattr('prompt_enhancer.intake.KnowledgeGateway', forbidden)
    monkeypatch.setattr('prompt_enhancer.intake.open_knowledge_store', forbidden)
    request = {'task': 'edit', 'prompt': 'edit', 'source_image': base64.b64encode(image_bytes()).decode()}
    payload, pinned = run_intake(config, request, project_root)
    assert payload['outcome'] == 'ready' and not pinned and payload['retrieval']['state'] == 'disabled'


# ----------------------------------------------------------------------------- legacy republish, identity, dev profile


def test_legacy_current_is_republished_once_then_unchanged(k2_env, monkeypatch):
    folder(k2_env, 'f', 'Name\ndescription', count=2)
    with monkeypatch.context() as legacy:  # labelled double of the earlier publisher: no copies, old layout name
        legacy.setattr('prompt_enhancer.kb_sync.LAYOUT', 'kb-sync-v1')
        legacy.setattr('prompt_enhancer.kb_sync.retain_images',
                       lambda builder, source, rows, limits: {row['image_id'] for row in rows['images']})
        assert invoke(k2_env, SyncServer())['status'] == 'published'
    old = k2_env.store.current()
    with k2_env.store.pin() as pin, pytest.raises(StorageError, match='kb sync'):
        pin.read_image(pin.read_rows('images')[0]['image_id'])
    assert invoke(k2_env, SyncServer())['status'] == 'published'
    new = k2_env.store.current()
    assert new != old
    with k2_env.store.pin() as pin:
        pin.check_retained_images(pin.read_rows('images'))
    assert invoke(k2_env, SyncServer())['status'] == 'unchanged'
    assert k2_env.store.current() == new


def test_publish_refuses_when_images_table_differs_from_retained_copies(k2_env, monkeypatch):
    """P3-4: the table a snapshot publishes must list exactly the ids that were copied."""
    folder(k2_env, 'f', 'Name\ndescription', count=2)
    real = retain_images
    monkeypatch.setattr('prompt_enhancer.kb_sync.retain_images',  # labelled double: reports one copy fewer than made
                        lambda *args: set(sorted(real(*args))[:-1]))
    with pytest.raises(StorageError, match='table differs from retained copies'):
        invoke(k2_env, SyncServer())
    assert k2_env.store.current() is None


@pytest.mark.parametrize('replace', ['images_directory', 'copy_file'])
def test_link_inside_published_snapshot_refused_on_read(k2_env, tmp_path, replace):
    """A published copy swapped for a link (junction or symlink) is refused by the no-follow snapshot read."""
    import shutil
    folder(k2_env, 'f', 'Name\ndescription', count=2)
    invoke(k2_env, SyncServer())
    outside = tmp_path / 'moved'
    with k2_env.store.pin() as pin:
        image_id = pin.read_rows('images')[0]['image_id']
        images = k2_env.store.path(f'{pin._directory}/images')
        if replace == 'images_directory':
            shutil.move(images, outside)
            dir_link(images, outside)
            restore = lambda: (drop_link(images), shutil.move(outside, images))
        else:
            copy = images / image_id[7:]
            shutil.move(copy, outside)
            file_link(copy, outside)
            restore = lambda: (copy.unlink(), shutil.move(outside, copy))
        try:
            with pytest.raises(StorageError):
                pin.read_image(image_id)
        finally:
            restore()
        assert pin.read_image(image_id)  # restored: the refusal was the link, not a damaged copy


def test_each_limit_enters_sync_identity(k2_env):
    from k2_fixtures import scan
    from prompt_enhancer.kb_sync import sync_identity

    folder(k2_env, 'a', 'Name\ndescription')
    scanned = scan(k2_env)
    base = sync_identity(k2_env.config, scanned, {}, k2_env.store)
    for limit in ('max_image_bytes', 'max_total_bytes'):
        k2_env.config['sync']['retained_images'][limit] += 1
        assert sync_identity(k2_env.config, scanned, {}, k2_env.store) != base, limit
        k2_env.config['sync']['retained_images'][limit] -= 1
    assert sync_identity(k2_env.config, scanned, {}, k2_env.store) == base


def test_dev_example_declares_explicit_retention_limits_and_refuses_bad_ones(project_root, k2_env):
    """The dev example carries its own dev-only values; a bad override or a sentinel is still refused."""
    from pathlib import Path
    import yaml

    dev = yaml.safe_load((Path(__file__).resolve().parents[1] / 'configs' / 'enhancer.dev.example.yaml').read_text('utf-8'))
    limits = dev['sync']['retained_images']
    assert set(limits) == {'max_image_bytes', 'max_total_bytes'}
    assert all(type(value) is int and value > 0 for value in limits.values())  # explicit, not a sentinel
    k2_env.config['sync']['retained_images'] = dict(limits)
    validate_sync_config(k2_env.config, project_root=k2_env.root)  # the dev values pass the same validation
    k2_env.config['sync']['retained_images']['max_total_bytes'] = 'deploy_required'
    with pytest.raises(ConfigurationError):
        validate_sync_config(k2_env.config, project_root=k2_env.root)
