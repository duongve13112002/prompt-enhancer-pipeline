"""The reference check (T-018 P2): fit and verdict arithmetic, the `server.reference_check.min_cosine` and
`.max_norm_deviation` reads of `embed reference-check` (override and refusal), the reference-file guards (E2), K1 errors
as refusals (E1), the EG2 kernel refusal (F6) and the command line end to end against a loopback server holding a
labelled engine double or through a labelled httpx.MockTransport. No model, no torch, no GPU."""

import asyncio
import json
import math
from copy import deepcopy
from functools import partial
from pathlib import Path

import httpx
from hypothesis import HealthCheck, given, settings, strategies as st
import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer.embed_server import __main__ as cli
from prompt_enhancer.embed_server import reference as ref
from prompt_enhancer.embed_server.models import EmbedResult
from prompt_enhancer.embed_server.settings import server_settings
from prompt_enhancer.k1_embed import Embedder, embedder_settings
from embed_server_fixtures import EngineDouble, EngineFault, free_port, running_server, server_profile
from k1_fixtures import image_bytes

KEY = 'vlm.backends.embedder.server.reference_check'
CHECK = {'min_cosine': 0.9, 'max_norm_deviation': 0.001}  # test-only tolerances


def tolerances(**changes):
    return dict(CHECK, **changes)


def with_check(profile, value):
    profile = deepcopy(profile)
    profile['vlm']['backends']['embedder']['server']['reference_check'] = value
    return profile


def write(project_root, profile, name):
    path = project_root / name
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding='utf-8')
    return path


# --- arithmetic ------------------------------------------------------------------------------------------------------

