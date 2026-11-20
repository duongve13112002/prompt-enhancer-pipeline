"""MS-3 (decision 179, R-001, R-004): a one-image request keeps today's bytes end to end. The baselines under
`tests/fixtures/ms3_baseline/` were captured on the unchanged code at 1100f90 (production S0 to S6 against the labelled
loopback model double; S0 and the request parser on tiny synthetic images) and are compared here for a `request.v0` and
for a one-item `request.v1`."""

import json
from pathlib import Path

import pytest

import intake_fixtures
from intake_fixtures import activate, configuration
from ms3_fixtures import committed_root, differences, images, install_committed_prompts, normalized, run_pipeline
from prompt_enhancer.cli_input import Refusal, parse_request
from prompt_enhancer.intake import intake

FIXTURES = Path(__file__).parent / 'fixtures' / 'ms3_baseline'
PIPELINE = FIXTURES / 'edit_one_image.json'
INTAKE = FIXTURES / 'intake_one_image.json'
PROMPT = 'original intent'


def request_v0(**changes):
    return {'prompt': PROMPT, 'task': 'edit', 'source_image': images(1)[0], **changes}


def request_v1():
    return {'prompt': PROMPT, 'task': 'edit', 'source_images': images(1)}


@pytest.mark.parametrize('shape', ['v0', 'v1'])
def test_pipeline_bytes_equal_the_baseline(model_test_profile, tmp_path, project_root, shape, monkeypatch):
    request = request_v0() if shape == 'v0' else request_v1()
    monkeypatch.setattr(intake_fixtures, 'install_prompts', install_committed_prompts)
    root = committed_root(tmp_path)  # prompt and schema bytes independent of the checkout's line ends
    actual = normalized(run_pipeline(model_test_profile, tmp_path, root, request))
    baseline = json.loads(PIPELINE.read_text(encoding='utf-8'))
    assert actual == baseline, differences(actual, baseline)
    # the baseline holds the planner call with its image, the brief and the writer call
    assert baseline['stages']['brief'] and any('image_url' in str(call) for call in baseline['calls'])


@pytest.fixture
def env(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    activate(config, project_root)
    return config


def intake_case(config, project_root, request):
    with intake(request, config, project_root=project_root) as result:
        payload = dict(result.payload, request_id='<id>', config_hash='<hash>', effective_config='<config>')
        if payload['envelope'] is not None:
            payload['envelope'] = dict(payload['envelope'], trace_id='<id>')  # the request id, random per request
        return json.loads(json.dumps(payload, sort_keys=True))


def parse_case(request):
    try:
        return {'accepted': parse_request(json.dumps(request).encode(), Path('.').resolve())}
    except Refusal as error:
        return {'refused': str(error)}


def cases():
    good = images(1)[0]
    return {
        'ready': request_v0(),
        'bad_base64': request_v0(source_image='not base64!'),
        'not_an_image': request_v0(source_image='YmFkIGJ5dGVz'),
        'edit_without_source': {'prompt': PROMPT, 'task': 'edit'},
        'edit_null_source': {'prompt': PROMPT, 'task': 'edit', 'source_image': None},
        'generate_with_source': {'prompt': PROMPT, 'task': 'generate', 'source_image': good},
        'generate': {'prompt': PROMPT, 'task': 'generate'},
        'unknown_field': request_v0(width=3),
    }


def test_intake_and_parser_bytes_equal_the_baseline(env, project_root):
    actual = {name: {'intake': intake_case(env, project_root, request), 'parse': parse_case(request)}
              for name, request in cases().items()}
    baseline = json.loads(INTAKE.read_text(encoding='utf-8'))
    assert actual == baseline, differences(actual, baseline)
    assert baseline['ready']['intake']['source_image']['role'] == 'source_image'
