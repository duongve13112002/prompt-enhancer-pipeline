"""MS-2 (decision 179): `profile_manifest.v1` (manifest schema_version 2), its loader refusals, the pairing with the
configuration and the activation binding. Labelled CPU tests with synthetic bundles and drafter doubles; no model call."""

import asyncio
from copy import deepcopy
import hashlib
import json
import shutil
import sys
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from assembly_fixtures import ir, plan
from prompt_enhancer import profiles
from prompt_enhancer.config import (
    ConfigurationError, check_limit, load_profile, manifest_schema_id, overlapping_syntaxes, render_syntax,
    syntax_error, validate_renderer,
)
from prompt_enhancer.profiles import (
    CHECK_SCHEMAS, CHECK_SCHEMAS_V1, SourceLimitManifestMismatch, check_profile, check_schemas, confirm_profile,
    register_profile, resolve_profile,
)
from prompt_enhancer.writer import render_brief

from profile_fixtures import (EXAMPLE, SECTIONED, RunnerDouble, DrafterDouble, bundle, config_data, draft,
                              install_prompts, load, set_manifest, write_golden)

NULL_RENDERER = {'accepts_reference_images': False, 'max_source_images': None, 'source_image_syntax': None,
                 'reference_image_syntax': None}


@pytest.fixture(autouse=True)
def prompts(project_root):
    install_prompts(project_root)


def renderer(**changes):
    value = {'accepts_reference_images': True, 'max_source_images': 3, 'source_image_syntax': '<source_{n}>',
             'reference_image_syntax': '<ref_{n}>'}
    value.update(changes)
    return value


def manifest_v1(**changes):
    value = yaml.safe_load((EXAMPLE / 'manifest.yaml').read_bytes())
    value['status'] = 'draft'
    value.update(changes)
    return value


def write_manifest(tmp_path, manifest):
    path = tmp_path / 'manifest.yaml'
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding='utf-8')
    (tmp_path / 'system_prompt.txt').write_bytes(SECTIONED)
    return tmp_path  # load_profile takes the bundle folder


# ---- the manifest schema and the loader --------------------------------------------------------------------------

def test_the_shipped_example_is_a_version_2_manifest_and_loads(tmp_path, project_root):
    manifest = yaml.safe_load((EXAMPLE / 'manifest.yaml').read_bytes())
    assert manifest['schema_version'] == 2 and manifest_schema_id(2) == 'profile_manifest.v1'
    assert manifest['renderer'] == {'accepts_reference_images': False, 'max_source_images': 1,
                                    'source_image_syntax': 'none', 'reference_image_syntax': 'none'}
    shutil.copytree(EXAMPLE, tmp_path / 'example')
    assert load_profile(tmp_path / 'example', project_root=project_root)[0]['schema_version'] == 2


def test_a_version_1_manifest_stays_loadable(tmp_path, project_root):
    value = manifest_v1(schema_version=1, renderer={'accepts_reference_images': False, 'image_reference_syntax': 'none'})
    assert manifest_schema_id(1) == 'profile_manifest.v0'
    assert load_profile(write_manifest(tmp_path, value), project_root=project_root)[0]['schema_version'] == 1


def test_the_removed_key_is_unknown_in_version_2_and_the_new_keys_unknown_in_version_1(tmp_path, project_root):
    value = manifest_v1(renderer=dict(renderer(), image_reference_syntax='none'))
    with pytest.raises(ConfigurationError, match='image_reference_syntax'):
        load_profile(write_manifest(tmp_path, value), project_root=project_root)
    value = manifest_v1(schema_version=1, renderer=renderer())
    with pytest.raises(ConfigurationError, match='renderer'):
        load_profile(write_manifest(tmp_path, value), project_root=project_root)


def test_an_unknown_manifest_version_is_refused(tmp_path, project_root):
    with pytest.raises(ConfigurationError, match='schema_version'):
        load_profile(write_manifest(tmp_path, manifest_v1(schema_version=3)), project_root=project_root)


@pytest.mark.parametrize('key', sorted(NULL_RENDERER))
def test_each_missing_renderer_key_is_refused_naming_it(tmp_path, project_root, key):
    value = renderer()
    del value[key]
    with pytest.raises(ConfigurationError, match=key):
        load_profile(write_manifest(tmp_path, manifest_v1(renderer=value)), project_root=project_root)