def test_cosine_of_parallel_orthogonal_and_opposite_vectors():
    assert ref.cosine([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert ref.cosine([1.0, 0.0], [0.0, 3.0]) == pytest.approx(0.0)
    assert ref.cosine([1.0, 1.0], [-1.0, -1.0]) == pytest.approx(-1.0)
    for bad in (([1.0], [1.0, 0.0]), ([], []), ([0.0, 0.0], [1.0, 0.0]), ([math.inf, 0.0], [1.0, 0.0])):
        with pytest.raises(ref.ReferenceCheckError):
            ref.cosine(*bad)


@pytest.mark.parametrize('value, floored', [(0.99995, 0.9999), (0.9999, 0.9999), (0.99999999, 0.9999), (1.0, 1.0),
                                            (0.12349, 0.1234)])
def test_the_fit_floors_to_four_decimals(value, floored):
    assert ref.floor_to(value) == floored


@given(st.floats(min_value=1e-6, max_value=1.0, allow_nan=False))
def test_property_the_floor_never_rises_and_loses_less_than_one_step(value):
    floored = ref.floor_to(value)
    assert floored <= value and value - floored < 10 ** -ref.FIT_DECIMALS + 1e-12


def test_the_fit_is_the_lowest_pairwise_cosine_of_any_input():
    runs = {'a': [[1.0, 0.0], [0.0, 1.0]], 'b': [[1.0, 0.0], [0.1, 1.0]], 'c': [[1.0, 0.05], [0.0, 1.0]]}
    fit = ref.fit_min_cosine(runs)
    worst = ref.cosine([0.0, 1.0], [0.1, 1.0])  # input 1, runs a-b and b-c
    assert fit['raw_min_cosine'] == pytest.approx(worst)
    assert fit['min_cosine'] == ref.floor_to(worst) and fit['decimals'] == 4
    assert fit['per_input'][0]['min_cosine'] == pytest.approx(ref.cosine([1.0, 0.0], [1.0, 0.05]))
    assert set(fit['per_input'][1]['pair']) <= {'a', 'b', 'c'} and 'b' in fit['per_input'][1]['pair']


@pytest.mark.parametrize('runs', [{'only': [[1.0]]}, {}, {'a': [[1.0]], 'b': []}, {'a': [], 'b': []}])
def test_the_fit_refuses_runs_without_a_spread(runs):
    with pytest.raises(ref.ReferenceCheckError):
        ref.fit_min_cosine(runs)


def test_the_verdict_passes_at_the_tolerance_and_fails_below_it_or_at_another_length():
    inputs = [ref.Input('text', f'text-{i}', 'x', None, 't') for i in range(3)]
    reference = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    at = [0.8, 0.6]  # cosine 0.8 exactly
    served = [at, [0.79, math.sqrt(1 - 0.79 ** 2)], [1.0, 0.0, 0.0]]
    verdict = ref.judge(inputs, reference, served, ref.cosine([1.0, 0.0], at), 0.001, 2)
    assert [item['verdict'] for item in verdict['items']] == ['pass', 'fail', 'fail'] and not verdict['passed']
    assert verdict['items'][2]['cosine'] is None and verdict['items'][2]['dimension'] == 3
    assert ref.judge(inputs[:1], reference[:1], [at], 0.5, 0.001, 2)['passed']
    with pytest.raises(ref.ReferenceCheckError):
        ref.judge(inputs, reference[:2], served, 0.5, 0.001, 2)


@pytest.mark.parametrize('scale, bound, passed', [
    (2.0, 0.001, False),  # the review's probe: a server that skips L2 keeps cosine 1.0
    (0.5, 0.001, False), (1.0005, 0.001, True), (0.9995, 0.001, True),  # rounding inside the bound passes
    (1.0005, 0.0001, False),  # the bound decides, not a constant
    (1.0, 1e-12, True), (1e30, 0.5, False),  # K1 rounds to float32, so a served norm stays finite
])
def test_the_verdict_needs_a_norm_within_the_bound_and_never_normalises(scale, bound, passed):
    inputs = [ref.Input('text', 'text-0', 'x', None, 't')]
    served = [[scale * 0.6, scale * 0.8]]
    verdict = ref.judge(inputs, [[0.6, 0.8]], served, 0.99, bound, 2)
    assert verdict['passed'] is passed and verdict['max_norm_deviation'] == bound
    assert verdict['items'][0]['norm'] == pytest.approx(scale)  # recorded as received


def test_the_norm_fit_is_the_largest_deviation_of_any_run_rounded_up():
    runs = {'a': [[0.6, 0.8], [1.0, 0.0]], 'b': [[0.6, 0.8], [1.00154404, 0.0]], 'c': [[0.0, 0.99977]]}
    fit = ref.fit_max_norm_deviation(runs)
    assert fit['raw_max_norm_deviation'] == pytest.approx(0.00154404) and fit['max_norm_deviation'] == 0.0016
    assert ref.fit_max_norm_deviation({'a': [[1.0, 0.0]]})['max_norm_deviation'] == 0.0
    # a true length overflow (P3: 1e200 values now have a finite length, measured without squaring)
    for bad in ({}, {'a': []}, {'a': [[1.5e308, 1.5e308]]}):
        with pytest.raises(ref.ReferenceCheckError):
            ref.fit_max_norm_deviation(bad)


@pytest.mark.parametrize('value, ceiled', [(0.0015440438051930183, 0.0016), (0.0022919215404879623, 0.0023),
                                           (0.0016, 0.0016), (0.0, 0.0), (0.00000001, 0.0001)])
def test_the_norm_fit_rounds_up_to_four_decimals(value, ceiled):
    assert ref.ceil_to(value) == ceiled  # the first two: the recorded dev runs (P2 fix round 1)


@given(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_property_the_ceiling_never_falls_and_adds_less_than_one_step(value):
    ceiled = ref.ceil_to(value)
    assert ceiled >= value and ceiled - value < 10 ** -ref.FIT_DECIMALS + 1e-12


@pytest.mark.parametrize('sizes, ok', [([1], True), ([1, 2, 4], True), ([2, 4], False), ([], False), ([1, 0], False),
                                       ([1, 1], False), ([1, True], False)])
def test_batch_sizes_need_the_baseline_one_and_positive_distinct_values(sizes, ok):
    if ok:
        ref.check_batch_sizes(sizes)
    else:
        with pytest.raises(ref.ReferenceCheckError):
            ref.check_batch_sizes(sizes)


# --- configuration: `server.reference_check.min_cosine` --------------------------------------------------------------

@pytest.fixture
def profile(model_test_profile):
    return server_profile(model_test_profile, port=free_port())


@pytest.mark.parametrize('check', [tolerances(min_cosine=0.9871), tolerances(min_cosine=1), tolerances(min_cosine=0.5),
                                   tolerances(max_norm_deviation=0.0023), tolerances(max_norm_deviation=0),
                                   tolerances(max_norm_deviation=0.5)])
def test_reference_check_reads_a_measured_tolerance(project_root, profile, check):
    config = load_config(write(project_root, with_check(profile, check), 'rc-ok.yaml'),
                         'embed reference-check', project_root=project_root)
    assert config['vlm']['backends']['embedder']['server']['reference_check'] == check


@pytest.mark.parametrize('check, message', [
    (None, 'reference_check: missing key'), ({}, "is a required property"),
    ({'min_cosine': 0.9}, "'max_norm_deviation' is a required property"),
    ({'max_norm_deviation': 0.001}, "'min_cosine' is a required property"),
    (tolerances(min_cosine='fit_from_bench'), 'fit_from_bench'), (tolerances(min_cosine=0), 'min_cosine'),
    (tolerances(min_cosine=-0.5), 'min_cosine'), (tolerances(min_cosine=1.0001), 'min_cosine'),
    (tolerances(min_cosine=True), 'min_cosine'), (tolerances(min_cosine='high'), 'min_cosine'),
    (tolerances(min_cosine=float('nan')), 'min_cosine'),
    (tolerances(max_norm_deviation='fit_from_bench'), 'max_norm_deviation: unresolved sentinel fit_from_bench'),
    (tolerances(max_norm_deviation=-0.0001), 'max_norm_deviation'),
    (tolerances(max_norm_deviation=1), 'max_norm_deviation'),
    (tolerances(max_norm_deviation=True), 'max_norm_deviation'),
    (tolerances(max_norm_deviation='loose'), 'max_norm_deviation'),
    (tolerances(max_norm_deviation=float('nan')), 'max_norm_deviation'),
    (tolerances(max_norm_deviation=float('inf')), 'max_norm_deviation'),
    (tolerances(max_cosine=1), 'reference_check'),
])
def test_reference_check_refuses_a_missing_unresolved_or_out_of_range_tolerance(project_root, profile, check, message):
    if check is None:
        changed = deepcopy(profile)
        changed['vlm']['backends']['embedder']['server'].pop('reference_check', None)
    else:
        changed = with_check(profile, check)
    with pytest.raises(ConfigurationError, match=message):
        load_config(write(project_root, changed, 'rc-bad.yaml'), 'embed reference-check', project_root=project_root)


@pytest.mark.parametrize('check', [None, {'min_cosine': 'fit_from_bench', 'max_norm_deviation': 'fit_from_bench'}])
def test_the_server_does_not_read_the_tolerance(project_root, profile, check):
    changed = deepcopy(profile) if check is None else with_check(profile, check)
    config = load_config(write(project_root, changed, 'rc-serve.yaml'), 'embed serve', project_root=project_root)
    assert server_settings(config, project_root).model_id  # loads with no value or with the sentinel


# --- the reference file guards ---------------------------------------------------------------------------------------

def _loaded(project_root, profile, name='rc-run.yaml'):
    path = write(project_root, profile, name)
    config = load_config(path, 'embed reference-check', project_root=project_root)
    return path, config, server_settings(config, project_root), embedder_settings(config)


def _inputs(project_root, k1_settings):
    folder = project_root / 'rc-inputs'
    folder.mkdir(exist_ok=True)
    (folder / 'a.png').write_bytes(image_bytes(size=(6, 4), color=(10, 20, 30)))
    (folder / 'b.png').write_bytes(image_bytes(size=(5, 7), color=(200, 40, 9)))
    (folder / 'texts.json').write_text(json.dumps(['a red chair', 'two cats']), encoding='utf-8')
    images = [str(folder / 'a.png'), str(folder / 'b.png')]
    return images, str(folder / 'texts.json'), ref.prepare_inputs(project_root, images, folder / 'texts.json',
                                                                  k1_settings)


def _reference(settings, inputs, vectors):
    """A reference file in the format the reference step writes (test-only: vectors given by the test)."""
    baseline = ref.run_label(settings.dtype, 1, ref.BASELINE_ORDER)
    return {'format': ref.FORMAT, 'identity': ref._identity(settings),
            'inputs': [{'kind': item.kind, 'label': item.label, 'sha256': item.sha256} for item in inputs],
            'recipe': {'image_limits': ref.processor_limits(settings)}, 'baseline': baseline,
            'runs': {baseline: vectors}}


def test_inputs_are_prepared_as_k1_prepares_them_and_stay_inside_the_project(project_root, profile, tmp_path_factory):
    _, _, _, k1_settings = _loaded(project_root, with_check(profile, CHECK))
    _, _, inputs = _inputs(project_root, k1_settings)
    assert [item.kind for item in inputs] == ['image', 'image', 'text', 'text']
    assert [item.label for item in inputs] == ['a.png', 'b.png', 'text-0', 'text-1']
    assert all(len(item.sha256) == 64 for item in inputs) and inputs[0].data.startswith(b'\x89PNG')
    outside = tmp_path_factory.mktemp('elsewhere-rc') / 'c.png'
    outside.write_bytes(image_bytes())
    if not outside.resolve().is_relative_to(project_root.resolve()):
        with pytest.raises(ref.ReferenceCheckError, match='inside the project root'):
            ref.prepare_inputs(project_root, [str(outside)], None, k1_settings)
    bad = project_root / 'rc-inputs' / 'bad.json'
    for content in ('not json', '["ok", ""]', '{"a": 1}'):
        bad.write_text(content, encoding='utf-8')
        with pytest.raises(ref.ReferenceCheckError, match='--texts'):
            ref.prepare_inputs(project_root, [], bad, k1_settings)
    broken = project_root / 'rc-inputs' / 'broken.png'
    broken.write_bytes(b'\x89PNG not an image')
    with pytest.raises(ref.ReferenceCheckError, match='K1 preparation'):
        ref.prepare_inputs(project_root, [str(broken)], None, k1_settings)
    with pytest.raises(ref.ReferenceCheckError, match='no inputs'):
        ref.prepare_inputs(project_root, [], None, k1_settings)


@pytest.mark.parametrize('edit, message', [
    (lambda r: r.update(format='other'), 'reference file'),
    (lambda r: r['identity'].update(revision='b' * 40), 'another model'),
    (lambda r: r['identity'].update(dtype='float32'), 'another model'),
    (lambda r: r['inputs'][0].update(sha256='0' * 64), 'other inputs'),
    (lambda r: r['inputs'].pop(), 'other inputs'),
    (lambda r: r['recipe'].update(image_limits={'max_soft_tokens': 280}), 'image limits'),
    (lambda r: r.update(baseline='float32/b1/forward'), 'baseline'),
    (lambda r: r['runs'][r['baseline']][0].append(0.0), 'native vectors'),
])
def test_a_reference_made_for_something_else_is_refused(project_root, profile, edit, message):
    _, _, settings, k1_settings = _loaded(project_root, with_check(profile, CHECK))
    _, _, inputs = _inputs(project_root, k1_settings)
    reference = _reference(settings, inputs, [[1.0, 0.0, 0.0, 0.0]] * len(inputs))
    path = project_root / 'rc-reference.json'
    path.write_text(json.dumps(reference), encoding='utf-8')
    assert ref.load_reference(path, settings, inputs)['baseline'] == reference['baseline']
    edit(reference)
    path.write_text(json.dumps(reference), encoding='utf-8')
    with pytest.raises(ref.ReferenceCheckError, match=message):
        ref.load_reference(path, settings, inputs)


def _second_run(r):
    r['runs']['bfloat16/b2/forward'] = [[1.0, 0.0, 0.0, 0.0]] * (len(r['inputs']) - 1)


# E2: every field the check reads is validated; the file's own text never reaches the message
MALFORMED = [
    ('{', 'not UTF-8 JSON'), ('', 'not UTF-8 JSON'), ('[1, 2]', 'reference file'), ('null', 'reference file'),
    ('{"format": "embed_reference.v0"}', 'another model'), ('[' * 100000, 'not UTF-8 JSON'),
    (lambda r: r.update(identity=[1]), 'another model'), (lambda r: r.update(inputs='x'), 'other inputs'),
    (lambda r: r.pop('recipe'), 'image limits'), (lambda r: r.update(recipe=[]), 'image limits'),
    (lambda r: r.update(recipe={}), 'image limits'),
    (lambda r: r.pop('runs'), 'no runs'), (lambda r: r.update(runs=[]), 'no runs'),
    (lambda r: r.update(runs={}), 'no runs'),
    (lambda r: r.pop('baseline'), 'baseline'), (lambda r: r.update(baseline=['x']), 'baseline'),
    (lambda r: r.update(baseline={'a': 1}), 'baseline'),
    (lambda r: r['runs'].update({r['baseline']: 'x'}), 'one vector per input'),
    (lambda r: r['runs'].update({r['baseline']: r['runs'][r['baseline']][1:]}), 'one vector per input'),
    (_second_run, 'one vector per input'),
    (lambda r: r['runs'].update({r['baseline']: [[1, 0, 0, 0]] * len(r['inputs'])}), 'finite numbers'),
    (lambda r: r['runs'].update({r['baseline']: [[]] * len(r['inputs'])}), 'finite numbers'),
    (lambda r: r['runs'].update({r['baseline']: [[True, 0.0]] * len(r['inputs'])}), 'finite numbers'),
    (lambda r: r['runs'].update({r['baseline']: [['1.0']] * len(r['inputs'])}), 'finite numbers'),
    (lambda r: r['runs'].update({r['baseline']: [None] * len(r['inputs'])}), 'finite numbers'),
    (lambda r: r['runs'][r['baseline']].__setitem__(1, [1.0, 0.0]), 'differ in length'),
]


@pytest.fixture
def reference_case(project_root, profile):
    _, _, settings, k1_settings = _loaded(project_root, with_check(profile, CHECK))
    _, _, inputs = _inputs(project_root, k1_settings)
    return settings, inputs, _reference(settings, inputs, [[1.0, 0.0, 0.0, 0.0] for _ in inputs])


@pytest.mark.parametrize('case, message', MALFORMED)
def test_a_malformed_reference_file_is_a_reference_refusal(project_root, reference_case, case, message):
    settings, inputs, reference = reference_case
    if callable(case):
        case(reference)
        text = json.dumps(reference)
    else:
        text = case
    path = project_root / 'rc-malformed.json'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ref.ReferenceCheckError, match=message):
        ref.load_reference(path, settings, inputs)


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity', '1e400'])
def test_a_non_finite_reference_number_is_refused(project_root, reference_case, constant):
    settings, inputs, reference = reference_case
    text = json.dumps(reference).replace('[1.0, 0.0, 0.0, 0.0]', f'[{constant}, 0.0, 0.0, 0.0]', 1)
    path = project_root / 'rc-nan.json'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ref.ReferenceCheckError, match='finite numbers'):
        ref.load_reference(path, settings, inputs)


def test_a_reference_file_that_is_not_utf8_is_refused(project_root, reference_case):
    settings, inputs, _ = reference_case
    path = project_root / 'rc-latin1.json'
    path.write_bytes(b'{"format": "\xff"}')
    with pytest.raises(ref.ReferenceCheckError, match='UnicodeDecodeError'):
        ref.load_reference(path, settings, inputs)


JSON_VALUES = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=5),
                           lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner,
                                                                                       max_size=3), max_leaves=8)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_property_any_damaged_reference_file_is_accepted_or_refused_never_crashes(project_root, reference_case, data):
    """Truncated text, a field replaced by any JSON value, or a field removed: the result is the parsed reference or a
    `ReferenceCheckError`; nothing else escapes (E2)."""
    server, inputs, reference = reference_case
    damaged = deepcopy(reference)
    kind = data.draw(st.sampled_from(['truncate', 'replace', 'remove', 'replace_run']))
    if kind == 'truncate':
        text = json.dumps(damaged)
        text = text[:data.draw(st.integers(0, len(text) - 1))]
    else:
        field = data.draw(st.sampled_from(sorted(damaged)))
        if kind == 'remove':
            del damaged[field]
        elif kind == 'replace':
            damaged[field] = data.draw(JSON_VALUES)
        else:
            vectors = damaged['runs'][damaged['baseline']]
            vectors[data.draw(st.integers(0, len(vectors) - 1))] = data.draw(JSON_VALUES)
        text = json.dumps(damaged)
    path = project_root / 'rc-property.json'
    path.write_text(text, encoding='utf-8')
    try:
        loaded = ref.load_reference(path, server, inputs)
    except ref.ReferenceCheckError:
        return
    assert loaded == json.loads(text)


