"""Decision 188 (b): the plain `image` probe retries a PROBE_MALFORMED reply with another seed (labelled HTTP double).

The double answers every challenge from its pixels (ProductionServer) and wraps the answer in a code fence on the
scripted attempts, which the strict probe parser refuses (PROBE_MALFORMED). Nothing else is ever retried.
"""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
import yaml

from prompt_enhancer import model_probe as probes
from prompt_enhancer.config import ConfigurationError, load_config
from prompt_enhancer.model_client import ModelClientRun
from test_kb_sync_production_probes import ProductionServer
from test_model_production_probes import bind_double, production_env  # noqa: F401  (fixture)

BASE_SEED = 5000


def plain_image(body):
    content = body['messages'][-1]['content']
    return (isinstance(content, list) and any(part['type'] == 'image_url' for part in content)
            and 'response_format' not in body)


class FencedImageServer:
    """Fences the reply of a plain image challenge while its attempt index (seed - base) is below `fail_first`."""

    def __init__(self, fail_first, wrong=False, http_500_at=None):
        self.double = ProductionServer()
        self.fail_first, self.wrong, self.http_500_at = fail_first, wrong, http_500_at
        self.image_bodies = []

    def handler(self, request):
        body = json.loads(request.content)
        answer = self.double.answer(request.url.path, body)
        if plain_image(body):
            self.image_bodies.append(body)
            index = body.get('seed', BASE_SEED) - BASE_SEED
            if index == self.http_500_at:  # a transport failure of that attempt (HTTP 500)
                return httpx.Response(500, json={'error': 'scripted failure'})
            message = answer['choices'][0]['message']
            if self.wrong:
                message['content'] = json.dumps({'value': 99})
            elif index < self.fail_first:
                message['content'] = '```json\n' + message['content'] + '\n```'
        return httpx.Response(200, json=answer)


def load(env, *, attempts, max_requests=40, seed=BASE_SEED, name='probe-retry.yaml'):
    backend = env.config['vlm']['backends']['main']
    backend['probe'].update(image_attempts=attempts, max_requests=max_requests)
    for role in env.config['vlm']['roles'].values():  # the loader needs a seed base on every role with an image
        if isinstance(role.get('image'), dict) or role is env.config['vlm']['roles']['observer']:
            role['sampling'] = {**role['sampling'], 'seed': seed}
    path = env.root / name
    path.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    return load_config(path, 'kb sync', project_root=env.root)


def run_probe(env, config, server, role='observer', start_on=None):
    """Probe the role's backend; `start_on` names the backend an image call of a role bound elsewhere falls back to
    (`RoleClient._call` starts exactly that backend with the role's name)."""
    async def execute():
        run = ModelClientRun(config, env.root)
        bind_double(run, env, handler=server.handler)
        try:
            if start_on is None:
                await run.for_role(role)
            else:
                await run.client.start_backend(start_on, role_name=role)
            events = [event for event in run.client.probe_events if event['capability'] == 'image']
            return run.probes['main'], events, run.client._probe_allowances['main'].sent
        finally:
            await run.close()
    return asyncio.run(execute())


def challenge_count(env):
    return probes.probe_image_challenges(env.root)


def attempts_of(event):
    return [[(item['attempt'], item['seed'], item['code']) for item in challenge['attempts']]
            for challenge in event['challenges']]


def test_pass_on_the_second_attempt_keeps_the_failed_attempt_in_the_evidence(production_env):
    env = production_env
    server = FencedImageServer(fail_first=1)
    proof, (event,), sent = run_probe(env, load(env, attempts=3), server)
    assert proof.capabilities['image'] is True and event['status'] == 'complete'
    count = challenge_count(env)
    for log in attempts_of(event):
        assert log == [(0, BASE_SEED, 'PROBE_MALFORMED'), (1, BASE_SEED + 1, None)]
    assert len(server.image_bodies) == 2 * count
    digests = [item['reply_digest'] for challenge in event['challenges'] for item in challenge['attempts']]
    assert all(isinstance(value, str) and len(value) == 64 for value in digests)


