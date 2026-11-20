"""Declared stage revisions and their guards (KBS-S3-1, R-017).

Artifact keys carry a declared revision per stage, never a hash of module source: an edit outside the code that shapes
an artifact keeps every embedding, card and alias. The guard pins a digest of the code that does shape one, so a
forgotten bump fails here instead of silently reusing stale artifacts.
"""

import ast
import builtins
import importlib
import inspect
import re
import sys
from pathlib import Path

import pytest

import stage_guard as guard
from k2_fixtures import folder
from kb_sync_fixtures import SyncServer, invoke
from prompt_enhancer import k0_checkpoint, k1_embed, k2_naming, k2_observe, k5_index
from prompt_enhancer.k1_embed import embedder_settings, image_revisions, text_revisions
from embed_server_fixtures import server_profile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src' / 'prompt_enhancer'

# Append-only history per stage: (declared revision, digest of its inventory). A change inside an inventoried
# function changes the digest, so the author must append a row with a NEW revision (and bump the constant) in the same
# change; the test refuses a repeated revision or a repeated digest, so editing the last digest in place is visible
# in review as a rewritten row, never as a silent update.
PINS = {
    'k0': [('k0-scan-v4', 'a145c7f9b49651f066d5ee260c834c1175771e79b5f458cd1788eddffee3cbab'),
           ('k0-scan-v5', '65dc9db101a60eb014b05c219304245d1b77362853bca28957fce80dc352851d'),
           ('k0-scan-v6', '9bd8b0ef67c283caf135fe492fb42ca5db1a0ce66765eb2a2c8de7ee8c7f2ea5')],
    'k1_image': [('k1-image-1', 'd3c1fdf6d02ac622be705264121b9cedb4c626df8619beb76647767fef9da8e3'),
                 ('k1-image-2', '57e5fa8e1bba3795576532c69a4a99c7a0c5589e3d54e4b619d73108851e94a8'),
                 ('k1-image-3', '033c211f37c3def09a9103ab23b764777c8ba173debebebe6249a409a3503975'),
                 ('k1-image-4', '04f50a2531d660924a1c5ec9eea5ab29860fc016c8b7f65e84bbcd59d5ac7149')],
    'k1_text': [('k1-text-1', '861021292f329dd3cd006a65823d398ef6207b145b7779646c83db6291ef3d48'),
                ('k1-text-2', '9b0bdb7c4d79becd4f75843704812eb8f7d90e5e0a2325794bdffe4d5de69d3e')],
    'model_call': [('model-call-1', 'd343f8ab7bf36edd21b077e7152cf1e9c5404747d0e337e0d8f275e1b0842466'),
                   ('model-call-2', '9785be00c96416c3342fcff3bccf84c720e036d22e83aab58b9dec397219355c'),
                   # K3-RETRY: a per-request seed override on the request copy; the probe seed retry
                   ('model-call-3', '6c2b6e8b5f92f8edb789bb60d75edbf87dfc703bc55b50dc978c01de1718ba29'),
                   # T017-FIX on K3-RETRY: addendum_layout, the narrower reply schema, the posted system hash
                   ('model-call-4', '3289be06c097f147e875604a7417bc884984ced2e0b9a312872566d479064a9b'),
                   # T017-FIX2: the per-request (shortlist-bounded) sent schema, its hash and client validation
                   # (re-pinned in place in fix round 1, N1, before the row reached main)
                   ('model-call-5', 'cd041a98fbd884f0d996ca7f0657abf063d701da3168e7ce35684639fb29c176'),
                   # T017-FIX3 (the writer branch passes the bundle output schema id; no K-role request changes)
                   # and CLOUD-1 (the Anthropic structured-output schema, x-api-key, thinking disabled, sent
                   # hash), integrated under one bump
                   ('model-call-6', '5a8e0706acd2e112374d9abfda7d8f400bc2531ffb75e5bd63ea085cbb439236'),
                   # T017-HAIKU: `$comment` and `minProperties` removed from the schema sent to Anthropic
                   ('model-call-7', 'ac3acc47fa1b66619fa4a55236ded5cb6f0b14a391fea1ae3872930e15c16b63'),
                   # CLOUD-2: `output.transport: tool_forced` (forced non-strict tool request, tool_use reply parsing)
                   ('model-call-8', '414a6e4e59bafc3ada60b8736d8c7af68ae59ce2b6a1714669d4316a306d4b5e'),
                   # T017-FIX5: `tool_choice` carries `disable_parallel_tool_use`; a writer reply keeps its text
                   ('model-call-9', '14be61a3280fb954be6cbc58ccd337a456c95b9b04d5064329f480430a15b87b')],
    'k2_naming': [('k2-naming-1', 'f7c8bc6ccac88fd3bde0d29807ee60669e67b67f6e7b054ded05998ca6e11fa5'),
                  ('k2-naming-2', '9028bbc88242340042e849112324d30d03e9568f25384d5863bf5dac1976eba6')],
    'k2_draw': [('k2-draw-v1', '085e0f67f4a98374df27d6c6103d10119f223e71975675382c3e1763dc71117b')],
    'k2_observe': [('k2-observe-1', 'e8b5d4c403b8f058114556fff14bd100bff47083b25d78b101e2307a8db27fba'),
                   ('k2-observe-2', '9ede0abca2bd891708b305b83c9c5de621fe174d60016ff5a8278c86dbbe4002'),
                   ('k2-observe-3', 'cd7bdde0196b89533b228a58858bd67f2893b09c62058a9944d65764e17e7bf9'),  # K3-RETRY
                   ('k2-observe-4', 'ed31a60ddafc5e76acd306037271b94535beb4bf587a90ac8439f2a5f8283681')],  # CLOUD-1
    'k3': [('k3-card-1', '1247c6586399c4d98fdf64513accee9feb29db2cbb5c743daf02946c4a82611b'),
           ('k3-card-2', 'c80d7fc38f7d3d8e2f2ae348ccbb0305c02ae3bc885b4ca2b874aa5d65677a30'),
           ('k3-card-3', 'baac7f04d4106c4be7b789775409a5804db3f23b92728f67bb4921e4d6e8ddd1'),  # K3-RETRY
           ('k3-card-4', '2cbf013f5b80b10cc03dce62337e47597ea355d100af64af0e97b52989b5e93a')],  # CLOUD-1
    'k4': [('k4-alias-1', 'e0b9748cea1ad96d2d904ec855ce9b00f5dda8e080d9a1588a9b121b80bd48e6'),
           ('k4-alias-2', 'b4404f20f5f572e969fbe228ebb00e6ac3a898fdbcb13f5b81a756f1e3054f44')],
    'k5': [('k5-index-1', 'e1609a9b78845d6465985457b276da2cd022c001a8999dedb7747bba7ee8bf4f'),
           ('k5-index-2', '3a25f1e9a1e1ab8ceebccc0f75c6ef959a1ba201d80809e4554a0b47ac75cfd0')],
    'kb_sync': [('kb-sync-1', '2126cc1a8a831a34add01a3aa8df7a5f2b074359cce34b3505f1d91cd23a607f'),
                ('kb-sync-2', 'd37741dbeab9763048f69f8983f2ac34e58a228fdeaa8cb0d73e6606f93e870b'),
                ('kb-sync-3', 'a407996f66c99ae87dd53d4b42e3162c4705d1e36d640abe1f490587ecb46094'),
                ('kb-sync-4', '72f51ce38e75c40bef7e944325029f175670660888d9b0020c210eb5d1b2659d'),
                ('kb-sync-5', 'd8a0ee04f66cc973b883fd3c906f2d005133793fbf7c27033e075eea4ae66980'),
                ('kb-sync-6', '2f461e017a52045c51348a265cd694a93ff91248f6e2bf6c9a45c3bff69a2c8c')],  # KB-EXTERNAL: one physical root
}
ENTRIES = sorted({(module, name) for stage in guard.STAGES.values() for module, name in stage[2]})