# --- the command line end to end -------------------------------------------------------------------------------------

def _served_vectors(k1_settings, inputs):
    async def ask():
        embedder = Embedder(k1_settings)
        try:
            images = await embedder.embed_images([item.data for item in inputs if item.kind == 'image'])
            return images + await embedder.embed_texts([item.text for item in inputs if item.kind == 'text'])
        finally:
            await embedder.close()
    return asyncio.run(ask())


def _turned(vector, cos):
    """A unit vector at exactly cosine `cos` to the unit `vector` (test-only construction)."""
    other = [0.0] * len(vector)
    index = min(range(len(vector)), key=lambda i: abs(vector[i]))
    other[index] = 1.0
    dot = sum(a * b for a, b in zip(vector, other))
    ortho = [o - dot * v for o, v in zip(other, vector)]
    norm = math.sqrt(sum(x * x for x in ortho))
    ortho = [x / norm for x in ortho]
    return [cos * v + math.sqrt(1 - cos * cos) * o for v, o in zip(vector, ortho)]


def _compare_args(path, project_root, reference_path, out, images, texts):
    return ['reference-check', 'compare', '--config', str(path), '--project-root', str(project_root),
            '--reference', str(reference_path), '--out', str(out), '--texts', texts,
            *[part for image in images for part in ('--image', image)]]


