"""MS-3 test harness: tiny synthetic source images, an S0 environment for a chosen source cap, and the production
Pipeline run against the labelled loopback model double (no real model, no network beyond 127.0.0.1)."""

import asyncio
import base64
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from threading import Thread

from PIL import Image

from intake_fixtures import activate, configuration
from prompt_enhancer.pipeline import Pipeline
from prompt_enhancer.profiles import check_profile, confirm_profile, register_profile, resolve_profile
from prompt_enhancer.storage import open_data_store
from profile_fixtures import (
    ROOT, SECTIONED, DrafterDouble, RunnerDouble, bundle, install_prompts, set_manifest, write_golden,
)
from test_ms2_manifest_v1 import state_renderer, v1_draft
from test_pipeline_config import runtime
from test_pipeline_s0_s6 import FullServerDouble


def png_b64(color, width=8, height=4):
    """A distinct tiny image per colour: two colours never share bytes (duplicate rule)."""
    stream = BytesIO()
    Image.new('RGB', (width, height), color).save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


COLORS = ('red', 'green', 'blue', 'yellow', 'white', 'black', 'orange', 'purple')


def images(count):
    return [png_b64(COLORS[index]) for index in range(count)]


def v1_renderer(cap):
    return {'accepts_reference_images': False, 'max_source_images': cap,
            'source_image_syntax': 'none' if cap == 1 else '<source_{n}>', 'reference_image_syntax': 'none'}


def configure_cap(config, cap):
    """Test-only: the configured source cap and the per-call image limit that must not be below it (MS-2)."""
    config['edit']['max_source_images'] = cap
    config['vlm']['backends']['main']['image']['max_images_per_call'] = max(cap, 1)
    return config


def activate_manifest_v2(config, project_root, manifest_cap):
    """Register, confirm and check the synthetic profile with a `profile_manifest.v1` manifest (schema_version 2) whose
    `renderer.max_source_images` is `manifest_cap`: the drafter double leaves the renderer nulls and the admin fills them
    (the MS-2 flow, real registration, confirm and check code). Returns the bundle folder."""
    config['vlm']['roles']['manifest_drafter']['output']['schema_id'] = 'profile_manifest.v1'
    install_prompts(project_root)
    folder = bundle(Path(config['profiles']['root']).parent, 'p1', SECTIONED)
    result = asyncio.run(register_profile(config, 'p1', DrafterDouble(v1_draft()), project_root=project_root))
    state_renderer(folder, v1_renderer(manifest_cap))
    ids = [conflict['id'] for conflict in result['manifest']['conflicts']]
    confirm_profile(config, 'p1', {cid: 'profile_wins' for cid in ids}, project_root=project_root)
    for cid in ids:
        write_golden(folder, f'g_{cid}', [cid])
    set_manifest(folder, golden_tests=[f'g_{cid}' for cid in ids] or ['g0'])
    if not ids:
        write_golden(folder, 'g0', [])
    asyncio.run(check_profile(config, 'p1', RunnerDouble(), project_root=project_root))
    return folder


def copy_committed(source, target):
    """Test-only: copy a tree of text files with the committed LF line ends. A Windows checkout with `core.autocrlf`
    holds them with CRLF, and prompt and schema bytes are hashed and sent as they are, so a byte baseline taken from the
    checkout would depend on the OS (CI 45: the Windows-captured baseline failed on Ubuntu)."""
    for path in sorted(Path(source).rglob('*')):
        if path.is_file():
            destination = Path(target) / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = path.read_bytes()
            # a file with a NUL byte is binary (every PNG has one in its IHDR length) and is copied unchanged
            destination.write_bytes(data if b'\x00' in data else data.replace(b'\r\n', b'\n'))


def install_committed_prompts(project_root):
    """Test-only `install_prompts` with the committed prompt bytes (see `copy_committed`)."""
    copy_committed(ROOT / 'prompts', Path(project_root) / 'prompts')