# The syntax-tree dump format belongs to the Python minor version (decision 124); CI and the dev venv run 3.12.
dump_format = pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason='pins are digests of the Python 3.12 ast.dump')


def declared(stage):
    module, constant, _ = guard.STAGES[stage]
    return getattr(importlib.import_module('prompt_enhancer.' + module), constant)


def test_every_stage_has_pins_and_a_declared_revision():
    assert set(PINS) == set(guard.STAGES)
    for stage in guard.STAGES:
        assert isinstance(declared(stage), str) and declared(stage)


@pytest.mark.parametrize('stage', sorted(guard.STAGES))
def test_history_is_append_only_by_revision_and_digest(stage):
    revisions, digests = zip(*PINS[stage])
    assert len(set(revisions)) == len(revisions) and len(set(digests)) == len(digests)


@dump_format
@pytest.mark.parametrize('stage', sorted(guard.STAGES))
def test_pinned_inventory_matches_declared_revision(stage):
    """Fails when an inventoried function changed without a new revision and pin row, or a revision moved alone."""
    revision, digest = PINS[stage][-1]
    assert declared(stage) == revision, 'revision constant and the last pin row disagree'
    assert guard.stage_digest(stage) == digest, (
        f'{stage}: code that shapes its artifacts changed; bump the revision, append a pin row with the new '
        f'digest and revision (stored artifacts of the stage are recomputed once)')