@pytest.mark.parametrize('key', ['max_source_images', 'source_image_syntax', 'reference_image_syntax'])
def test_a_null_the_drafter_left_is_refused_naming_the_key(tmp_path, project_root, key):
    with pytest.raises(ConfigurationError, match=f'renderer.{key}: null'):
        load_profile(write_manifest(tmp_path, manifest_v1(renderer=renderer(**{key: None}))), project_root=project_root)


@pytest.mark.parametrize('value', [0, -1, True, False, 1.5, 2.0, '2', [], 2 ** 70])
def test_a_bad_source_image_limit_is_refused(tmp_path, project_root, value):
    with pytest.raises(ConfigurationError, match='renderer.max_source_images'):
        load_profile(write_manifest(tmp_path, manifest_v1(renderer=renderer(max_source_images=value))),
                     project_root=project_root)


@pytest.mark.parametrize('template', ['', 'no placeholder', '{n}{n}', '<{n}_{n}>', '{N}', '{ n }', '{{n}}', 'a{n}}', '{n', 'x{n}{',
                                      '{n}{}', '{0}', '{n:02d}', 'none ', 'None', 'NONE'])
@pytest.mark.parametrize('key', ['source_image_syntax', 'reference_image_syntax'])
def test_template_near_misses_are_refused_naming_the_key(tmp_path, project_root, key, template):
    with pytest.raises(ConfigurationError, match=f'renderer.{key}'):
        load_profile(write_manifest(tmp_path, manifest_v1(renderer=renderer(**{key: template}))), project_root=project_root)


@pytest.mark.parametrize('template', ['{n}', '<source_{n}>', 'image {n}', '[{n}]', '图{n}'])
def test_valid_templates_are_accepted(template):
    validate_renderer(renderer(source_image_syntax=template, reference_image_syntax='none'), 'm')


def test_none_with_a_cap_above_one_is_refused_and_with_one_accepted():
    with pytest.raises(ConfigurationError, match='renderer.source_image_syntax: none is allowed only when'):
        validate_renderer(renderer(source_image_syntax='none', max_source_images=2), 'm')
    validate_renderer(renderer(source_image_syntax='none', max_source_images=1), 'm')


def test_a_reference_syntax_needs_accepts_reference_images():
    with pytest.raises(ConfigurationError, match='renderer.reference_image_syntax: a syntax is set but'):
        validate_renderer(renderer(accepts_reference_images=False), 'm')
    validate_renderer(renderer(accepts_reference_images=False, reference_image_syntax='none'), 'm')
    validate_renderer(renderer(accepts_reference_images=True, reference_image_syntax='none'), 'm')


def test_equal_templates_are_refused():
    with pytest.raises(ConfigurationError, match='renderer.source_image_syntax: equal to renderer.reference_image_syntax'):
        validate_renderer(renderer(source_image_syntax='<img_{n}>', reference_image_syntax='<img_{n}>'), 'm')


def test_overlap_is_equality_or_substring_within_and_across_groups():
    with pytest.raises(ConfigurationError, match='renderer.reference_image_syntax'):  # "ref 1" in "ref 10"
        validate_renderer(renderer(max_source_images=10, source_image_syntax='<s{n}>', reference_image_syntax='ref {n}'), 'm')
    validate_renderer(renderer(max_source_images=9, source_image_syntax='<s{n}>', reference_image_syntax='ref {n}'), 'm')
    with pytest.raises(ConfigurationError, match='renderer.source_image_syntax'):  # within the source group
        validate_renderer(renderer(max_source_images=10, source_image_syntax='image {n}', reference_image_syntax='<r{n}>'), 'm')
    with pytest.raises(ConfigurationError, match='renderer'):  # across: "<img 1>" contains "img 1"
        validate_renderer(renderer(max_source_images=2, source_image_syntax='<img {n}>', reference_image_syntax='img {n}'), 'm')
    validate_renderer(renderer(max_source_images=1, source_image_syntax='image {n}', reference_image_syntax='<r{n}>'), 'm')


def test_non_default_values_are_accepted():
    """Override case: another cap and other syntaxes than the example's."""
    validate_renderer(renderer(max_source_images=3, source_image_syntax='<source_{n}>', reference_image_syntax='<ref_{n}>'), 'm')
    validate_renderer(renderer(max_source_images=2 ** 10, source_image_syntax='@s{n}.', reference_image_syntax='@r{n}.'), 'm')