@pytest.mark.parametrize('min_cosine, status', [(0.99, cli.MISMATCH), (0.9, 0)])
def test_the_configured_tolerance_decides_the_verdict(project_root, profile, min_cosine, status, capsys):
    path, config, settings, k1_settings = _loaded(project_root, with_check(profile, tolerances(min_cosine=min_cosine)))
    images, texts, inputs = _inputs(project_root, k1_settings)
    with running_server(settings) as (base, engine, stop, result):
        served = _served_vectors(k1_settings, inputs)
        reference = _reference(settings, inputs, [list(served[0])] + [_turned(served[1], 0.95)] + served[2:])
        reference_path = project_root / 'rc-ref.json'
        reference_path.write_text(json.dumps(reference), encoding='utf-8')
        out = project_root / 'runs' / 'rc'
        done = cli.main(_compare_args(path, project_root, reference_path, out, images, texts))
    assert done == status, capsys.readouterr()
    check = json.loads((out / 'check.json').read_text(encoding='utf-8'))
    verdicts = [item['verdict'] for item in check['verdict']['items']]
    assert verdicts == ['pass', 'fail' if min_cosine > 0.95 else 'pass', 'pass', 'pass']
    assert check['verdict']['items'][1]['cosine'] == pytest.approx(0.95)
    assert check['verdict']['min_cosine'] == min_cosine and len(engine.calls) == 4  # the double served both rounds


class ScaledEngine(EngineDouble):
    """Labelled test double: the engine double's unit vectors times `scale`, as a server that skips L2 would send."""

    def __init__(self, scale):
        super().__init__()
        self.scale = scale

    def embed(self, items):
        result = super().embed(items)
        return EmbedResult([[self.scale * value for value in vector] for vector in result.vectors],
                           result.prompt_tokens)


