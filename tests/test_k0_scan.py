"""K0 source scanning with synthetic trees and explicitly labelled fault doubles."""

from copy import deepcopy
import os
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from prompt_enhancer import k0_scan
from prompt_enhancer.k0 import parse_info, rows_from_scan


def scan(root, previous=None):
    return k0_scan.scan_source(root, info_names=['info.txt', 'other.txt'], encoding='utf-8',
                               extensions=['.jpg', '.png'], separator='|', previous=previous or {})


def make_tree(tmp_path):
    root = tmp_path / 'source'
    folder = root / 'folder'
    folder.mkdir(parents=True)
    (folder / 'info.txt').write_text('Ａ | B\none | two', encoding='utf-8')
    (folder / 'a.JPG').write_bytes(b'image')
    return root, folder


def test_scan_ignores_nested_and_other_files_and_matches_extension(tmp_path):
    root, folder = make_tree(tmp_path)
    (folder / 'ignored.json').write_text('not data for K0')
    nested = folder / 'nested'
    nested.mkdir()
    (nested / 'secret.jpg').write_bytes(b'must not read')
    result = scan(root)
    assert result['report']['nested'] == ['folder/nested']
    assert list(result['folders']['folder']['images']) == ['a.JPG']
    rows = rows_from_scan(result['folders'], '|')
    assert [row['name_as_written'] for row in rows['folder_concepts']] == ['Ａ', 'B']


def test_duplicate_normalised_names_refuse_folder(tmp_path):
    root, folder = make_tree(tmp_path)
    (folder / 'info.txt').write_text('Ａ|a\ndescription', encoding='utf-8')
    result = scan(root)
    assert result['report']['duplicate_names'] == ['folder']
    rows = rows_from_scan(result['folders'], '|')
    assert rows['folder_concepts'] == []
    assert rows['folders'][0]['error'] == 'duplicate_names'


def test_unreadable_directory_stops_before_result(tmp_path, monkeypatch):
    root, _ = make_tree(tmp_path)
    original = k0_scan._native_open

    def deny_directory(path, parent_fd, name, directory):  # labelled fault double
        if name == 'folder' and directory:
            raise PermissionError('simulated unreadable directory')
        return original(path, parent_fd, name, directory)

    monkeypatch.setattr(k0_scan, '_native_open', deny_directory)
    with pytest.raises(k0_scan.ScanError, match='cannot safely list'):
        scan(root)


@pytest.mark.parametrize('unreadable', ['info.txt', 'a.JPG'])
def test_unreadable_file_retains_prior_record(tmp_path, monkeypatch, unreadable):
    root, folder = make_tree(tmp_path)
    previous = scan(root)['folders']
    snapshot = deepcopy(previous)
    original = k0_scan._native_open

    def deny_file(path, parent_fd, name, directory):  # labelled fault double
        if name == unreadable:
            raise PermissionError('simulated unreadable file')
        return original(path, parent_fd, name, directory)

    monkeypatch.setattr(k0_scan, '_native_open', deny_file)
    (folder / unreadable).write_bytes(b'new content')
    result = scan(root, previous)
    assert result['report']['unreadable'] == [f'folder/{unreadable}']
    key = 'info' if unreadable == 'info.txt' else 'images'
    assert result['folders']['folder'][key] == snapshot['folder'][key]
    assert rows_from_scan(result['folders'], '|') == rows_from_scan(snapshot, '|')


def test_undecodable_first_info_does_not_fall_through(tmp_path):
    root, folder = make_tree(tmp_path)
    previous = scan(root)['folders']
    (folder / 'info.txt').write_bytes(b'\xff')
    (folder / 'other.txt').write_text('different\ndescription', encoding='utf-8')
    result = scan(root, previous)
    assert result['report']['unreadable'] == ['folder/info.txt']
    assert result['folders']['folder']['info'] == previous['folder']['info']


def test_entry_swapped_between_lstat_and_open_is_not_read(tmp_path, monkeypatch):
    root, folder = make_tree(tmp_path)
    previous = scan(root)['folders']
    original = k0_scan._native_open
    replacement = folder / 'replacement.tmp'
    replacement.write_bytes(b'not the original')

    def swap_then_open(path, parent_fd, name, directory):  # labelled swap double
        if name == 'a.JPG':
            os.replace(replacement, path)
        return original(path, parent_fd, name, directory)

    monkeypatch.setattr(k0_scan, '_native_open', swap_then_open)
    result = scan(root, previous)
    assert result['report']['unreadable'] == ['folder/a.JPG']
    assert result['folders']['folder']['images']['a.JPG'] == previous['folder']['images']['a.JPG']


def test_directory_link_is_reported_and_never_entered(tmp_path):
    root, folder = make_tree(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'do-not-read.jpg').write_bytes(b'outside')
    link = folder / 'link'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)
    try:
        result = scan(root)
        assert result['report']['links'] == ['folder/link']
        assert list(result['folders']['folder']['images']) == ['a.JPG']
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            os.unlink(link)