def oracle(renderer_value):
    """Independent brute force: every rendering of both groups for n in 1..cap, pairwise substring-free."""
    cap, source, reference = (renderer_value[k] for k in ('max_source_images', 'source_image_syntax', 'reference_image_syntax'))
    names = [source.replace('{n}', str(i)) for i in range(1, cap + 1)]
    if reference != 'none':
        names += [reference.replace('{n}', str(i)) for i in range(1, cap + 1)]
    return all(i == j or a not in b for i, a in enumerate(names) for j, b in enumerate(names))


ALPHABET = st.sampled_from(['a', 'b', '1', '2', ' ', '<', '>', '_', '{n}', '{', '}', '0', 'n'])


@settings(max_examples=300, deadline=None)
@given(st.lists(ALPHABET, max_size=6), st.lists(ALPHABET, max_size=6), st.integers(min_value=1, max_value=12))
def test_the_template_validator_accepts_exactly_one_n_and_substring_free_renderings(source_parts, reference_parts, cap):
    source, reference = ''.join(source_parts), ''.join(reference_parts)
    value = {'accepts_reference_images': True, 'max_source_images': cap, 'source_image_syntax': source,
             'reference_image_syntax': reference}
    shape_ok = all(t.count('{n}') == 1 and t.replace('{n}', '').count('{') == 0 and t.replace('{n}', '').count('}') == 0
                   for t in (source, reference))
    try:
        validate_renderer(value, 'm')
        accepted = True
    except ConfigurationError:
        accepted = False
    # Without `none` in play the validator accepts exactly: one `{n}`, no other brace, distinct templates, no substring.
    expected = shape_ok and source != reference and oracle(value)
    assert accepted == expected


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=5), st.integers(min_value=1, max_value=12))
def test_syntax_error_and_render_agree(text, count):
    template = 'a' + '{n}' + text.replace('{', '').replace('}', '')
    assert syntax_error(template) is None
    assert render_syntax(template, count) == [template.replace('{n}', str(i)) for i in range(1, count + 1)]


def test_overlapping_syntaxes_reports_the_first_pair():
    assert overlapping_syntaxes({'a': ('x{n}', 2), 'b': ('y{n}', 2)}) is None
    assert overlapping_syntaxes({'a': ('x{n}', 10), 'b': ('y{n}', 2)})[0] == 'a'


def brute_overlaps(groups):
    """The reference (decision 149): every ordered pair (container, contained) of group names where a rendering of the
    contained group equals or lies inside a rendering of the container group, by rendering every string."""
    texts = {name: render_syntax(syntax, count) for name, (syntax, count) in groups.items()}
    return {(outer, inner) for outer, outer_texts in texts.items() for inner, inner_texts in texts.items()
            for j, big in enumerate(outer_texts) for i, small in enumerate(inner_texts)
            if small in big and not (outer == inner and i == j)}


AFFIX = st.text(alphabet='a1209 <>_', max_size=4)  # digits in the affixes: a prefix that ends or a suffix that starts with one
COUNT = st.one_of(st.integers(min_value=1, max_value=300), st.sampled_from([9, 10, 11, 99, 100, 101, 109, 110, 199, 200]))
TEMPLATE = st.one_of(st.just('none'), st.builds(lambda prefix, suffix: prefix + '{n}' + suffix, AFFIX, AFFIX))


@settings(max_examples=600, deadline=None)
@given(st.dictionaries(st.sampled_from(['source', 'reference']), st.tuples(TEMPLATE, COUNT), min_size=1))
def test_the_structural_overlap_check_equals_the_rendering_oracle(groups):
    expected = brute_overlaps(groups)
    clash = overlapping_syntaxes(groups)
    assert (clash is None) == (not expected)
    if clash is not None:
        assert clash in expected  # the pair it names is a real container and contained pair


