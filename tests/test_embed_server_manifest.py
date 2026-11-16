"""Model manifest schema and the verification that runs before any weight is loaded (T-018)."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
from jsonschema import Draft202012Validator
import pytest

from prompt_enhancer.embed_server.manifest import ManifestError, load_manifest, verify_model

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / 'contracts/schemas/model_manifest.v0.schema.json').read_bytes())
REV = 'e87f176479d0855a907a41277aca2f8ee7a09523'
REPO = 'unsloth/Qwen3.5-4B-GGUF'
# A copy of the two entries of the dev machine's models/manifest.json (the format the schema must accept as is).
EXISTING = {
    'schema': 'model_manifest.v0',
    'entries': [
        {'repository': REPO, 'revision': REV, 'file': 'Qwen3.5-4B-Q3_K_M.gguf', 'size': 2293388448,
         'sha256': 'd6981ab4d77ba712b48ef69d69042d75b5e39b9dce5fb5a5b054fd08e06afb95', 'licence': 'apache-2.0',
         'path': f'models/unsloth--Qwen3.5-4B-GGUF/{REV}/Qwen3.5-4B-Q3_K_M.gguf'},
        {'repository': REPO, 'revision': REV, 'file': 'mmproj-F16.gguf', 'size': 672423616,
         'sha256': 'cd88edcf8d031894960bb0c9c5b9b7e1fea6ebee02b9f7ce925a00d12891f864', 'licence': 'apache-2.0',
         'path': f'models/unsloth--Qwen3.5-4B-GGUF/{REV}/mmproj-F16.gguf'}]}
FAST = hsettings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def valid(manifest):
    return not list(Draft202012Validator(SCHEMA).iter_errors(manifest))


def test_the_schema_accepts_the_existing_entries_unchanged():
    assert valid(EXISTING)


def test_the_schema_accepts_the_optional_gated_flag():
    manifest = deepcopy(EXISTING)
    manifest['entries'][0]['gated'] = False
    assert valid(manifest)


@pytest.mark.parametrize('edit', [
    lambda m: m.update(schema='model_manifest.v1'), lambda m: m.pop('schema'), lambda m: m.pop('entries'),
    lambda m: m.update(extra=1), lambda m: m['entries'][0].pop('licence'), lambda m: m['entries'][0].update(size=-1),
    lambda m: m['entries'][0].update(size='big'), lambda m: m['entries'][0].update(sha256='abc'),
    lambda m: m['entries'][0].update(sha256='D' * 64), lambda m: m['entries'][0].update(revision='main'),
    lambda m: m['entries'][0].update(repository='no-owner'), lambda m: m['entries'][0].update(file='../escape'),
    lambda m: m['entries'][0].update(file='/abs'), lambda m: m['entries'][0].update(gated='no'),
    lambda m: m['entries'][0].update(unknown=1), lambda m: m.update(entries={})])
def test_the_schema_refuses(edit):
    manifest = deepcopy(EXISTING)
    edit(manifest)
    assert not valid(manifest)


def test_the_example_manifest_lists_the_three_embedding_models():
    example = json.loads((ROOT / 'configs/model_manifest.example.json').read_bytes())
    assert valid(example)
    by_repo = {}
    for entry in example['entries']:
        by_repo.setdefault(entry['repository'], []).append(entry)
        assert entry['path'] == f"models/{entry['repository'].replace('/', '--')}/{entry['revision']}/{entry['file']}"
        assert entry['licence'] == 'apache-2.0' and entry['gated'] is False
    assert sorted(by_repo) == ['Qwen/Qwen3-VL-Embedding-2B', 'Qwen/Qwen3-VL-Embedding-8B', 'google/embeddinggemma-2']
    large = {entry['file']: entry for entry in example['entries'] if entry['sha256'] != 'pin_required'}
    assert large['model.safetensors'] in by_repo['google/embeddinggemma-2'] + by_repo['Qwen/Qwen3-VL-Embedding-2B']
    shards = [entry for entry in by_repo['Qwen/Qwen3-VL-Embedding-8B'] if entry['file'].startswith('model-0000')]
    assert len(shards) == 4 and sum(entry['size'] for entry in shards) == 16_289_679_624  # the four LFS shards
    assert all(entry['sha256'] != 'pin_required' for entry in shards)
    assert {entry['revision'] for entry in by_repo['Qwen/Qwen3-VL-Embedding-8B']} == {
        '2c4565515e0f265c6511776e7193b22c0968ddc7'}


# The four 8B shards exactly as the public Hub tree listing of the pinned revision states them (documented source:
# https://huggingface.co/api/models/Qwen/Qwen3-VL-Embedding-8B/tree/2c4565515e0f265c6511776e7193b22c0968ddc7, read
# 2026-11-16, fix round 1 of T-018; the full 64-digit hashes, so a mistyped digit anywhere is caught).
HUB_8B_SHARDS = {
    'model-00001-of-00004.safetensors': (4998056552, '79ef275ec5f751d5fb59357c00d473268f9fd74abf5e38aa30137d268e7733c4'),
    'model-00002-of-00004.safetensors': (4915962464, 'a4da61f512e84fc0f0b80bcb7bcc5137eb3bf25b658a7d55f84f4056078545f0'),
    'model-00003-of-00004.safetensors': (4915962496, '7fb17cf8f06d6fe5aaacf114e85c4e6d8318799f24b517f50d1ec154a8d47007'),
    'model-00004-of-00004.safetensors': (1459698112, '000213b6d1d03ed9023fac23716da51ec4c5be221a04526c2f732d31d8fed1f5'),
}


def test_the_8b_shards_of_the_example_match_the_hub_listing_in_full():
    example = json.loads((ROOT / 'configs/model_manifest.example.json').read_bytes())
    found = {entry['file']: (entry['size'], entry['sha256']) for entry in example['entries']
             if entry['repository'] == 'Qwen/Qwen3-VL-Embedding-8B' and entry['file'].startswith('model-0000')}
    assert found == HUB_8B_SHARDS


# --- load and verify ----------------------------------------------------------------------------------------------

def make_model(root, files, repository='acme/tiny', revision='a' * 40, extra_entry=None):
    """Write a tiny model directory and its manifest (test-only bytes); return the manifest dict."""
    directory = root / 'models' / repository.replace('/', '--') / revision
    entries = []
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append({'repository': repository, 'revision': revision, 'file': name, 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest(), 'licence': 'apache-2.0',
                        'path': f"models/{repository.replace('/', '--')}/{revision}/{name}"})
    manifest = {'schema': 'model_manifest.v0', 'entries': entries + ([extra_entry] if extra_entry else [])}
    write_manifest(root, manifest)
    return manifest


def write_manifest(root, manifest):
    (root / 'models').mkdir(exist_ok=True)
    (root / 'models' / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


FILES = {'config.json': b'{"a": 1}', 'sub/model.safetensors': b'weights' * 100, 'tokenizer.json': b'{}'}
MODEL = ('acme/tiny', 'a' * 40)


def test_a_matching_model_is_verified_and_its_directory_returned(project_root, tmp_path):
    make_model(tmp_path, FILES)
    shutil_schemas(project_root, tmp_path)
    directory = verify_model(tmp_path, *MODEL)
    assert directory == Path(os.path.realpath(tmp_path)) / 'models' / 'acme--tiny' / ('a' * 40)
    assert (directory / 'sub' / 'model.safetensors').is_file()


def shutil_schemas(project_root, target):
    import shutil
    if not (target / 'contracts').exists():
        shutil.copytree(project_root / 'contracts', target / 'contracts')


@pytest.fixture
def root(project_root, tmp_path):
    shutil_schemas(project_root, tmp_path)
    return tmp_path


def refused(root, match):
    with pytest.raises(ManifestError, match=match):
        verify_model(root, *MODEL)


def test_the_two_existing_entries_load_through_the_loader(root):
    write_manifest(root, EXISTING)
    assert load_manifest(root)['entries'][0]['file'] == 'Qwen3.5-4B-Q3_K_M.gguf'


def test_a_missing_or_invalid_manifest_is_refused(root):
    refused(root, 'cannot read the model manifest')
    (root / 'models').mkdir()
    (root / 'models' / 'manifest.json').write_text('{not json', encoding='utf-8')
    refused(root, 'cannot read the model manifest')
    write_manifest(root, {'schema': 'model_manifest.v0', 'entries': 'x'})
    refused(root, 'entries')


def test_a_model_without_entries_is_refused(root):
    make_model(root, FILES)
    with pytest.raises(ManifestError, match='no manifest entry'):
        verify_model(root, 'acme/other', 'a' * 40)
    with pytest.raises(ManifestError, match='no manifest entry'):
        verify_model(root, 'acme/tiny', 'b' * 40)


def test_a_missing_file_is_refused(root):
    make_model(root, FILES)
    (root / 'models/acme--tiny' / ('a' * 40) / 'config.json').unlink()
    refused(root, 'config.json: missing')


def test_a_size_difference_is_refused(root):
    make_model(root, FILES)
    (root / 'models/acme--tiny' / ('a' * 40) / 'config.json').write_bytes(b'{"a": 12}')
    refused(root, 'config.json: size')


def test_one_flipped_byte_of_the_same_size_is_refused_by_the_hash(root):
    make_model(root, FILES)
    target = root / 'models/acme--tiny' / ('a' * 40) / 'sub' / 'model.safetensors'
    data = bytearray(target.read_bytes())
    data[3] ^= 1
    target.write_bytes(bytes(data))
    refused(root, 'model.safetensors: sha256 differs')


def test_an_unlisted_file_in_the_model_directory_is_refused(root):
    make_model(root, FILES)
    for name in ('extra.json', 'sub/extra.json', '.cache/x'):
        target = root / 'models/acme--tiny' / ('a' * 40) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'x')
        refused(root, 'not listed in the manifest')
        target.unlink()
    verify_model(root, *MODEL)


def test_unresolved_sentinels_are_refused(root):
    manifest = make_model(root, FILES)
    manifest['entries'][0]['sha256'] = 'pin_required'
    write_manifest(root, manifest)
    refused(root, 'unresolved')
    manifest['entries'][0].update(sha256=hashlib.sha256(FILES['config.json']).hexdigest(), size='pin_required')
    write_manifest(root, manifest)
    refused(root, 'unresolved')


def test_a_path_that_is_not_the_layout_path_is_refused(root):
    manifest = make_model(root, FILES)
    manifest['entries'][0]['path'] = 'models/elsewhere/config.json'
    write_manifest(root, manifest)
    refused(root, 'layout path')


def test_a_file_listed_twice_is_refused(root):
    manifest = make_model(root, FILES)
    manifest['entries'].append(dict(manifest['entries'][0]))
    write_manifest(root, manifest)
    refused(root, 'listed twice')


def test_entries_of_other_models_do_not_interfere(root):
    make_model(root, FILES)
    manifest = json.loads((root / 'models/manifest.json').read_text(encoding='utf-8'))
    manifest['entries'].append(dict(manifest['entries'][0], repository='acme/other', revision='c' * 40,
                                    path='models/acme--other/' + 'c' * 40 + '/config.json'))  # a file that does not exist
    write_manifest(root, manifest)
    verify_model(root, *MODEL)


def test_a_link_is_refused(root):
    make_model(root, FILES)
    directory = root / 'models/acme--tiny' / ('a' * 40)
    outside = root / 'outside'
    outside.mkdir()
    (outside / 'config.json').write_bytes(FILES['config.json'])
    (directory / 'config.json').unlink()
    try:
        os.symlink(outside / 'config.json', directory / 'config.json')
    except (OSError, NotImplementedError):
        pytest.skip('this account cannot create symbolic links')
    refused(root, 'link')


def test_the_hash_is_streamed_over_files_larger_than_one_block(root):
    big = os.urandom(3 * (1 << 20) + 5)  # test-only random bytes across several blocks
    make_model(root, {'config.json': big})
    verify_model(root, *MODEL)


@given(st.binary(max_size=64), st.integers(0, 63))
@FAST
def test_property_any_single_byte_change_is_refused(project_root, data, position):
    import tempfile
    data = data + b'\x00'
    with tempfile.TemporaryDirectory(dir=project_root) as name:  # scratch stays under the test base temp
        root = Path(name)
        shutil_schemas(project_root, root)
        make_model(root, {'config.json': data})
        verify_model(root, *MODEL)
        target = root / 'models/acme--tiny' / ('a' * 40) / 'config.json'
        changed = bytearray(data)
        changed[position % len(changed)] ^= 0x55
        target.write_bytes(bytes(changed))
        with pytest.raises(ManifestError):
            verify_model(root, *MODEL)