# -- the guard is exact: unlisted edits are invisible, listed ones move the digest -----------------------------------
def _mutated(module, name, edit):
    tree = ast.parse(guard.module_source(module))
    node = guard._find(tree.body, name.split('.'))
    edit(node)
    return ast.unparse(ast.fix_missing_locations(tree))


def _append_statement(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        node.body.append(ast.parse('_marker = 1').body[0])
    else:
        node.value = ast.Tuple(elts=[node.value, ast.Constant(0)], ctx=ast.Load())


def _add_docstring(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        first = node.body[0]
        has_docstring = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                         and isinstance(first.value.value, str))
        docstring = ast.Expr(ast.Constant('A new docstring that must not matter.'))
        if has_docstring:
            node.body[0] = docstring
        else:
            node.body.insert(0, docstring)


@pytest.mark.parametrize(('module', 'name'), ENTRIES, ids=[f'{m}:{n}' for m, n in ENTRIES])
def test_a_change_inside_an_inventoried_entry_moves_its_dump(module, name):
    original = guard.entry_dump(guard.module_source(module), name)
    assert guard.entry_dump(_mutated(module, name, _append_statement), name) != original


@pytest.mark.parametrize(('module', 'name'), ENTRIES, ids=[f'{m}:{n}' for m, n in ENTRIES])
def test_comments_docstrings_and_unlisted_code_leave_an_entry_dump(module, name):
    text = guard.module_source(module)
    original = guard.entry_dump(text, name)
    scratch = '# a new comment\n' + text + '\n\ndef _unlisted_helper():\n    """Not in any inventory."""\n    return 1\n'
    assert guard.entry_dump(scratch, name) == original
    assert guard.entry_dump(_mutated(module, name, _add_docstring), name) == original


def test_an_edit_outside_the_inventories_leaves_every_stage_digest():
    """Simulated on in-memory scratch copies: a comment and a new helper in config.py and the stage modules."""
    modules = {module for stage in guard.STAGES.values() for module, _ in stage[2]} | {'config', 'k5_index'}
    scratch = {module: guard.module_source(module) + '\n# unrelated\n\ndef _new_unrelated():\n    return 2\n'
               for module in modules}
    for stage in guard.STAGES:
        assert guard.stage_digest(stage, scratch) == guard.stage_digest(stage)


def test_an_edit_inside_an_inventory_moves_only_the_stages_that_list_it():
    original = {stage: guard.stage_digest(stage) for stage in guard.STAGES}
    changed = _mutated('k3_cards', 'assemble_card', _append_statement)
    moved = {stage for stage in guard.STAGES if guard.stage_digest(stage, {'k3_cards': changed}) != original[stage]}
    assert moved == {'k3'}


# -- completeness: a function nobody classified fails here (review F2) ------------------------------------------------
def test_every_function_and_constant_is_pinned_or_left_out_with_a_reason():
    missing, stale, doubled = guard.unclassified()
    assert not missing, f'pin these in a stage inventory or add them to LEFT_OUT with a reason: {missing}'
    assert not stale, f'LEFT_OUT names that no longer exist: {stale}'
    assert not doubled, f'names both pinned and left out: {doubled}'
    assert all(reason.strip() for _, reason, _ in guard.LEFT_OUT)


def test_a_new_unclassified_function_in_a_stage_module_is_reported():
    """Mutant: a new top-level function, method and constant in three covered modules, none classified."""
    scratch = {
        'k1_embed': guard.module_source('k1_embed') + '\n\ndef _new_order(rows):\n    return rows\n',
        'k5_index': guard.module_source('k5_index') + '\n\nNEW_TABLE = ()\n',
        'model_client': guard.module_source('model_client') + '\n\nclass Extra:\n    def shape(self):\n        return 1\n',
    }
    missing, stale, doubled = guard.unclassified(lambda module: scratch.get(module) or guard.module_source(module))
    assert {('k1_embed', '_new_order'), ('k5_index', 'NEW_TABLE'), ('model_client', 'Extra.shape')} <= set(missing)
    assert not stale and not doubled


def test_a_left_out_name_that_is_removed_is_reported_stale():
    text = guard.module_source('k4_aliases').replace('def build_aliases(', 'def build_aliases_renamed(')
    _, stale, _ = guard.unclassified(lambda module: text if module == 'k4_aliases' else guard.module_source(module))
    assert ('k4_aliases', 'build_aliases') in stale


# -- extracted helpers and shared code: one mutant each, no revision bump, the right stages move -----------------------
HELPER_MUTANTS = [
    pytest.param('k1_embed', "items[start:start + settings.batch_size]", "items[start:start + settings.batch_size][::-1]",
                 {'k1_image', 'k1_text'}, id='MF-chunks-reversed'),
    pytest.param('k1_embed', "sorted(rows, key=lambda row: row['image_id'])",
                 "sorted(rows, key=lambda row: row['image_id'], reverse=True)", {'k1_image'}, id='image-order'),
    pytest.param('k1_embed', 'sorted({item.text for item in items})', 'sorted({item.text for item in items}, reverse=True)',
                 {'k1_text'}, id='text-order'),
    pytest.param('storage.identity', 'ensure_ascii=False, allow_nan=False)', 'ensure_ascii=True, allow_nan=False)',
                 {'k1_image', 'k1_text', 'k2_naming', 'k2_draw', 'k2_observe', 'k3', 'k4', 'k5'},
                 id='MG-canonical-json'),
    pytest.param('model_client', 'is int and usage[key] >= 0}', 'is int and usage[key] > 0}', {'model_call'},
                 id='MH-usage'),
    pytest.param('k2_observe', "sorted(members, key=lambda row: row['name'])",
                 "sorted(members, key=lambda row: row['name'], reverse=True)", {'k2_observe'}, id='declared-order'),
    pytest.param('k2_observe', "'concept_id': row['concept_id']}", "'concept_id': row['name_as_written']}",
                 {'k2_observe'}, id='member-layout'),
    pytest.param('k5_index', "'size_bytes': len(data), 'artifact': artifact}", "'size_bytes': len(data) + 1, "
                 "'artifact': artifact}", {'k5'}, id='part-entry'),
]


@pytest.mark.parametrize(('module', 'old', 'new', 'moved'), HELPER_MUTANTS)
def test_a_helper_or_shared_edit_moves_exactly_the_stages_that_pin_it(module, old, new, moved):
    text = guard.module_source(module)
    assert text.count(old) == 1, 'the mutant must hit one place'
    changed = text.replace(old, new)
    after = {stage for stage in guard.STAGES if guard.stage_digest(stage, {module: changed}) != guard.stage_digest(stage)}
    assert after == moved


# -- the declared revision is what keys the artifacts ---------------------------------------------------------------
def test_no_source_reads_enter_any_artifact_key(model_test_profile, monkeypatch):
    """Key construction never reads a Python file: patched readers fail any `.py` read while keys are built."""
    settings = embedder_settings(server_profile(model_test_profile))

    class Catalog:  # labelled test double: the only thing `_tools` asks of a catalog
        @staticmethod
        def schema(_name):
            return {}

    real_read_text, real_read_bytes, real_open = Path.read_text, Path.read_bytes, builtins.open

    def refuse(path):
        if str(path).endswith('.py'):
            raise AssertionError(f'artifact key read source file {path}')

    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k: (refuse(self), real_read_text(self, *a, **k))[1])
    monkeypatch.setattr(Path, 'read_bytes', lambda self: (refuse(self), real_read_bytes(self))[1])
    monkeypatch.setattr(builtins, 'open', lambda file, *a, **k: (refuse(file), real_open(file, *a, **k))[1])
    monkeypatch.setattr(inspect, 'getsource', lambda *a, **k: pytest.fail('artifact key used inspect.getsource'))
    image_revisions(settings), text_revisions(settings), k5_index._tools(Catalog)
    k2_naming._role_revisions(model_test_profile, 'folder_namer', b'prompt', ROOT)
    k2_observe.observation_revisions(model_test_profile, b'prompt', ROOT)