def test_directory_swapped_for_link_is_refused_before_listing(tmp_path, monkeypatch):
    root, folder = make_tree(tmp_path)
    moved = root / 'moved'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'must-not-read.jpg').write_bytes(b'outside')
    original = k0_scan._native_open

    def swap_to_link(path, parent_fd, name, directory):  # labelled swap double
        if name == 'folder' and directory:
            os.rename(folder, moved)
            if os.name == 'nt':
                import _winapi
                _winapi.CreateJunction(str(outside), str(folder))
            else:
                os.symlink(outside, folder, target_is_directory=True)
        return original(path, parent_fd, name, directory)

    monkeypatch.setattr(k0_scan, '_native_open', swap_to_link)
    try:
        with pytest.raises(k0_scan.ScanError, match='cannot safely list'):
            scan(root)
    finally:
        if os.path.lexists(folder):
            if os.name == 'nt':
                os.rmdir(folder)
            else:
                os.unlink(folder)


def test_image_replaced_with_subdirectory_preserves_previous(tmp_path):
    root, folder = make_tree(tmp_path)
    previous = scan(root)['folders']
    (folder / 'a.JPG').unlink()
    (folder / 'a.JPG').mkdir()
    result = scan(root, previous)
    assert result['report']['nested'] == ['folder/a.JPG']
    assert result['report']['unreadable'] == ['folder/a.JPG']
    assert result['folders']['folder']['images'] == previous['folder']['images']


def test_image_replaced_with_link_preserves_previous(tmp_path):
    root, folder = make_tree(tmp_path)
    previous = scan(root)['folders']
    (folder / 'a.JPG').unlink()
    target = tmp_path / 'external'
    target.mkdir()
    link = folder / 'a.JPG'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)
    try:
        result = scan(root, previous)
        assert result['report']['links'] == ['folder/a.JPG']
        assert result['folders']['folder']['images'] == previous['folder']['images']
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            os.unlink(link)


@given(st.lists(st.text(alphabet='abcABC', min_size=1, max_size=8), min_size=2, max_size=8,
                unique_by=str.casefold), st.text(alphabet='xyz ', min_size=1, max_size=30))
def test_parser_keeps_mismatched_second_line_verbatim(names, description):
    parsed = parse_info('|'.join(names) + '\n' + description, '|')
    assert parsed['names'] == names
    assert parsed['status'] == 'description_mismatch'
    assert parsed['line2'] == description
    assert parsed['descriptions'] == []


# S42-ci3 fix round 1 (review P3-4): the POSIX capability checks judge the platform sets by name, as the store does.
K0_DIR_FD = ('open', 'stat')  # stated here, not read from the module under test
K0_FD = ('listdir',)


class PosixView:
    """Labelled double: the `os` that `k0_scan` alone sees, posing as POSIX. Its capability sets hold the native
    functions named; the functions it calls are recording doubles, as any wrapper would be."""

    def __init__(self, dir_fd=K0_DIR_FD, fd=K0_FD):
        self.name = 'posix'
        self.O_NOFOLLOW = self.O_NONBLOCK = self.O_DIRECTORY = 0
        self.supports_dir_fd = {getattr(os, name) for name in dir_fd}
        self.supports_fd = {getattr(os, name) for name in fd}
        self.calls = []

    def open(self, name, flags, dir_fd=None):
        self.calls.append(('open', name, dir_fd))
        return 7

    def stat(self, name, dir_fd=None, follow_symlinks=True):
        self.calls.append(('stat', name, dir_fd, follow_symlinks))
        return 'stat result'

    def listdir(self, fd):
        self.calls.append(('listdir', fd))
        return ['b', 'a']

    def __getattr__(self, attribute):
        return getattr(os, attribute)


K0_CALLS = {
    'open': lambda: k0_scan._native_open('p', 3, 'n', True),
    'stat': lambda: k0_scan._lstat('p', 3, 'n'),
    'listdir': lambda: k0_scan._names('p', 3),
}


def test_k0_capability_checks_are_not_fooled_by_replaced_functions(monkeypatch):
    view = PosixView()
    monkeypatch.setattr(k0_scan, 'os', view)
    assert K0_CALLS['open']() == 7
    assert K0_CALLS['stat']() == 'stat result'
    assert K0_CALLS['listdir']() == ['a', 'b']
    assert view.calls == [('open', 'n', 3), ('stat', 'n', 3, False), ('listdir', 3)]


@pytest.mark.parametrize('missing', sorted(K0_CALLS))
def test_k0_capability_checks_refuse_without_their_primitive(monkeypatch, missing):
    view = PosixView(tuple(n for n in K0_DIR_FD if n != missing), tuple(n for n in K0_FD if n != missing))
    monkeypatch.setattr(k0_scan, 'os', view)
    with pytest.raises(k0_scan.ScanError, match='unavailable'):
        K0_CALLS[missing]()
    assert view.calls == []
    for name, call in K0_CALLS.items():  # the other two still pass their own check
        if name != missing:
            call()
