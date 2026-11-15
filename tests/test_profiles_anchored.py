"""T-006-int (R-014, R-015, R-017): every profile bundle write is anchored below the project root.

T-012 wrote `manifest.yaml`, `golden_results.json`, `activation.json` and `check.lock` by name after `_inside`
resolved the bundle, so a directory swapped for a link after that check redirected the write. Now each act opens
every component from the project root relative to its verified parent. The probes swap the bundle folder for a
real link (a junction on Windows, a symlink on POSIX) to a directory outside the project root, at the last
boundary before the act (right after the anchored layer's name check: a scheduling hook, the only double besides
the labelled lock double), and assert the act is refused and nothing outside changes.
"""

import asyncio
import os
from pathlib import Path
import shutil
import uuid

import pytest
from test_storage_anchored import _dir_link, _drop_link, _is_link

from prompt_enhancer import profiles
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.profiles import check_profile, confirm_profile, register_profile, resolve_profile
from prompt_enhancer.storage import StorageError
from prompt_enhancer.storage import _anchored

from profile_fixtures import (SECTIONED, DrafterDouble, RunnerDouble, bundle, config_data, draft, install_prompts,
                              load, set_manifest, write_golden)

VICTIMS = ('manifest.yaml', 'golden_results.json', 'activation.json', 'check.lock')


@pytest.fixture(autouse=True)
def prompts(project_root):
    install_prompts(project_root)


@pytest.fixture
def outside(project_root):
    """A directory outside the project root holding a victim file for every bundle file a write could reach."""
    folder = Path(project_root).resolve().parent / f'outside-{uuid.uuid4().hex}'
    folder.mkdir()
    for name in VICTIMS:
        (folder / name).write_bytes(b'must survive')
    yield folder
    shutil.rmtree(folder)


def _unchanged(outside):
    return sorted(path.name for path in outside.iterdir()) == sorted(VICTIMS) and \
        all((outside / name).read_bytes() == b'must survive' for name in VICTIMS)


def run(coroutine):
    return asyncio.run(coroutine)


def _registered(tmp_path, project_root):
    folder = bundle(tmp_path, 'p1', SECTIONED)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile register')
    run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))
    return folder


def _checkable(tmp_path, project_root):
    folder = _registered(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    write_golden(folder, 'g1', ['c1'])
    set_manifest(folder, golden_tests=['g1'])
    return folder


class BundleSwap:
    """Move the bundle aside and put a link to `outside` in its place, once; undo it, removing links as links."""

    def __init__(self, folder, outside):
        self.folder, self.moved, self.outside = folder, folder.with_name('p1-displaced'), outside
        self.swapped, self.error = False, None

    def __call__(self):
        if self.swapped or self.error:
            return
        try:
            self.folder.rename(self.moved)
        except PermissionError as error:  # Windows: a held handle below the bundle (the bundle lock) forbids it
            self.error = error
            return
        _dir_link(self.folder, self.outside)
        self.swapped = True

    def undo(self):
        if self.swapped and _is_link(self.folder):
            _drop_link(self.folder)
            self.moved.rename(self.folder)


def _arm(monkeypatch, name, nth, swap):
    """Fire `swap` after the anchored layer's name check accepted the `nth` act on a file called `name`."""
    real, seen = _anchored.real_inside, {}

    def checked(root, relative):
        result = real(root, relative)
        leaf = relative.rsplit('/', 1)[-1]
        seen[leaf] = seen.get(leaf, 0) + 1
        if (leaf, seen[leaf]) == (name, nth):
            swap()
        return result
    monkeypatch.setattr(_anchored, 'real_inside', checked)


def _lock_elsewhere(monkeypatch, tmp_path):
    """Labelled double: the bundle lock lives outside the bundle, so no held handle stops the swap and the probe
    reaches the anchored refusal itself (with the real lock, Windows refuses the swap; see the `real` cases)."""
    place = tmp_path / 'locks'
    place.mkdir()
    real = profiles.lock_file
    monkeypatch.setattr(profiles, 'lock_file', lambda root, name, timeout_s, poll_s, create:
                        real(place.resolve(), 'check.lock', timeout_s, poll_s, create=create))


def _command(command, tmp_path, project_root):
    if command == 'register':
        config = load(tmp_path, project_root, config_data(tmp_path), 'profile register')
        return lambda: run(register_profile(config, 'p1', DrafterDouble(draft()), project_root=project_root))
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    if command == 'confirm':
        return lambda: confirm_profile(config, 'p1', {'c1': 'profile_wins'}, project_root=project_root)
    return lambda: run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))


# Each anchored act of the three commands, in order: (command, file name, nth act on that name).
_ACTS = [('register', 'manifest.yaml', 1), ('confirm', 'manifest.yaml', 1), ('check', 'activation.json', 1),
         ('check', 'golden_results.json', 1), ('check', 'activation.json', 2)]
_LOCK_OPENS = [('register', 'check.lock', 1), ('confirm', 'check.lock', 1), ('check', 'check.lock', 1)]


def _prepare(command, tmp_path, project_root):
    if command == 'register':
        return bundle(tmp_path, 'p1', SECTIONED)
    if command == 'confirm':
        return _registered(tmp_path, project_root)
    return _checkable(tmp_path, project_root)