@pytest.mark.parametrize('scale, bound, status', [
    (2.0, 0.001, cli.MISMATCH),  # the review's N1 probe: cosine 1.0, norm 2.0, passed before the fix
    (1.0003, 0.001, 0), (1.0003, 0.0002, cli.MISMATCH),  # the profile bound decides (override)
])
def test_the_norm_bound_decides_the_verdict_end_to_end(project_root, profile, scale, bound, status, capsys):
    changed = with_check(profile, tolerances(max_norm_deviation=bound))
    path, config, settings, k1_settings = _loaded(project_root, changed)
    images, texts, inputs = _inputs(project_root, k1_settings)
    with running_server(settings, engine=ScaledEngine(scale)):
        unit = [[value / scale for value in vector] for vector in _served_vectors(k1_settings, inputs)]
        reference_path = project_root / 'rc-ref-norm.json'
        reference_path.write_text(json.dumps(_reference(settings, inputs, unit)), encoding='utf-8')
        out = project_root / 'runs' / 'rc-norm'
        done = cli.main(_compare_args(path, project_root, reference_path, out, images, texts))
    captured = capsys.readouterr()
    assert done == status, captured
    check = json.loads((out / 'check.json').read_text(encoding='utf-8'))
    assert all(item['cosine'] == pytest.approx(1.0) for item in check['verdict']['items'])  # cosine alone would pass
    assert all(item['norm'] == pytest.approx(scale, rel=1e-6) for item in check['verdict']['items'])
    assert json.loads(captured.out)['max_norm_deviation'] == bound


def test_compare_refuses_an_unresolved_tolerance_before_contacting_the_server(project_root, profile, capsys):
    for check in (tolerances(min_cosine='fit_from_bench'), tolerances(max_norm_deviation='fit_from_bench')):
        path = write(project_root, with_check(profile, check), 'rc-sentinel.yaml')
        done = cli.main(['reference-check', 'compare', '--config', str(path), '--project-root', str(project_root),
                         '--reference', str(project_root / 'absent.json'), '--out', str(project_root / 'runs' / 'x')])
        captured = capsys.readouterr()
        assert done == cli.REFUSED and captured.err.startswith('refused: ') and 'fit_from_bench' in captured.err


def _compare_with(project_root, profile, monkeypatch, handler=None, engine=None, port=None, baseline=None):
    """`compare` against a real loopback server holding `engine`, through a labelled httpx.MockTransport double
    `handler` (K1's own transport hook), or against `port` where nothing listens; returns the exit status. The
    reference holds `baseline[0]` for every input (default a unit vector)."""
    changed = with_check(profile, CHECK)
    if port is not None:
        changed['vlm']['backends']['embedder']['base_urls'] = [f'http://127.0.0.1:{port}']
    path, config, settings, k1_settings = _loaded(project_root, changed, 'rc-e1.yaml')
    images, texts, inputs = _inputs(project_root, k1_settings)
    vector = [0.5, 0.5, 0.5, 0.5] if baseline is None else baseline[0]
    reference_path = project_root / 'rc-ref-e1.json'
    reference_path.write_text(json.dumps(_reference(settings, inputs, [list(vector) for _ in inputs])),
                              encoding='utf-8')
    args = _compare_args(path, project_root, reference_path, project_root / 'runs' / 'rc-e1', images, texts)
    if handler is not None:
        monkeypatch.setattr(ref, 'run_compare', partial(ref.run_compare, transport=httpx.MockTransport(handler)))
        done = cli.main(args)
    elif engine is not None:
        with running_server(settings, engine=engine):
            done = cli.main(args)
    else:
        done = cli.main(args)
    return done


SECRET = 'payload-text-that-must-not-be-printed'


def _timeout(request):
    raise httpx.ReadTimeout('timed out', request=request)


@pytest.mark.parametrize('handler, code', [
    (lambda request: httpx.Response(500, text=SECRET), 'HTTP_ERROR: status 500'),
    (_timeout, 'HTTP_ERROR: ReadTimeout'),
    (lambda request: httpx.Response(200, text=SECRET), 'RESPONSE_INVALID: response is not JSON'),
    (lambda request: httpx.Response(200, json={'data': [], 'note': SECRET}), 'RESPONSE_INVALID: expected'),
    (lambda request: httpx.Response(200, json={'data': [{'index': index, 'embedding': [1.0]} for index in (0, 1)]}),
     'DIMENSION_MISMATCH'),
])
def test_a_k1_embedding_error_is_one_refusal_line(project_root, profile, monkeypatch, capsys, handler, code):
    done = _compare_with(project_root, profile, monkeypatch, handler=handler)
    captured = capsys.readouterr()
    assert done == cli.REFUSED, captured
    assert captured.err.startswith(f'refused: {code}') and captured.err.count('\n') == 1, captured.err
    assert SECRET not in captured.err and 'Traceback' not in captured.err and captured.out == ''


def test_an_http_500_and_a_closed_port_from_a_real_server_are_refusals(project_root, profile, monkeypatch, capsys):
    done = _compare_with(project_root, profile, monkeypatch, engine=EngineDouble(fail=EngineFault(SECRET)))
    captured = capsys.readouterr()
    assert done == cli.REFUSED and captured.err == 'refused: HTTP_ERROR: status 500\n', captured
    done = _compare_with(project_root, profile, monkeypatch, port=free_port())  # nothing listens there
    captured = capsys.readouterr()
    # Windows retries a refused SYN past the 1 s connect timeout of the test profile; Linux refuses at once
    assert done == cli.REFUSED and captured.err in ('refused: HTTP_ERROR: ConnectError\n',
                                                    'refused: HTTP_ERROR: ConnectTimeout\n'), captured


def test_a_malformed_reference_is_refused_before_any_request(project_root, profile, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ref, 'run_compare', lambda *args, **kwargs: calls.append(args))
    path, config, settings, k1_settings = _loaded(project_root, with_check(profile, CHECK), 'rc-e2.yaml')
    images, texts, inputs = _inputs(project_root, k1_settings)
    for text in ('{', '{"format": "embed_reference.v0"}'):  # the review's E2 probes
        reference_path = project_root / 'rc-e2.json'
        reference_path.write_text(text, encoding='utf-8')
        done = cli.main(_compare_args(path, project_root, reference_path, project_root / 'runs' / 'e2', images, texts))
        captured = capsys.readouterr()
        assert done == cli.REFUSED and captured.err.startswith('refused: ') and 'Traceback' not in captured.err
    assert calls == []