def test_sources_hold_no_module_source_fingerprint():
    for path in SRC.rglob('*.py'):
        text = path.read_text(encoding='utf-8')
        assert 'tool_revision' not in text and 'MODEL_TOOL_MODULES' not in text, path
        assert 'inspect.getsource' not in text, path
        assert not re.search(r'Path\(__file__\)(\.parent)?(\s*/[^\n]*)?\.read_(text|bytes)', text), path


def test_k1_image_and_text_keys_carry_their_own_revision(model_test_profile, monkeypatch):
    settings = embedder_settings(server_profile(model_test_profile))
    image, text = image_revisions(settings), text_revisions(settings)
    monkeypatch.setattr(k1_embed, 'K1_IMAGE_REVISION', 'k1-image-test')
    assert image_revisions(settings)['schema'] != image['schema'] and text_revisions(settings) == text
    monkeypatch.undo()
    monkeypatch.setattr(k1_embed, 'K1_TEXT_REVISION', 'k1-text-test')
    assert text_revisions(settings)['schema'] != text['schema'] and image_revisions(settings) == image


@pytest.mark.parametrize(('role', 'make'), [
    ('folder_namer', lambda config: k2_naming._role_revisions(config, 'folder_namer', b'prompt', ROOT)),
    ('info_assessor', lambda config: k2_naming._role_revisions(config, 'info_assessor', b'prompt', ROOT)),
    ('observer', lambda config: k2_observe.observation_revisions(config, b'prompt', ROOT)),
])
def test_model_stage_keys_carry_stage_and_model_call_revisions(model_test_profile, monkeypatch, role, make):
    own = 'K2_OBSERVE_REVISION' if role == 'observer' else 'K2_NAMING_REVISION'
    other = 'K2_NAMING_REVISION' if role == 'observer' else 'K2_OBSERVE_REVISION'
    module = k2_observe if role == 'observer' else k2_naming
    before = make(model_test_profile)
    with monkeypatch.context() as patch:
        patch.setattr(module, own, 'test-stage')
        assert make(model_test_profile)['schema'] != before['schema']
    with monkeypatch.context() as patch:
        patch.setattr(module, 'MODEL_CALL_REVISION', 'test-model-call')
        assert make(model_test_profile)['schema'] != before['schema']
    with monkeypatch.context() as patch:  # another stage's declaration is not this stage's identity
        patch.setattr(k2_observe if role != 'observer' else k2_naming, other, 'test-other')
        assert make(model_test_profile) == before