@pytest.mark.parametrize('groups, clash', [
    ({'s': ('a{n}', 10)}, True),  # a1 inside a10 (within a group)
    ({'s': ('a{n}', 9)}, False),
    ({'s': ('{n}', 10)}, True),  # 1 inside 10
    ({'s': ('{n}b', 10)}, False),
    ({'s': ('{n}0', 10)}, True),  # "10" renders for n=1, and "100" for n=10: the suffix starts with a digit
    ({'s': ('a1{n}', 12)}, True),  # a prefix that ends with a digit: "a11" inside "a110"
    ({'s': ('a{n}', 1)}, False),
    ({'s': ('x{n}', 100), 'r': ('y{n}', 100)}, True),  # within a group only
    ({'s': ('<{n}>', 99), 'r': ('{n}', 99)}, True),  # across groups, an empty prefix and suffix
    ({'s': ('<{n}>', 99), 'r': ('[{n}]', 99)}, False),
    ({'s': ('{n}', 3), 'r': ('{n}', 3)}, True),  # equal across groups
    ({'s': ('<{n}>', 10), 'r': ('none', 10)}, False),  # none renders nothing
    ({'s': ('none', 10), 'r': ('none', 10)}, False),
    # the number ends in a digit of the other suffix and only that digit exceeds the cap: ' 15a' is r's m=1 but s stops at 14
    ({'s': (' {n}a', 14), 'r': (' {n}5a', 150)}, False),
    ({'s': (' {n}a', 15), 'r': (' {n}5a', 150)}, True),
])
def test_structural_overlap_cases_match_the_oracle(groups, clash):
    assert bool(brute_overlaps(groups)) == clash == (overlapping_syntaxes(groups) is not None)


def test_the_overlap_check_does_not_render_and_costs_nothing_for_a_huge_cap(monkeypatch):
    import prompt_enhancer.config as config_module

    def forbidden(*args, **kwargs):
        raise AssertionError('the overlap check rendered the syntaxes')

    monkeypatch.setattr(config_module, 'render_syntax', forbidden)
    assert overlapping_syntaxes({'s': ('<s{n}>', sys.maxsize), 'r': ('<r{n}>', sys.maxsize)}) is None
    assert overlapping_syntaxes({'s': ('s{n}', sys.maxsize)}) is not None  # s1 inside s10
    assert overlapping_syntaxes({'s': ('<{n}>', sys.maxsize), 'r': ('[{n}]', 10 ** 12)}) is None
    validate_renderer(renderer(max_source_images=sys.maxsize, source_image_syntax='<s{n}>',
                               reference_image_syntax='<r{n}>'), 'm')


@given(st.one_of(st.integers(), st.booleans(), st.floats(), st.text(max_size=3), st.none()))
def test_the_renderer_limit_accepts_exactly_positive_non_bool_integers(value):
    expected = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= sys.maxsize
    try:
        check_limit(value, 'm', 'renderer.max_source_images')
        accepted = True
    except ConfigurationError as error:
        accepted = False
        assert 'renderer.max_source_images' in str(error)
    assert accepted == expected


# ---- the pairing with the configuration ----------------------------------------------------------------------

def edit_data(tmp_path, cap, attach=False, references=2, version=1):
    data = config_data(tmp_path)
    data['edit'].update(attach_kb_reference_images=attach, max_kb_reference_images=references, max_source_images=cap)
    data['vlm']['backends']['main']['image']['max_images_per_call'] = max(cap, 1)
    if version == 2:
        data['vlm']['roles']['manifest_drafter']['output']['schema_id'] = 'profile_manifest.v1'
    return data


def v1_draft(value=None, tasks=('generate', 'edit')):
    return draft(supported_tasks=list(tasks), edit_output_semantics='whole_edited_image' if 'edit' in tasks else None,
                 renderer=dict(value or NULL_RENDERER))


def state_renderer(folder, value):
    """The admin edits the renderer lines of the registered manifest in place (a text edit keeps the form confirm needs)."""
    path = folder / 'manifest.yaml'
    lines = path.read_bytes().decode('utf-8').splitlines(keepends=True)  # bytes: no newline translation
    done = set()
    for index, line in enumerate(lines):
        key = line.strip().split(':', 1)[0]
        if key in value and line.startswith('  ') and not line.startswith('   ') and key not in done:
            if line.strip().endswith(('null', 'false', 'true')) and key in NULL_RENDERER:
                lines[index] = f'  {key}: ' + yaml.safe_dump(value[key]).split(chr(10) + '...')[0].strip() + chr(10)
                done.add(key)
    assert done == set(value), (done, 'renderer lines not found in the registered manifest')
    path.write_bytes(''.join(lines).encode('utf-8'))


def activate(tmp_path, project_root, data, value, *, hand_renderer=None, drafts_v1=False):
    """Register (the drafter double returns `value`), let the admin write the renderer, confirm and check."""
    folder = bundle(tmp_path, 'p1', SECTIONED)
    config = load(tmp_path, project_root, data, 'profile register')
    result = asyncio.run(register_profile(config, 'p1', DrafterDouble(value), project_root=project_root))
    if hand_renderer is not None:
        state_renderer(folder, hand_renderer)
    config = load(tmp_path, project_root, data, 'profile check')
    ids = [conflict['id'] for conflict in result['manifest']['conflicts']]
    confirm_profile(config, 'p1', {cid: 'profile_wins' for cid in ids}, project_root=project_root)
    for cid in ids:
        write_golden(folder, f'g_{cid}', [cid])
    set_manifest(folder, golden_tests=[f'g_{cid}' for cid in ids] or ['g0'])
    if not ids:
        write_golden(folder, 'g0', [])
    asyncio.run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    return folder, config


