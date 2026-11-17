"""Shared fixtures of the embedding server tests (T-018): a profile with an `embed_server` backend, a labelled engine
double and a loopback server runner. Every value here is test-only and labelled; nothing loads a model."""

from contextlib import contextmanager
from copy import deepcopy
import socket
import threading
import time

import httpx

from prompt_enhancer.embed_server.pooling import l2_normalize
from prompt_enhancer.embed_server.protocol import EngineError
from prompt_enhancer.embed_server.models import EmbedResult
from prompt_enhancer.embed_server.server import serve

DIMENSION = 4  # test-only: the vector length of the profile and of the engine double


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def server_profile(model_test_profile, port=9100, family='embedding_gemma2'):
    """The conftest profile with its embedder turned into an `embed_server` backend (test-only values)."""
    profile = deepcopy(model_test_profile)
    backend = profile['vlm']['backends']['embedder']
    backend['engine'] = {'name': 'embed_server', 'version': 'test-version'}
    backend['base_urls'] = [f'http://127.0.0.1:{port}']
    qwen = family == 'qwen3_vl_embedding'
    backend['server'] = {  # attention: a kernel the family supports (EG2 eager only, families.attn_implementations)
        'family': family, 'dtype': 'bfloat16', 'attn_implementation': 'sdpa' if qwen else 'eager', 'forward_batch': 2,
        'max_length': 64,
        'image': ({'min_pixels': 4, 'max_pixels': 4096, 'max_soft_tokens': 'none'} if qwen else
                  {'min_pixels': 'none', 'max_pixels': 'none', 'max_soft_tokens': 16}),
        'default_instruction': 'Represent the input.' if qwen else 'none', 'idle_exit_s': 0,
        'max_request_bytes': 100000, 'max_image_bytes': 50000,
        'max_pending_requests': 8, 'max_connections': 16, 'max_discard_bytes': 400000, 'read_timeout_s': 5}
    profile['embedding']['request']['image_instruction'] = 'Represent the input.' if qwen else 'none'
    profile['runtime'].update(gpus=[0], vram_budget_gb=1)
    profile['vision']['format'] = 'png'  # test-only: the tests send PNG bytes, and the server checks bytes against the media type
    return profile


class EngineDouble:
    """Labelled test double of the embedding engine: content-derived unit vectors, no model, no torch."""

    native_dimension = DIMENSION

    def __init__(self, fail=None, delay=0.0):
        self.fail, self.delay, self.calls, self.closed = fail, delay, [], False

    def embed(self, items):
        self.calls.append(items)
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        vectors = []
        for item in items:
            if item.image is not None:
                seed = sum(item.image.getpixel((0, 0))) + item.image.width * 7 + item.image.height
            else:
                seed = sum(item.text.encode('utf-8'))
            vectors.append(l2_normalize([(seed + index) % 17 + 1 for index in range(DIMENSION)]))
        return EmbedResult(vectors, sum(1 for _ in items))

    def close(self):
        self.closed = True


class EngineFault(EngineError):
    pass


@contextmanager
def running_server(settings, engine=None, load=None, *, stop=None, clock=None, announce=None):
    """Run `serve` in a thread against the engine double; yield (base_url, engine, stop, result). `stop`, `clock`
    and `announce` reach `serve` unchanged when given (labelled test seams: a counting stop event, a fake clock, a
    recorder of the ready line)."""
    engine = engine or EngineDouble()
    stop, result = stop or threading.Event(), {}
    options = {} if clock is None else {'clock': clock}

    def run():
        try:
            result['reason'] = serve(settings, load or (lambda _: engine), stop=stop,
                                     announce=announce or (lambda _: None), **options)
        except BaseException as error:  # reported to the test, never swallowed
            result['error'] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    base = f'http://{settings.host}:{settings.port}'
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and 'error' not in result:
        try:
            if httpx.get(base + settings.health_path, timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    try:
        yield base, engine, stop, result
    finally:
        stop.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
