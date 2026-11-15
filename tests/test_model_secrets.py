import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from prompt_enhancer.model_secrets import load_api_key


def test_synthetic_credential_and_path_refusal(tmp_path):
    directory = tmp_path / '.secrets'
    directory.mkdir()
    (directory / 'fake.key').write_text('synthetic-token\n', encoding='utf-8')
    assert load_api_key(tmp_path, '.secrets/fake.key') == 'synthetic-token'
    for path in ('../fake.key', '.secrets/../fake.key', '/fake.key', '.secrets\\fake.key', 'other/fake.key'):
        with pytest.raises(ValueError):
            load_api_key(tmp_path, path)
    for invalid in ('', 'first\nsecond'):
        (directory / 'fake.key').write_text(invalid, encoding='utf-8')
        with pytest.raises(ValueError):
            load_api_key(tmp_path, '.secrets/fake.key')


@pytest.mark.parametrize('target_location', ['inside', 'outside', 'missing'])
def test_missing_and_linked_credential_refused(tmp_path, target_location):
    directory = tmp_path / '.secrets'
    directory.mkdir()
    with pytest.raises(ValueError, match='api_key_file.*missing or unreadable'):
        load_api_key(tmp_path, '.secrets/missing')
    (directory / 'target').write_text('synthetic', encoding='utf-8')
    (tmp_path / 'outside').write_text('synthetic', encoding='utf-8')
    target = {'inside': directory / 'target', 'outside': tmp_path / 'outside',
              'missing': tmp_path / 'absent'}[target_location]
    try:
        (directory / 'link').symlink_to(target)
    except OSError:
        pytest.skip('symbolic link creation unavailable')
    with pytest.raises(ValueError, match='api_key_file.*link'):
        load_api_key(tmp_path, '.secrets/link')


def test_configured_link_refused_before_read(tmp_path, monkeypatch):
    directory = tmp_path / '.secrets'
    directory.mkdir()
    candidate = directory / 'link'
    candidate.write_text('synthetic', encoding='utf-8')
    original = os.lstat
    monkeypatch.setattr(os, 'lstat', lambda path, *args, **kwargs: SimpleNamespace(st_mode=stat.S_IFLNK, st_nlink=1)
                        if Path(path) == candidate else original(path, *args, **kwargs))
    with pytest.raises(ValueError, match='api_key_file.*link'):
        load_api_key(tmp_path, '.secrets/link')


def test_hard_link_refused_before_read(tmp_path, monkeypatch):
    directory = tmp_path / '.secrets'
    directory.mkdir()
    target = tmp_path / 'target'
    target.write_text('synthetic', encoding='utf-8')
    os.link(target, directory / 'link')
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: pytest.fail('credential was read'))
    with pytest.raises(ValueError, match='api_key_file.*hard links'):
        load_api_key(tmp_path, '.secrets/link')


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction API unavailable')
def test_junction_refused_before_read(tmp_path, monkeypatch):
    import _winapi

    directory = tmp_path / '.secrets'
    directory.mkdir()
    target = tmp_path / 'target'
    target.mkdir()
    junction = directory / 'link'
    _winapi.CreateJunction(str(target), str(junction))
    try:
        monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: pytest.fail('credential was read'))
        with pytest.raises(ValueError, match='api_key_file.*link'):
            load_api_key(tmp_path, '.secrets/link')
    finally:
        os.rmdir(junction)