@pytest.mark.parametrize('fail_first', [0, 1, 2, 3])
def test_negative_only_after_the_last_attempt_fails(production_env, fail_first):
    env = production_env
    server = FencedImageServer(fail_first=fail_first)
    proof, (event,), _ = run_probe(env, load(env, attempts=3), server)
    count = challenge_count(env)
    if fail_first < 3:
        assert proof.capabilities['image'] is True and event['status'] == 'complete'
        assert len(server.image_bodies) == count * (fail_first + 1)
    else:
        assert proof.capabilities['image'] is False and proof.errors['image'] == 'PROBE_MALFORMED'
        assert event['status'] == 'negative' and len(server.image_bodies) == 3 * count
        for log in attempts_of(event):  # a failed probe keeps every attempt
            assert log == [(k, BASE_SEED + k, 'PROBE_MALFORMED') for k in range(3)]


def test_a_retry_changes_only_the_seed(production_env):
    env = production_env
    server = FencedImageServer(fail_first=2)
    run_probe(env, load(env, attempts=3), server)
    stripped = [{key: value for key, value in body.items() if key != 'seed'} for body in server.image_bodies]
    assert len({json.dumps(body, sort_keys=True) for body in stripped}) == challenge_count(env)  # one per challenge
    seeds = {}
    for body in server.image_bodies:
        seeds.setdefault(json.dumps({k: v for k, v in body.items() if k != 'seed'}, sort_keys=True), []).append(body['seed'])
    assert all(value == [BASE_SEED, BASE_SEED + 1, BASE_SEED + 2] for value in seeds.values())


def test_a_seeded_retry_call_reuses_the_proof_of_the_configured_role(production_env):
    """The proof key is made from the configured role: a call with a retry seed neither re-probes nor adds a key."""
    env = production_env
    config = load(env, attempts=2)

    async def execute():
        run = ModelClientRun(config, env.root)
        double = bind_double(run, env)
        try:
            await run.for_role('info_assessor')
            sent, keys = len(double.probe_requests), set(run.client._production_proofs)
            for seed in (None, BASE_SEED + 1, BASE_SEED + 2):
                await run.client.call('info_assessor', b'system', json.dumps({'folder_name': 'f'}), seed=seed)
            return sent, len(double.probe_requests), keys, set(run.client._production_proofs)
        finally:
            await run.close()
    sent, after, keys, after_keys = asyncio.run(execute())
    assert after == sent and after_keys == keys


@pytest.mark.parametrize('failing_attempt', [1, 2])
def test_a_transport_failure_on_a_retry_is_failed_never_negative_or_incomplete(production_env, failing_attempt):
    """Attempt 0 is malformed (retried); the retry itself gets HTTP 500: no verdict, a failed probe (PROBE_FAILED)."""
    env = production_env
    server = FencedImageServer(fail_first=failing_attempt, http_500_at=failing_attempt)
    proof, (event,), _ = run_probe(env, load(env, attempts=4), server)
    count = challenge_count(env)
    assert event['status'] == 'failed' and event['reason'] == 'PROBE_FAILED'
    assert proof.capabilities.get('image') is None and proof.errors['image'] == 'PROBE_FAILED'
    assert {case['status'] for case in event['challenges']} == {'failed'}
    assert len(server.image_bodies) == count * (failing_attempt + 1)  # the failed attempt ends the challenge
    for log in attempts_of(event):  # entries before the failure and the failed attempt itself stay in the log
        assert log == [(k, BASE_SEED + k, 'PROBE_MALFORMED') for k in range(failing_attempt)] + [
            (failing_attempt, BASE_SEED + failing_attempt, 'PROBE_FAILED')]


def test_a_wrong_answer_is_a_verdict_and_is_never_retried(production_env):
    env = production_env
    server = FencedImageServer(fail_first=0, wrong=True)
    proof, (event,), _ = run_probe(env, load(env, attempts=4), server)
    assert len(server.image_bodies) == challenge_count(env)
    assert proof.capabilities['image'] is False and event['status'] == 'negative'
    assert proof.errors['image'] == 'PROBE_WRONG_ANSWER'