def test_register_with_the_version_2_drafter_writes_schema_version_2_and_the_admin_fills_the_nulls(tmp_path, project_root):
    data = edit_data(tmp_path, 2, version=2)
    folder, config = activate(tmp_path, project_root, data, v1_draft(), hand_renderer=renderer(max_source_images=2))
    manifest = yaml.safe_load((folder / 'manifest.yaml').read_bytes())
    assert manifest['schema_version'] == 2 and set(manifest['renderer']) == set(NULL_RENDERER)
    view = resolve_profile(config, 'p1', project_root=project_root)
    assert view.manifest['renderer']['max_source_images'] == 2
    record = json.loads((folder / 'activation.json').read_bytes())
    assert set(record['schemas_sha256']) == set(CHECK_SCHEMAS_V1) and 'profile_manifest.v1' in record['schemas_sha256']


def test_a_registered_manifest_with_the_nulls_is_refused_at_check_naming_the_key(tmp_path, project_root):
    data = edit_data(tmp_path, 1, version=2)
    with pytest.raises(ConfigurationError, match='renderer.max_source_images: null'):
        activate(tmp_path, project_root, data, v1_draft())


def test_a_version_1_manifest_with_a_cap_of_two_or_more_is_refused_with_a_fixed_message(tmp_path, project_root):
    data = edit_data(tmp_path, 1)
    value = draft(supported_tasks=['generate', 'edit'], edit_output_semantics='whole_edited_image')
    folder, config = activate(tmp_path, project_root, data, value)  # a version-1 profile with cap 1: fine
    assert resolve_profile(config, 'p1', project_root=project_root).profile_id == 'p1'
    effective = json.loads(json.dumps(config))
    effective['edit']['max_source_images'] = 2
    with pytest.raises(SourceLimitManifestMismatch) as error:
        resolve_profile(effective, 'p1', project_root=project_root)
    assert str(error.value) == SourceLimitManifestMismatch.MESSAGE and 'profile register' in str(error.value)
    assert isinstance(error.value, profiles.AttachmentRendererMismatch)  # intake passes it through, fixed text
    assert 'p1' not in str(error.value) and str(tmp_path) not in str(error.value)


@pytest.mark.parametrize('cap', [2, 3, 7])
def test_a_generate_only_version_1_manifest_still_loads_with_a_cap_of_two_or_more(tmp_path, project_root, cap):
    """Override case for F1: only an edit-capable version-1 manifest is refused; a generate-only one has no source image."""
    data = edit_data(tmp_path, 1)
    folder, config = activate(tmp_path, project_root, data, draft(supported_tasks=['generate']))
    assert yaml.safe_load((folder / 'manifest.yaml').read_bytes())['supported_tasks'] == ['generate']
    effective = json.loads(json.dumps(config))
    effective['edit']['max_source_images'] = cap
    view = resolve_profile(effective, 'p1', project_root=project_root)
    assert view.profile_id == 'p1' and view.supported_tasks == ('generate',)


@pytest.mark.parametrize('tasks', [['edit'], ['generate', 'edit']])
def test_an_edit_capable_version_1_manifest_is_refused_with_a_cap_of_two_or_more(tmp_path, project_root, tasks):
    data = edit_data(tmp_path, 1)
    value = draft(supported_tasks=tasks, edit_output_semantics='whole_edited_image')
    folder, config = activate(tmp_path, project_root, data, value)
    effective = json.loads(json.dumps(config))
    effective['edit']['max_source_images'] = 2
    with pytest.raises(SourceLimitManifestMismatch):
        resolve_profile(effective, 'p1', project_root=project_root)


def test_a_version_1_manifest_with_attachments_on_still_loads_MS5_obligation(tmp_path, project_root):
    """Pinned as it is today (single source, attachments on): MS-5 must refuse this pairing (see the task file)."""
    data = edit_data(tmp_path, 1, attach=True)
    value = draft(supported_tasks=['generate', 'edit'], edit_output_semantics='whole_edited_image',
                  renderer={'accepts_reference_images': True, 'image_reference_syntax': 'none'})
    folder, config = activate(tmp_path, project_root, data, value)
    assert resolve_profile(config, 'p1', project_root=project_root).profile_id == 'p1'


