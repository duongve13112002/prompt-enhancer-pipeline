"""Real directory links for tests: a junction on Windows (no privilege needed), a symlink elsewhere."""

import os
import subprocess

import pytest

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def make_link(link, target):
    """A directory link `link` -> `target`; skips the test when this machine cannot create one."""
    if os.name == 'nt':
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True,
                              creationflags=NO_WINDOW)
        made = done.returncode == 0
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
            made = True
        except OSError:
            made = False
    if not made:
        pytest.skip('a directory link cannot be created here')
    assert is_directory_link(link), f'{link} was created but is not a link: a test on it would prove nothing'


def is_directory_link(path):
    """True for a symlink and, on Windows, a junction (`os.path.islink` is false for a junction)."""
    return os.path.islink(path) or os.path.isjunction(path)


def drop_link(link):
    """Remove the link itself, never its target (a junction with rmdir, a POSIX symlink with unlink)."""
    if os.path.lexists(link):
        if os.name == 'nt':
            os.rmdir(link)
        else:
            os.unlink(link)