def test_k5_and_k0_keys_carry_their_revision(monkeypatch):
    class Catalog:  # labelled test double
        @staticmethod
        def schema(_name):
            return {}

    before = k5_index._tools(Catalog)
    assert before['code'] == k5_index.K5_INDEX_REVISION and set(before) == {
        'numpy', 'arrow', 'unicode', 'code', 'normalization', 'schema'}
    monkeypatch.setattr(k5_index, 'K5_INDEX_REVISION', 'k5-test')
    assert k5_index._tools(Catalog) != before
    assert k0_checkpoint._REVISION['schema'] == k0_checkpoint.K0_SCAN_REVISION
    key = k0_checkpoint._key('k0_scan', {'a': 1})
    monkeypatch.setitem(k0_checkpoint._REVISION, 'schema', 'k0-test')
    assert k0_checkpoint._key('k0_scan', {'a': 1}) != key


def _patch_everywhere(monkeypatch, name, value):
    for module in list(sys.modules.values()):
        if getattr(module, '__name__', '').startswith('prompt_enhancer') and hasattr(module, name):
            monkeypatch.setattr(module, name, value)


def _embedder_images(server):
    return sum(1 for body in server.embedder.requests if 'image_url' in str(body))


STAGE_ROLES = {'k2_naming': {'info_assessor', 'folder_namer'}, 'k2_observe': {'observer'}, 'k3': {'card_builder'},
               'k4': {'alias_generator'}, 'model_call': {'info_assessor', 'folder_namer', 'observer', 'card_builder',
                                                         'alias_generator'}}