def test_a_version_2_manifest_with_references_beyond_the_cap_is_rechecked_for_overlap(tmp_path, project_root):
    data = edit_data(tmp_path, 2, attach=True, references=2, version=2)
    hand = renderer(max_source_images=2, source_image_syntax='<s{n}>', reference_image_syntax='ref {n}')
    folder, config = activate(tmp_path, project_root, data, v1_draft(), hand_renderer=hand)
    effective = json.loads(json.dumps(config))
    effective['edit']['max_kb_reference_images'] = 10  # "ref 1" would sit inside "ref 10"
    with pytest.raises(ConfigurationError, match='renderer.reference_image_syntax'):
        resolve_profile(effective, 'p1', project_root=project_root)


def test_attachments_with_a_renderer_that_accepts_no_reference_images_stay_refused(tmp_path, project_root):
    data = edit_data(tmp_path, 1, attach=True, version=2)
    hand = renderer(max_source_images=1, accepts_reference_images=False, reference_image_syntax='none',
                    source_image_syntax='none')
    with pytest.raises(profiles.AttachmentRendererMismatch):
        activate(tmp_path, project_root, data, v1_draft(), hand_renderer=hand)


# ---- the activation binding ------------------------------------------------------------------------------------

def test_the_schema_snapshot_is_bound_to_the_manifest_version():
    assert check_schemas({'schema_version': 1}) == CHECK_SCHEMAS and 'profile_manifest.v0' in CHECK_SCHEMAS
    assert check_schemas({'schema_version': 2}) == CHECK_SCHEMAS_V1 and 'profile_manifest.v1' in CHECK_SCHEMAS_V1
    assert 'profile_manifest.v0' not in CHECK_SCHEMAS_V1 and 'profile_manifest.v1' not in CHECK_SCHEMAS
    assert set(CHECK_SCHEMAS) - {'profile_manifest.v0'} == set(CHECK_SCHEMAS_V1) - {'profile_manifest.v1'}


def test_a_version_1_profile_still_checks_and_resolves_with_its_original_schema_set(tmp_path, project_root):
    data = edit_data(tmp_path, 1)
    folder, config = activate(tmp_path, project_root, data, draft())
    record = json.loads((folder / 'activation.json').read_bytes())
    assert set(record['schemas_sha256']) == set(CHECK_SCHEMAS)  # the same four ids it had before MS-2
    assert resolve_profile(config, 'p1', project_root=project_root).activation is not None


# ---- one source image stays byte-identical -------------------------------------------------------------------------

def brief_for(renderer_value, config):
    manifest = {'profile_id': 'p1', 'template_format': 'plain', 'output_schema_id': 'none', 'output_language': 'English',
                'quote_semantics': 'literal_text', 'edit_output_semantics': 'whole_edited_image', 'renderer': renderer_value}
    profile = SimpleNamespace(system_message=b'Admin\r\n', user_template=None, manifest=manifest, template_values={},
                              input_variable='original_prompt', addendum=None, validator_artifacts=(), validator_hashes={},
                              validator_checks=())
    payload = {'effective_config': config, 'outcome': 'ready', 'prompt': 'make the sky pink', 'task': 'edit',
               'request_id': 'r1', 'retrieval': {'state': 'disabled', 'snapshot_id': None}, 'source_image': {'data': 'AA=='}}
    return render_brief(SimpleNamespace(payload=payload, profile=profile, snapshot=None), plan(ir()), None, b'{{data}}')


PINNED_ONE_IMAGE_SHA256 = '934265d53939e75fcaa1befd1402ebcef486ebed27dac3e647589b953567be59'  # measured with writer.py unchanged from HEAD


def test_a_one_image_request_gives_the_same_bytes_with_a_version_1_or_a_version_2_manifest(model_test_profile, tmp_path):
    from intake_fixtures import configuration
    config = configuration(model_test_profile, tmp_path)
    v0 = brief_for({'accepts_reference_images': False, 'image_reference_syntax': 'none'}, config)
    v1 = brief_for({'accepts_reference_images': False, 'max_source_images': 1, 'source_image_syntax': 'none',
                    'reference_image_syntax': 'none'}, config)
    assert v0 == v1
    assert hashlib.sha256(v0.encode('utf-8')).hexdigest() == PINNED_ONE_IMAGE_SHA256