@pytest.mark.parametrize(('command', 'name', 'nth'), _ACTS + _LOCK_OPENS)
def test_a_link_swapped_into_the_bundle_during_a_write_is_refused(tmp_path, project_root, monkeypatch, outside,
                                                                  command, name, nth):
    folder = _prepare(command, tmp_path, project_root)
    if name != 'check.lock':
        _lock_elsewhere(monkeypatch, tmp_path)
    call = _command(command, tmp_path, project_root)
    swap = BundleSwap(folder, outside)
    _arm(monkeypatch, name, nth, swap)
    try:
        with pytest.raises(StorageError) as excinfo:
            call()
        assert type(excinfo.value) is StorageError, repr(excinfo.value)
        assert swap.swapped and 'is a link' in str(excinfo.value), str(excinfo.value)
        assert _unchanged(outside)
    finally:
        swap.undo()


@pytest.mark.skipif(os.name != 'nt', reason='Windows refuses to move a directory with an open descendant')
@pytest.mark.parametrize(('command', 'name', 'nth'), _ACTS)
def test_the_held_bundle_lock_stops_the_swap_on_windows(tmp_path, project_root, monkeypatch, outside, command, name,
                                                        nth):
    """With the real bundle lock held, the swap itself is refused by the OS, and the write lands in the bundle."""
    folder = _prepare(command, tmp_path, project_root)
    swap = BundleSwap(folder, outside)
    _arm(monkeypatch, name, nth, swap)
    try:
        call = _command(command, tmp_path, project_root)
        call()
    finally:
        swap.undo()
    assert not swap.swapped and isinstance(swap.error, PermissionError)
    assert (folder / name).is_file() and _unchanged(outside)


@pytest.mark.parametrize('command', ['register', 'confirm', 'check'])
def test_a_bundle_that_is_a_link_out_of_the_project_is_refused_before_any_write(tmp_path, project_root, outside,
                                                                                 command):
    """Before the call: T-012's containment rule (`_inside`) refuses it, as before; nothing outside changes."""
    folder = _prepare(command, tmp_path, project_root)
    call = _command(command, tmp_path, project_root)
    folder.rename(folder.with_name('p1-displaced'))
    _dir_link(folder, outside)
    try:
        with pytest.raises(ConfigurationError, match='resolved outside project root'):
            call()
        assert _unchanged(outside)
    finally:
        _drop_link(folder)


# ----------------------------------------------------------------------------- Q4: segment rule (PM decision)

_SEGMENT_CLASSES = {
    'short-name alias': 'p~1',
    'not NFC': 'café',
    'control character': 'p\x01q',
    'windows-forbidden character': 'p<q',
}


@pytest.mark.parametrize('kind', sorted(_SEGMENT_CLASSES))
def test_a_profile_folder_outside_the_segment_rule_is_refused_before_any_work(tmp_path, project_root, kind):
    """PM decision pending user review (T-006-int Q4): a bundle name the anchored layer would refuse is refused by
    every profile command before any read, paid call or write, fail closed, never rewritten."""
    profile_id = _SEGMENT_CLASSES[kind]
    try:  # create it where the platform allows, so the refusal is not just a missing folder
        bundle(tmp_path, profile_id, SECTIONED)
    except OSError:
        pass
    data = config_data(tmp_path)
    drafter = DrafterDouble(draft(profile_id=profile_id))
    register_config = load(tmp_path, project_root, data, 'profile register')
    check_config = load(tmp_path, project_root, data, 'profile check')
    calls = [lambda: run(register_profile(register_config, profile_id, drafter, project_root=project_root)),
             lambda: confirm_profile(check_config, profile_id, {}, project_root=project_root),
             lambda: run(check_profile(check_config, profile_id, RunnerDouble(), project_root=project_root)),
             lambda: resolve_profile(check_config, profile_id, project_root=project_root)]
    for call in calls:
        with pytest.raises(StorageError, match='invalid path segment|device name or a short-name|not in Unicode NFC'):
            call()
    assert drafter.calls == []


def test_a_profiles_root_outside_the_segment_rule_is_refused(tmp_path, project_root):
    data = config_data(tmp_path)
    data['profiles']['root'] = str(tmp_path / 'prof~1')
    folder = tmp_path / 'prof~1' / 'p1'
    folder.mkdir(parents=True)
    (folder / 'system_prompt.txt').write_bytes(SECTIONED)
    drafter = DrafterDouble(draft())
    config = load(tmp_path, project_root, data, 'profile register')
    with pytest.raises(StorageError, match='short-name'):
        run(register_profile(config, 'p1', drafter, project_root=project_root))
    assert drafter.calls == [] and sorted(path.name for path in folder.iterdir()) == ['system_prompt.txt']


def test_bundle_writes_land_at_the_resolved_bundle(tmp_path, project_root):
    """T-012 behaviour kept: register, confirm and check write the bundle files, and nothing else, in the bundle."""
    folder = _checkable(tmp_path, project_root)
    config = load(tmp_path, project_root, config_data(tmp_path), 'profile check')
    run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        ['activation.json', 'check.lock', 'golden', 'golden_results.json', 'manifest.yaml', 'system_prompt.txt'])
    assert resolve_profile(config, 'p1', project_root=project_root).profile_id == 'p1'