def test_one_attempt_is_the_unchanged_single_probe(production_env):
    env = production_env
    server = FencedImageServer(fail_first=9)
    proof, (event,), _ = run_probe(env, load(env, attempts=1), server)
    assert len(server.image_bodies) == challenge_count(env) and proof.errors['image'] == 'PROBE_MALFORMED'
    assert all('attempts' not in challenge for challenge in event['challenges'])


def test_retries_draw_from_the_probe_allowance_and_an_unpaid_retry_is_incomplete_never_negative(production_env):
    env = production_env
    count = challenge_count(env)
    config = load(env, attempts=3, max_requests=40)
    # The plain `image` probe first sends text, json_schema and `count` image challenges (2 + count requests). The
    # allowance then pays attempt 1 of every challenge (`count`) and ONE more: one challenge gets attempt 2, the
    # others end without a verdict (PROBE_INCOMPLETE), which is never negative.
    allowance = 2 + count + count + 1
    config['vlm']['backends']['main']['probe']['max_requests'] = allowance
    server = FencedImageServer(fail_first=9)
    proof, (event,), sent = run_probe(env, config, server)
    assert sent == allowance and len(server.image_bodies) == count + count + 1
    assert sorted(len(log) for log in attempts_of(event)) == [2] * (count - 1) + [3]  # unsent attempts leave no log entry
    assert proof.capabilities.get('image') is None and proof.errors['image'] == 'PROBE_INCOMPLETE'
    assert event['status'] == 'incomplete'


def test_load_refuses_attempts_the_allowance_cannot_pay_and_names_both_keys(production_env):
    env = production_env
    count = challenge_count(env)
    with pytest.raises(ConfigurationError) as caught:
        load(env, attempts=4, max_requests=2 + 2 * count + 1)
    message = str(caught.value)
    assert 'probe.image_attempts' in message and 'probe.max_requests' in message
    assert f'{count} challenges x 3 retries = {3 * count}' in message
    load(env, attempts=4, max_requests=2 + 2 * count + 3 * count)  # exactly enough is accepted


@pytest.mark.parametrize('seed', ['none', None, -1])  # a boolean is refused earlier, by the role schema
def test_load_refuses_probe_attempts_without_an_integer_seed(production_env, seed):
    env = production_env
    with pytest.raises(ConfigurationError, match=r'image_attempts.*sampling\.seed'):
        load(env, attempts=2, seed=seed)


def cloud_planner_profile(env, *, main_attempts, cloud_attempts, seed=BASE_SEED, **cloud_changes):
    """The planner (an image role, plain text output so the loader accepts a cloud backend) sits on a whole anthropic
    backend that does not take images (`allow_images: false`), so its image call and plain `image` probe run on the
    default local backend `main`, which maps `sampling.seed`."""
    from test_model_production_probes import synthetic_provider_backend

    cloud = synthetic_provider_backend(env, 'anthropic')
    cloud.update({'allow_images': False, **cloud_changes})
    cloud['probe']['image_attempts'] = cloud_attempts
    env.config['vlm']['backends']['cloud'] = cloud
    planner = env.config['vlm']['roles']['planner'] = deepcopy(env.config['vlm']['roles']['observer'])  # a resolved role
    planner['backend'] = 'cloud'
    planner['output'] = {**planner['output'], 'format': 'none', 'schema_id': 'none', 'strict': False,
                         'validate_response': False}
    return load(env, attempts=main_attempts, seed=seed)