@pytest.mark.parametrize('stage', ['k2_naming', 'k2_observe', 'k3', 'k4', 'model_call'])
def test_sync_reruns_only_the_model_calls_of_the_bumped_stage(k2_env, monkeypatch, stage):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    first = set(server.roles)
    server.roles.clear()
    embeddings = len(server.embedder.requests)
    _patch_everywhere(monkeypatch, guard.STAGES[stage][1], declared(stage) + '-test')
    assert invoke(k2_env, server)['status'] == 'published'
    expected = first & STAGE_ROLES[stage]
    assert expected, 'the first sync made no call of this stage; the test would prove nothing'
    assert set(server.roles) == expected
    assert len(server.embedder.requests) == embeddings  # replies are unchanged, so no text or image is re-embedded


@pytest.mark.parametrize(('stage', 'part'), [('k1_image', 'images'), ('k1_text', 'texts')])
def test_sync_reembeds_only_the_bumped_k1_part(k2_env, monkeypatch, stage, part):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    images, total = _embedder_images(server), len(server.embedder.requests)
    assert images and total > images, 'both K1 parts must have run'
    server.roles.clear()
    _patch_everywhere(monkeypatch, guard.STAGES[stage][1], declared(stage) + '-test')
    assert invoke(k2_env, server)['status'] == 'published'
    now_images, now_total = _embedder_images(server), len(server.embedder.requests)
    assert server.roles == []
    if part == 'images':
        assert now_images > images and now_total - now_images == total - images
    else:
        assert now_images == images and now_total - now_images > total - images


def test_kb_sync_revision_replans_without_new_model_or_embedding_calls(k2_env, monkeypatch):
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    server.roles.clear()
    requests = len(server.embedder.requests)
    _patch_everywhere(monkeypatch, 'KB_SYNC_REVISION', 'kb-sync-test')
    assert invoke(k2_env, server)['status'] == 'published'
    assert server.roles == [] and len(server.embedder.requests) == requests


def test_a_partially_committed_image_batch_keeps_its_cached_successful_members():
    # Retest NF1: the request of a resumed chunk is the original batch minus cached refusals, in chunk order,
    # so bf16 vectors of the new members equal those of an uninterrupted run (T-018).
    from prompt_enhancer.k1_embed import _batch_members, _companion_lost
    records = {'b': {'vector': b'v'}, 'c': {'vector': None}}
    chunk = [('a', 'ka'), ('b', 'kb'), ('c', 'kc'), ('d', 'kd')]
    assert _batch_members(chunk, records) == [('a', 'ka'), ('b', 'kb'), ('d', 'kd')]
    assert _batch_members(chunk, {}) == chunk
    assert _companion_lost('b', None, records) and _companion_lost('b', ({}, None), records)
    assert not _companion_lost('b', ({}, object()), records)
    assert not _companion_lost('a', None, records) and not _companion_lost('a', ({}, None), records)


def test_a_bumped_k2_draw_revision_changes_the_sync_identity(k2_env, monkeypatch):
    # The draw stage makes no model call, so the call-count test above cannot cover it: a bumped draw revision
    # must still reach the sync identity, or `kb sync` answers `unchanged` and never applies it (retest NF2).
    folder(k2_env, 'f', 'Name\ndescription')
    server = SyncServer()
    invoke(k2_env, server)
    assert invoke(k2_env, server)['status'] == 'unchanged'
    requests = len(server.embedder.requests)
    _patch_everywhere(monkeypatch, guard.STAGES['k2_draw'][1], declared('k2_draw') + '-test')
    assert invoke(k2_env, server)['status'] == 'published'
    assert len(server.embedder.requests) == requests