def test_an_unexpected_error_after_the_profile_is_one_failed_line(project_root, profile, monkeypatch, capsys):
    """The `serve` convention (P1): the type only, status FAILED; a programming error is not a refusal."""
    def broken(*args, **kwargs):
        raise KeyError(SECRET)

    monkeypatch.setattr(ref, 'load_reference', broken)
    path, config, settings, k1_settings = _loaded(project_root, with_check(profile, CHECK), 'rc-failed.yaml')
    images, texts, inputs = _inputs(project_root, k1_settings)
    done = cli.main(_compare_args(path, project_root, project_root / 'any.json', project_root / 'runs' / 'f', images,
                                  texts))
    captured = capsys.readouterr()
    assert done == cli.FAILED and captured.err == 'failed: KeyError\n', captured


@pytest.mark.parametrize('step', ['serve', 'reference', 'compare'])
def test_embedding_gemma_2_with_sdpa_is_refused_before_hashing_binding_or_loading(project_root, profile, monkeypatch,
                                                                                    capsys, step):
    """F6: every command reads the profile through the shared validator, which refuses the kernel first."""
    from prompt_enhancer.embed_server import manifest

    touched = []
    for module, name in ((cli, 'verify_model'), (manifest, 'verify_model'), (cli, 'load_engine'), (cli, 'serve'),
                         (ref, 'run_reference'), (ref, 'load_reference'), (ref, 'run_compare')):
        monkeypatch.setattr(module, name, lambda *args, _name=name, **kwargs: touched.append(_name))
    changed = with_check(profile, CHECK)
    changed['vlm']['backends']['embedder']['server']['attn_implementation'] = 'sdpa'
    path = write(project_root, changed, 'rc-sdpa.yaml')
    common = ['--config', str(path), '--project-root', str(project_root)]
    image = project_root / 'rc-sdpa.png'
    image.write_bytes(image_bytes())
    inputs = ['--image', str(image), '--out', str(project_root / 'runs' / 'sdpa')]
    argv = {'serve': ['serve', *common],
            'reference': ['reference-check', 'reference', *common, *inputs, '--batch-size', '1'],
            'compare': ['reference-check', 'compare', *common, *inputs, '--reference', str(project_root / 'r.json')]}
    done = cli.main(argv[step])
    captured = capsys.readouterr()
    assert done == cli.REFUSED
    assert 'server.attn_implementation: sdpa is not supported by embedding_gemma2' in captured.err
    assert touched == [], touched


@pytest.mark.parametrize('extra, message', [(['--batch-size', '2'], 'batch-size 1'),
                                            (['--batch-size', '1'], 'model manifest')])
def test_the_reference_step_refuses_before_loading_anything(project_root, profile, extra, message, capsys):
    path = write(project_root, profile, 'rc-reference.yaml')
    (project_root / 'models' / 'manifest.json').unlink(missing_ok=True)
    folder = project_root / 'rc-inputs'
    folder.mkdir(exist_ok=True)
    (folder / 'a.png').write_bytes(image_bytes())
    done = cli.main(['reference-check', 'reference', '--config', str(path), '--project-root', str(project_root),
                     '--image', str(folder / 'a.png'), '--out', str(project_root / 'runs' / 'r'), *extra])
    captured = capsys.readouterr()
    assert done == cli.REFUSED and message in captured.err, captured.err
    assert not (project_root / 'runs' / 'r' / 'reference.json').exists()


class _PairsOnly:
    """Labelled test double of transformers 5's `SizeDict`: not a dict, iterates its set fields as pairs."""

    def __iter__(self):
        yield from (('longest_edge', 1310720), ('shortest_edge', 4096))


def test_the_recipe_records_image_limits_held_in_a_non_dict_size_type():
    from types import SimpleNamespace

    image = SimpleNamespace(size=_PairsOnly(), max_soft_tokens=None)
    module = SimpleNamespace(processor=SimpleNamespace(image_processor=image))
    model = type('M', (list,), {'default_prompt_name': 'default', 'prompts': {}, 'max_seq_length': 8})([module])
    recipe = ref._recipe(model)
    assert recipe['image_limits'] == {'size': {'longest_edge': 1310720, 'shortest_edge': 4096}}
    json.dumps(recipe)  # the P2 run failed here before the fix: SizeDict is not JSON serialisable


# --- P3: zero and huge vectors (RT1-N1, RT1-E2, overflow class) -----------------------------------------------------

def test_a_finite_vector_with_huge_values_has_a_length_and_a_cosine():
    """`math.fsum` of the squares overflows for values near 1e154; the length is measured without squaring
    (`math.hypot`), so a finite vector never raises `OverflowError` (P3 class fix)."""
    huge = [1e154] * 4
    assert ref.vector_norm(huge) == pytest.approx(2e154)
    assert ref.cosine(huge, [1.0, 1.0, 1.0, 1.0]) == pytest.approx(1.0)
    assert ref.cosine(huge, huge) == pytest.approx(1.0)  # the raw dot product (4e308) would overflow
    assert ref.norm_deviation(huge) == pytest.approx(2e154)
    assert ref.vector_norm([1.5e308, 1.5e308]) == math.inf  # the true length (2.1e308) overflows: never a pass
    assert ref.vector_norm([3.0, 4.0]) == 5.0 and ref.vector_norm([0.0, 0.0]) == 0.0


def test_the_cosine_of_subnormal_vectors_is_accurate():
    """F3 (P3 fix round 1): `hypot([5e-324] * 2)` rounds to 5e-324, so dividing by it gave cosine 1.0 against an axis
    and 2.0 with itself; scaling by the largest magnitude first gives the true values."""
    tiny = [5e-324, 5e-324]
    assert ref.cosine(tiny, [1.0, 0.0]) == pytest.approx(0.7071067811865476, abs=1e-15)
    assert ref.cosine(tiny, tiny) == pytest.approx(1.0, abs=1e-15)
    assert ref.unit_vector(tiny) == pytest.approx([0.7071067811865476] * 2, abs=1e-15)


def _exact_cosine(a, b):
    """Oracle: the exact dot product (`Fraction`) over the square root of the exact squared lengths (50 digits)."""
    from decimal import Decimal, localcontext
    from fractions import Fraction

    dot = sum(Fraction(x) * Fraction(y) for x, y in zip(a, b))
    squares = sum(Fraction(x) ** 2 for x in a) * sum(Fraction(y) ** 2 for y in b)
    with localcontext() as context:
        context.prec = 50
        return float(Decimal(dot.numerator) / Decimal(dot.denominator)
                     / (Decimal(squares.numerator) / Decimal(squares.denominator)).sqrt())