def test_a_role_on_a_backend_that_does_not_image_probe_retries_on_the_backend_that_does(production_env):
    """F3 direction 1: the probe of this role runs on `main` (seed-mapping), so `main`'s `image_attempts` and seed
    support decide, at load and at run time, not the anthropic backend the role names."""
    env = production_env
    config = cloud_planner_profile(env, main_attempts=3, cloud_attempts=1)
    server = FencedImageServer(fail_first=1)
    proof, (event,), _ = run_probe(env, config, server, role='planner', start_on='main')
    assert event['role'] == 'planner'
    assert proof.capabilities['image'] is True and event['status'] == 'complete'
    for log in attempts_of(event):
        assert log == [(0, BASE_SEED, 'PROBE_MALFORMED'), (1, BASE_SEED + 1, None)]
    assert len(server.image_bodies) == 2 * challenge_count(env)


def test_a_backend_that_never_image_probes_is_not_asked_for_a_seed(production_env):
    """F3 direction 2: `image_attempts: 5` on a backend that never image-probes (anthropic, no images) is not used, so
    its missing `seed` mapping refuses nothing, while the probing backend's own `image_attempts` stays 1."""
    env = production_env
    config = cloud_planner_profile(env, main_attempts=1, cloud_attempts=5)
    assert config['vlm']['backends']['cloud']['probe']['image_attempts'] == 5
    server = FencedImageServer(fail_first=9)
    proof, (event,), _ = run_probe(env, config, server, role='planner', start_on='main')
    assert len(server.image_bodies) == challenge_count(env) and proof.errors['image'] == 'PROBE_MALFORMED'


@pytest.mark.parametrize('seed', [BASE_SEED, 'none'])
def test_a_stopping_backend_without_a_seed_field_that_image_probes_needs_no_seed(production_env, seed):
    """Decision 205: with `allow_images: true` the anthropic backend probes images itself. With `stop` no backend that
    maps a seed is reachable, so `image_attempts: 2` loads with or without `sampling.seed`."""
    env = production_env
    config = cloud_planner_profile(env, main_attempts=1, cloud_attempts=2, seed=seed, allow_images=True,
                                   on_budget_exhausted='stop')
    assert config['vlm']['backends']['cloud']['probe']['image_attempts'] == 2
    assert config['vlm']['roles']['planner']['sampling']['seed'] == seed


@pytest.mark.parametrize('seed, refused', [('none', True), (BASE_SEED, False)])
def test_a_fallback_to_a_seed_mapping_backend_still_asks_for_a_seed(production_env, seed, refused):
    """With `fallback_to_default_backend` the local `main` (it maps a seed) is reachable, so the role needs a base seed
    for the retry that may run there."""
    env = production_env
    build = lambda: cloud_planner_profile(env, main_attempts=1, cloud_attempts=2, seed=seed, allow_images=True,  # noqa: E731
                                          on_budget_exhausted='fallback_to_default_backend')
    if not refused:
        assert build()['vlm']['roles']['planner']['sampling']['seed'] == seed
        return
    with pytest.raises(ConfigurationError, match=r'sampling\.seed'):
        build()


class AnthropicFencedImages:
    """Labelled Anthropic-shaped HTTP double: the first reply of every plain image challenge is code-fenced, which the
    strict probe parser refuses (PROBE_MALFORMED). A challenge is told apart by its whole body: with no seed, a
    retry sends the same bytes again."""

    def __init__(self):
        from test_model_production_probes import CloudProbeDouble
        self.double = CloudProbeDouble()
        self.image_bodies, self.seen = [], {}

    def handler(self, request):
        body = json.loads(request.content)
        answer = self.double.answer(request.url.path, body)
        content = body['messages'][-1]['content']
        if isinstance(content, list) and any(part['type'] == 'image' for part in content) \
                and 'format' not in body.get('output_config', {}):
            self.image_bodies.append(body)
            key = json.dumps(body, sort_keys=True)
            self.seen[key] = self.seen.get(key, 0) + 1
            if self.seen[key] == 1:
                answer['content'][0]['text'] = '```json\n' + answer['content'][0]['text'] + '\n```'
        return httpx.Response(200, json=answer)


