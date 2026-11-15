"""Give temporary test inputs an explicit, self-contained project root."""

from functools import partial
from pathlib import Path
import shutil

import pytest

from prompt_enhancer import load_config, load_profile


@pytest.fixture(scope='session')
def project_root(tmp_path_factory):
    root = tmp_path_factory.getbasetemp()
    schemas = Path(__file__).resolve().parents[1] / 'contracts' / 'schemas'
    shutil.copytree(schemas, root / 'contracts' / 'schemas')
    return root


@pytest.fixture(autouse=True)
def bind_temporary_project(request, project_root, monkeypatch):
    if request.module.__name__ in {'test_loader', 'test_field_contracts', 'test_fix01'}:
        monkeypatch.setattr(request.module, 'load_config', partial(load_config, project_root=project_root))
        monkeypatch.setattr(request.module, 'load_profile', partial(load_profile, project_root=project_root))