_unit_floats = st.lists(st.floats(-1, 1, allow_nan=False), min_size=1, max_size=6)
_NORMAL = 2.2250738585072014e-308  # test-only: the smallest positive normal float64


@settings(max_examples=400, deadline=None)
@given(_unit_floats, _unit_floats, st.integers(-1074, 1023), st.integers(-1074, 1023))
def test_property_the_cosine_is_accurate_and_scale_invariant(a, b, e, f):
    """F3 class: any finite vectors, scaled by powers of two down to the subnormal range and up to the largest
    exponent, give the exact cosine within 1e-12 and never leave [-1, 1] by more than rounding; a scaling that keeps
    every component normal changes the cosine not at all."""
    b = (b * 6)[:len(a)]
    scaled_a, scaled_b = [math.ldexp(x, e) for x in a], [math.ldexp(y, f) for y in b]
    if not (ref.usable(ref.vector_norm(scaled_a)) and ref.usable(ref.vector_norm(scaled_b))):
        with pytest.raises(ref.ReferenceCheckError):
            ref.cosine(scaled_a, scaled_b)
        return
    value = ref.cosine(scaled_a, scaled_b)
    assert abs(value - _exact_cosine(scaled_a, scaled_b)) <= 1e-12 and abs(value) <= 1 + 1e-15
    exact = all(math.ldexp(x, -e) == y for x, y in zip(scaled_a, a)) and \
        all(math.ldexp(x, -f) == y for x, y in zip(scaled_b, b))  # no component rounded or flushed to zero
    normal = all(x == 0 or abs(x) >= _NORMAL for x in scaled_a + scaled_b + a + b)
    if exact and normal and ref.usable(ref.vector_norm(a)) and ref.usable(ref.vector_norm(b)):
        assert value == ref.cosine(a, b)  # power-of-two scaling is exact, and so is the unit vector


@pytest.mark.parametrize('served', [[0.0, 0.0], [0.0, -0.0]])
def test_a_zero_served_vector_is_a_failed_item_not_an_error(served):
    """RT1-N1: the norm is classified before any cosine; a zero vector fails its item (cosine null, norm 0)."""
    inputs = [ref.Input('text', 'text-0', 'x', None, 't'), ref.Input('text', 'text-1', 'y', None, 'u')]
    verdict = ref.judge(inputs, [[0.6, 0.8], [0.6, 0.8]], [served, [0.6, 0.8]], 0.5, 0.5, 2)
    assert [item['verdict'] for item in verdict['items']] == ['fail', 'pass'] and not verdict['passed']
    assert verdict['items'][0]['cosine'] is None and verdict['items'][0]['norm'] == 0.0


def _zero_vectors(request):
    """Labelled test double of the server: native-dimension zero vectors for every input of the request."""
    count = len(json.loads(request.content)['input'])
    return httpx.Response(200, json={'data': [{'index': index, 'embedding': [0.0] * 4} for index in range(count)]})


def test_a_zero_served_vector_is_a_mismatch_with_a_written_check(project_root, profile, monkeypatch, capsys):
    """RT1-N1 end to end: the review's reproduction (unit reference, zero response through K1) exits MISMATCH and
    writes check.json; before the fix it was `refused: a zero or non-finite vector`, exit 2."""
    reference = [[1.0, 0.0, 0.0, 0.0]]
    done = _compare_with(project_root, profile, monkeypatch, handler=_zero_vectors, baseline=reference)
    captured = capsys.readouterr()
    assert done == cli.MISMATCH, captured
    check = json.loads((project_root / 'runs' / 'rc-e1' / 'check.json').read_text(encoding='utf-8'))
    assert all(item['cosine'] is None and item['norm'] == 0.0 and item['verdict'] == 'fail'
               for item in check['verdict']['items'])
    assert json.loads(captured.out)['cosines'] == [None] * 4


@pytest.mark.parametrize('vector, message', [([0.0, 0.0, 0.0, 0.0], 'zero or non-finite length'),
                                             ([-0.0, 0.0, 0.0, 0.0], 'zero or non-finite length'),
                                             ([1.5e308, 1.5e308, 0.0, 0.0], 'zero or non-finite length')])
def test_a_reference_vector_without_a_usable_length_is_refused_before_any_request(project_root, profile, monkeypatch,
                                                                                  capsys, vector, message):
    """RT1-E2: the artifact check admits only vectors that admit a cosine; the counting transport sees no call."""
    calls = []

    def counting(request):
        calls.append(request)
        return _zero_vectors(request)

    done = _compare_with(project_root, profile, monkeypatch, handler=counting, baseline=[vector])
    captured = capsys.readouterr()
    assert done == cli.REFUSED and message in captured.err, captured
    assert calls == []


def test_a_reference_vector_with_huge_finite_values_is_admitted(project_root, reference_case):
    settings, inputs, reference = reference_case
    reference['runs'][reference['baseline']] = [[1e154, 0.0, 0.0, 1e154] for _ in inputs]
    path = project_root / 'rc-huge.json'
    path.write_text(json.dumps(reference), encoding='utf-8')
    assert ref.load_reference(path, settings, inputs)['baseline'] == reference['baseline']


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=6),
       st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=6))
def test_property_the_verdict_never_raises_for_finite_vectors(expected, got):
    """Any finite reference and served vector gives a verdict (pass or fail), never an exception; a pass needs a
    finite, non-zero served length within the bound."""
    inputs = [ref.Input('text', 'text-0', 'x', None, 't')]
    try:
        ref._check_runs({'r': [expected]}, 1)
    except ref.ReferenceCheckError:
        assert ref.vector_norm(expected) in (0.0, math.inf)
        return
    verdict = ref.judge(inputs, [expected], [got], 0.5, 0.5, len(expected))
    item = verdict['items'][0]
    if item['verdict'] == 'pass':
        assert 0.5 <= item['norm'] <= 1.5 and item['cosine'] >= 0.5
    if item['norm'] in (0.0, math.inf):
        assert item['cosine'] is None and item['verdict'] == 'fail'


# --- P3: reports are written through the anchored storage layer (PM integration fix of `_write`) -------------------

def _writer_settings(project_root):
    from types import SimpleNamespace

    return SimpleNamespace(project_root=project_root)