@pytest.mark.parametrize('seed', [BASE_SEED, 'none'])
def test_an_anthropic_image_probe_retry_is_a_fresh_unseeded_sample(production_env, monkeypatch, seed):
    """CLOUD-1 (C), fix round 1: the plain `image` probe of a backend with no seed field retries a PROBE_MALFORMED
    reply once per configured attempt, sends no seed, and records `seed: None` plus `seed_field: none_in_protocol`,
    also when the role states an integer base seed (the protocol cannot send it)."""
    from prompt_enhancer import model_client
    from test_model_production_probes import bind_cloud_double, cloud_profile
    env = production_env
    monkeypatch.setattr(model_client, 'cloud_headers', lambda *args: {})  # labelled credential loader double
    config = cloud_profile(env, budget=100, policy='stop')
    cloud = config['vlm']['backends']['cloud_claude']
    cloud['probe'].update(image_attempts=2, max_requests=40)
    role = config['vlm']['roles']['observer']
    role['sampling'] = {**role['sampling'], 'seed': seed}
    path = env.root / 'anthropic-image-retry.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    loaded = load_config(path, 'kb status', project_root=env.root)
    server = AnthropicFencedImages()

    async def execute():
        run = ModelClientRun(loaded, env.root)
        bind_cloud_double(run, server.double, handler=server.handler)
        try:
            await run.for_role('observer')
            return [event for event in run.client.probe_events if event['capability'] == 'image'], \
                run.probes['cloud_claude']
        finally:
            await run.close()

    (event,), proof = asyncio.run(execute())
    count = challenge_count(env)
    assert event['status'] == 'complete' and proof.capabilities['image'] is True
    assert len(server.image_bodies) == 2 * count and set(server.seen.values()) == {2}   # the configured attempts, no more
    assert all('seed' not in body for body in server.image_bodies)
    entries = [item for challenge in event['challenges'] for item in challenge['attempts']]
    assert len(entries) == 2 * count
    assert [(item['attempt'], item['seed'], item['code'], item['seed_field']) for item in entries[:2]] == [
        (0, None, 'PROBE_MALFORMED', 'none_in_protocol'), (1, None, None, 'none_in_protocol')]
    assert all(item['seed'] is None and item['seed_field'] == 'none_in_protocol' for item in entries)


def test_an_unreadable_probe_bundle_is_refused_never_skipped(production_env):
    """F4: the bundle is read from the loader's project root; unreadable with `image_attempts > 1` is a refusal that
    names the bundle and the root, and with one attempt nothing needs it."""
    env = production_env
    bundle = env.root / probes.BUNDLE_PATH
    assert bundle.is_file()
    bundle.unlink()
    load(env, attempts=1, name='no-bundle-one-attempt.yaml')
    with pytest.raises(ConfigurationError) as caught:
        load(env, attempts=2, name='no-bundle-two-attempts.yaml')
    message = str(caught.value)
    assert 'vlm.backends.main.probe.image_attempts' in message and probes.BUNDLE_PATH.as_posix() in message
    assert str(env.root) in message and 'cannot be read' in message


@pytest.mark.parametrize('invalid', ['missing', 'fit_from_bench', 'fit_from_pilot', 0, -1, True, 1.5, '2'])
def test_missing_sentinel_and_invalid_image_attempts_are_refused(production_env, invalid):
    env = production_env
    probe = env.config['vlm']['backends']['main']['probe']
    if invalid == 'missing':
        del probe['image_attempts']
    else:
        probe['image_attempts'] = invalid
    path = env.root / 'probe-attempts-refusal.yaml'
    path.write_text(yaml.safe_dump(env.config), encoding='utf-8')
    with pytest.raises(ConfigurationError, match=r'vlm\.backends\.main\.probe\.image_attempts'):
        load_config(path, 'kb sync', project_root=env.root)


def test_a_non_default_attempt_count_is_read_from_the_profile(production_env):
    env = production_env
    server = FencedImageServer(fail_first=9)
    run_probe(env, load(env, attempts=5), server)
    assert len(server.image_bodies) == 5 * challenge_count(env)