def committed_root(target):
    """Test-only: `target` (the test's `tmp_path`, which holds its configured paths) made a project root with the
    committed contracts and prompts. The caller also replaces `intake_fixtures.install_prompts` by
    `install_committed_prompts`, because `activate` reinstalls the prompts."""
    for name in ('contracts/schemas', 'contracts/probes'):
        copy_committed(ROOT / name, Path(target) / name)
    install_committed_prompts(target)
    return target


def run_pipeline(model_test_profile, tmp_path, project_root, request, *, cap=1, manifest_cap=None, mutate=None):
    """Production S0 to S6 on a retrieval-off profile; returns the envelope, the model calls of the planner and the
    writer (probes dropped), the stored trace body and the number of model-server requests."""
    seen, answers = [], []
    handler = type('Handler', (FullServerDouble,), {'seen': seen, 'answers': answers})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = configure_cap(configuration(model_test_profile, tmp_path), cap)
    config['runtime'].update(runtime())
    config['storage']['data_root'] = str(tmp_path / 'data')
    config['resolution']['random_seed'] = 7
    backend = config['vlm']['backends']['main']
    backend.update(base_urls=[f'http://127.0.0.1:{server.server_port}'],
                   probe={'max_requests': 12, 'timeout_s': 2, 'image_attempts': 1})
    backend['image']['max_decode_pixels'] = 20_000_000
    if mutate is not None:
        mutate(config)
    if manifest_cap is None:
        activate(config, project_root)
    else:
        activate_manifest_v2(config, project_root, manifest_cap)
    handler.writer_system = resolve_profile(config, project_root=project_root).system_message.decode('utf-8')
    handler.verifier_system = (project_root / 'prompts/verifier_v0.txt').read_bytes().decode('utf-8')

    async def run():
        async with Pipeline(config, project_root=project_root) as pipeline:
            response = await pipeline.enhance(deepcopy(request))
            await response.after_delivery()
        return response.envelope

    try:
        envelope = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    rows = open_data_store(config, 'enhance once', project_root=project_root).read_rows('request_traces')
    assert len(rows) == 1
    calls = [body for body in seen if handler.is_planner(body) or body['messages'][0]['content']
             in (handler.writer_system, handler.verifier_system)]
    return {'envelope': envelope, 'calls': calls, 'body': rows[0]['body'], 'seen': len(seen), 'row': rows[0]}


def differences(left, right, path='$'):
    """The paths at which two JSON values differ (a readable failure message for the byte-identity tests)."""
    if isinstance(left, dict) and isinstance(right, dict):
        return [item for key in sorted(set(left) | set(right))
                for item in differences(left.get(key), right.get(key), f'{path}.{key}')]
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [item for index, (a, b) in enumerate(zip(left, right)) for item in differences(a, b, f'{path}[{index}]')]
    return [] if left == right else [path]


# Per-run values of a call record: timing, and hashes or records that carry the loopback port of the double.
MASKED_KEYS = frozenset(('latency_s', 'context_hash', 'record_json'))


# A capability probe re-encodes its bundle images with Pillow, whose bundled zlib differs between the Windows and the
# Linux wheels, so the prepared image bytes and the bodies that carry them are platform outputs (CI 45). They are masked
# only in probe evidence that holds images; the request's own source image is compared in full.
IMAGE_PROBE_KEYS = frozenset(('body_hashes', 'prepared_image_hashes'))


def masked(value):
    if isinstance(value, dict):
        if value.get('image_hashes') and 'capability' in value:
            value = {key: '<platform>' if key in IMAGE_PROBE_KEYS else item for key, item in value.items()}
        return {key: '<masked>' if key in MASKED_KEYS else masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


def normalized(result):
    """The parts that must stay byte-identical for one image: model call bodies, envelope (trace id masked) and the
    stored stage bodies that the request shapes (not timings)."""
    envelope = dict(result['envelope'], trace_id='<trace-id>')
    body = result['body']
    stages = {key: body.get(key) for key in ('plan', 'writer', 'brief', 'shortlist', 'assembly', 'intake_error')}
    return masked(json.loads(json.dumps({'envelope': envelope, 'calls': result['calls'], 'stages': stages},
                                        sort_keys=True, default=str)))
