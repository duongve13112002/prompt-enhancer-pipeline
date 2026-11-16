"""The command line, the no-torch import rule and the Windows launch flag (T-018). Subprocesses use faulthandler."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from embed_server_fixtures import free_port, server_profile
from prompt_enhancer.embed_server import __main__ as cli
from prompt_enhancer.embed_server import models
from prompt_enhancer.embed_server.families import FAMILIES
from prompt_enhancer.embed_server.models import ServerStartError, load_engine
from prompt_enhancer.embed_server.settings import load_server_settings

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # Windows only; 0 elsewhere (PLATFORM_GPU_RULES)
LOCAL_MODULES = ('protocol', 'pooling', 'families', 'settings', 'manifest', 'server', 'models', '__main__')


def run_python(args, project_root=None, timeout=120):
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run([sys.executable, '-X', 'faulthandler', *args], capture_output=True, text=True, timeout=timeout,
                          env=env, creationflags=NO_WINDOW, cwd=project_root)


def test_no_module_the_cpu_suite_imports_needs_torch_or_transformers():
    code = ('import sys\nsys.modules["torch"] = None\nsys.modules["transformers"] = None\n'
            + ''.join(f'import prompt_enhancer.embed_server.{name}\n' for name in LOCAL_MODULES)
            + 'import prompt_enhancer.k1_embed, prompt_enhancer.config\n'
            'bad = [name for name in sys.modules if name.split(".")[0] in ("torch", "transformers") and sys.modules[name]]\n'
            'assert not bad, bad\nprint("ok")\n')
    done = run_python(['-c', code])
    assert done.returncode == 0 and done.stdout.strip() == 'ok', done.stderr


def test_loading_the_engine_without_torch_is_a_clear_startup_error(project_root, model_test_profile, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'torch', None)  # test-only: makes `import torch` raise ImportError
    settings = _settings(project_root, model_test_profile)
    directory = tmp_path / 'weights'
    directory.mkdir()
    (directory / 'config.json').write_text(json.dumps({'model_type': 'embedding_gemma2',
                                                       'text_config': {'embedding_dim': 4}}), encoding='utf-8')
    with pytest.raises(ServerStartError, match='torch and transformers are required'):
        load_engine(settings, directory)


def test_the_engine_refuses_weights_of_another_family_before_importing_torch(project_root, model_test_profile, tmp_path):
    settings = _settings(project_root, model_test_profile)
    directory = tmp_path / 'weights'
    directory.mkdir()
    (directory / 'config.json').write_text(json.dumps({'model_type': 'qwen3_vl'}), encoding='utf-8')
    with pytest.raises(ServerStartError, match='model_type'):
        load_engine(settings, directory)
    (directory / 'config.json').write_text(json.dumps({'model_type': 'embedding_gemma2', 'auto_map': {'A': 'x.Y'},
                                                       'text_config': {'embedding_dim': 4}}), encoding='utf-8')
    with pytest.raises(ServerStartError, match='auto_map'):
        load_engine(settings, directory)
    with pytest.raises(ServerStartError, match='config.json'):
        load_engine(settings, tmp_path / 'missing')


def _settings(project_root, model_test_profile):
    profile = server_profile(model_test_profile, port=free_port())
    path = project_root / 'cli-profile.yaml'
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    return load_server_settings(path, project_root=project_root)


def test_family_table_matches_the_cards():
    assert FAMILIES['embedding_gemma2'].pooling == 'mean' and FAMILIES['embedding_gemma2'].forbidden_dtypes == ('float16',)
    assert FAMILIES['qwen3_vl_embedding'].pooling == 'last_token' and FAMILIES['qwen3_vl_embedding'].accepts_instruction


def _profile_file(project_root, model_test_profile, name):
    profile = server_profile(model_test_profile, port=free_port())
    path = project_root / name
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    return path


def test_the_command_line_refuses_a_bad_profile_with_status_2(project_root, model_test_profile):
    path = _profile_file(project_root, model_test_profile, 'cli-bad.yaml')
    profile = yaml.safe_load(path.read_text(encoding='utf-8'))
    profile['vlm']['backends']['embedder']['server']['dtype'] = 'float16'
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    done = run_python(['-m', 'prompt_enhancer.embed_server', 'serve', '--config', str(path),
                       '--project-root', str(project_root)])
    assert done.returncode == cli.REFUSED == 2 and done.stderr.startswith('refused: ') and 'dtype' in done.stderr
    assert 'Traceback' not in done.stderr and done.stdout == ''


def test_the_command_line_refuses_a_missing_manifest_before_binding_a_port(project_root, model_test_profile):
    path = _profile_file(project_root, model_test_profile, 'cli-nomanifest.yaml')
    manifest = project_root / 'models' / 'manifest.json'
    manifest.unlink(missing_ok=True)
    done = run_python(['-m', 'prompt_enhancer.embed_server', 'serve', '--config', str(path),
                       '--project-root', str(project_root)])
    assert done.returncode == 2 and 'model manifest' in done.stderr and 'Traceback' not in done.stderr


def test_the_command_line_reaches_the_engine_after_the_manifest_and_config_checks(project_root, model_test_profile):
    """Real weights are not used: a tiny labelled fixture passes the manifest and config.json checks; the
    process then stops at the missing torch with a clear message (the CPU environment has none)."""
    path = _profile_file(project_root, model_test_profile, 'cli-reach.yaml')
    revision = 'test'
    config = json.dumps({'model_type': 'embedding_gemma2', 'text_config': {'embedding_dim': 4}}).encode()
    # the profile's revision is `test`, which the manifest schema refuses (a 40-hex commit is required): use a real shape
    profile = yaml.safe_load(path.read_text(encoding='utf-8'))
    revision = 'f' * 40
    profile['vlm']['backends']['embedder']['model'].update(id='acme/tiny', revision=revision)
    profile['embedding']['revision'] = revision
    path.write_text(yaml.safe_dump(profile), encoding='utf-8')
    directory = project_root / 'models' / 'acme--tiny' / revision
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_bytes(config)
    (project_root / 'models' / 'manifest.json').write_text(json.dumps({'schema': 'model_manifest.v0', 'entries': [
        {'repository': 'acme/tiny', 'revision': revision, 'file': 'config.json', 'size': len(config),
         'sha256': hashlib.sha256(config).hexdigest(), 'licence': 'apache-2.0',
         'path': f'models/acme--tiny/{revision}/config.json'}]}), encoding='utf-8')
    done = run_python(['-c', 'import sys; sys.modules["torch"] = None; sys.argv = ["x", "serve", "--config", sys.argv[1], '
                       '"--project-root", sys.argv[2]]; from prompt_enhancer.embed_server.__main__ import main; '
                       'sys.exit(main())', str(path), str(project_root)])
    assert done.returncode == 2 and 'torch and transformers are required' in done.stderr, done.stderr
    assert 'Traceback' not in done.stderr


def test_the_launch_flag_is_the_no_window_flag_on_windows():
    assert NO_WINDOW == (subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    assert models.ServerStartError is cli.ServerStartError