def test_a_report_is_written_below_the_root_through_the_anchored_layer(project_root, monkeypatch):
    from prompt_enhancer.storage import _anchored

    calls = []
    real = _anchored.atomic_write_bytes

    def spy(root, relative, data, check=None):
        calls.append((root, relative))
        return real(root, relative, data, check)

    monkeypatch.setattr(_anchored, 'atomic_write_bytes', spy)
    out = project_root.resolve() / 'runs' / 'p3-write' / 'check.json'
    ref._write(_writer_settings(project_root), out, {'b': 1, 'a': [1.5]})
    assert calls == [(project_root.resolve(), 'runs/p3-write/check.json')]
    assert json.loads(out.read_text(encoding='utf-8')) == {'a': [1.5], 'b': 1}
    assert sorted(path.name for path in out.parent.iterdir()) == ['check.json']  # no temporary left


def test_a_report_outside_the_root_is_refused_with_a_structural_message(tmp_path_factory):
    root = tmp_path_factory.mktemp('p3-root').resolve()  # a root of its own, so the sibling lies outside it
    outside = tmp_path_factory.mktemp('p3-outside').resolve()
    with pytest.raises(ref.ReferenceCheckError) as raised:
        ref._write(_writer_settings(root), outside / 'check.json', {'a': 1})
    assert str(raised.value) == 'cannot write check.json (ValueError)'  # the file name only, never a path
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('segment', ['nul', 'a.', 'a?b'])
def test_a_report_path_with_a_segment_the_storage_rule_refuses_is_refused(project_root, segment):
    with pytest.raises(ref.ReferenceCheckError, match=r'^cannot write check\.json \(StorageError\)$'):
        ref._write(_writer_settings(project_root), project_root.resolve() / 'runs' / segment / 'check.json', {})


def test_a_link_below_the_root_is_refused_and_nothing_is_written_through_it(project_root, tmp_path_factory):
    import os

    target = tmp_path_factory.mktemp('p3-link-target').resolve()
    link = project_root.resolve() / 'runs' / 'p3-link'
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f'synthetic symlink creation unavailable: {error}')
    try:
        with pytest.raises(ref.ReferenceCheckError, match=r'^cannot write check\.json \(StorageError\)$'):
            ref._write(_writer_settings(project_root), link / 'check.json', {'a': 1})
        assert list(target.iterdir()) == []
    finally:
        os.rmdir(link) if os.name == 'nt' else link.unlink()


def _failing_write(root, relative, data, check=None):
    """Labelled I/O fault double of `atomic_write_bytes`: the OS refuses the write, naming the absolute path."""
    raise PermissionError(13, 'synthetic report I/O error', str(Path(root) / relative))


def test_a_report_io_error_is_refused_with_the_file_name_and_type_only(project_root, monkeypatch):
    """F4 (P3 fix round 1): an `OSError` at the writer is a structural refusal, never its path-bearing text."""
    from prompt_enhancer.storage import _anchored

    monkeypatch.setattr(_anchored, 'atomic_write_bytes', _failing_write)
    with pytest.raises(ref.ReferenceCheckError) as raised:
        ref._write(_writer_settings(project_root), project_root.resolve() / 'runs' / 'p3-io' / 'check.json', {})
    assert str(raised.value) == 'cannot write check.json (PermissionError)'


def _unit_vectors(request):
    """Labelled test double of the server: unit vectors of the native dimension for every input."""
    count = len(json.loads(request.content)['input'])
    return httpx.Response(200, json={'data': [{'index': i, 'embedding': [0.5] * 4} for i in range(count)]})


def test_compare_prints_a_report_io_error_without_the_path(project_root, profile, monkeypatch, capsys):
    from prompt_enhancer.storage import _anchored

    monkeypatch.setattr(_anchored, 'atomic_write_bytes', _failing_write)
    done = _compare_with(project_root, profile, monkeypatch, handler=_unit_vectors)
    captured = capsys.readouterr()
    assert done == cli.REFUSED and captured.err == 'refused: cannot write check.json (PermissionError)\n', captured
    written = project_root.resolve() / 'runs' / 'rc-e1' / 'check.json'
    assert str(written) not in captured.err and repr(str(written)) not in captured.err


@pytest.mark.parametrize('protected', ['models', '.secrets', '.git', '.venv', '.venv-gpu'])
def test_reference_check_refuses_an_out_dir_in_a_protected_directory(tmp_path_factory, protected):
    """F2 class: the report directory, after resolving links, is never below a protected project directory."""
    import os

    root = tmp_path_factory.mktemp('p3-protected').resolve()  # a synthetic root of its own
    (root / protected).mkdir()
    with pytest.raises(ref.ReferenceCheckError, match='protected project directory'):
        ref.check_output_dir(root, root / protected / 'out')
    link = root / 'runs' / 'alias'
    link.parent.mkdir()
    _directory_link(root / protected, link)
    try:
        with pytest.raises(ref.ReferenceCheckError, match='protected project directory'):
            ref.check_output_dir(root, link / 'out')
        assert ref.check_output_dir(root, root / 'runs' / 'out') == root / 'runs' / 'out'  # override: allowed
    finally:
        os.rmdir(link) if os.name == 'nt' else link.unlink()
    assert list((root / protected).iterdir()) == []


def _directory_link(target, link):
    """A junction on Windows (no privilege needed), a symlink elsewhere."""
    import os

    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f'synthetic symlink creation unavailable: {error}')


@pytest.mark.parametrize('family, dtype, expected', [
    ('embedding_gemma2', 'bfloat16', ('bfloat16', 'float32')),  # the EG2 card: bfloat16 or float32
    ('embedding_gemma2', 'float32', ('float32',)),  # the profile dtype is the baseline and is not run twice
    ('qwen3_vl_embedding', 'bfloat16', ('bfloat16',)),  # the Qwen card names no other dtype (P2 stage B)
])
def test_the_fit_runs_the_profile_dtype_first_then_the_card_s_other_dtypes(family, dtype, expected):
    from types import SimpleNamespace

    from prompt_enhancer.embed_server.families import FAMILIES

    assert ref.reference_dtypes(SimpleNamespace(dtype=dtype, family=FAMILIES[family])) == expected
