"""RES-1 cooperative decode stop and absent-callback equivalence; labelled CPU fixtures only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading

from hypothesis import given, settings, strategies as st
from PIL import Image, ImageFile, ImageOps
import pytest

from k1_fixtures import EmbeddingServer, image_bytes, k0_handoff, k1_profile, make_source
from res1_image_oracle import prepare_image_with_info as original_preparation
from storage_fixtures import make_store
from prompt_enhancer import k1_embed, model_image
from prompt_enhancer.storage.store import Checkpoints


@pytest.mark.parametrize('boundary', ['load', 'encode'])
def test_k1_stop_before_native_boundary_creates_no_result(project_root, tmp_path, model_test_profile, monkeypatch, boundary):
    source = make_source(tmp_path / 'source', {'image.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'store')
    handoff = k0_handoff(store, source)
    config = k1_profile(model_test_profile)
    config['runtime']['cpu_workers'] = 1
    entered, release = threading.Event(), threading.Event()
    stop_events, loads, encodes, checkpoint_events = [], [], [], []
    original_size, original_transpose = model_image._stored_size, ImageOps.exif_transpose
    original_load, original_save = ImageFile.ImageFile.load, Image.Image.save
    original_commit, original_fail = Checkpoints.commit, Checkpoints.fail

    def event_factory():
        event = threading.Event()
        stop_events.append(event)
        return event

    def held(value):
        entered.set()
        assert release.wait(5), 'labelled decode-boundary hold timed out'
        return value

    def size(image):
        return held(original_size(image)) if boundary == 'load' else original_size(image)

    def transpose(*args, **kwargs):
        value = original_transpose(*args, **kwargs)
        return held(value) if boundary == 'encode' else value

    def load(image, *args, **kwargs):
        loads.append(True)
        return original_load(image, *args, **kwargs)

    def save(image, *args, **kwargs):
        encodes.append(True)
        return original_save(image, *args, **kwargs)

    def committed(self, key, kind, data, **kwargs):
        if self.stage == k1_embed.IMAGE_STAGE:
            checkpoint_events.append(('commit', json.loads(data)))
        return original_commit(self, key, kind, data, **kwargs)

    def failed(self, *args, **kwargs):
        if self.stage == k1_embed.IMAGE_STAGE:
            checkpoint_events.append(('fail', args))
        return original_fail(self, *args, **kwargs)

    monkeypatch.setattr(k1_embed, 'Event', event_factory)
    monkeypatch.setattr(model_image, '_stored_size', size)
    monkeypatch.setattr(ImageOps, 'exif_transpose', transpose)
    monkeypatch.setattr(ImageFile.ImageFile, 'load', load)
    monkeypatch.setattr(Image.Image, 'save', save)
    monkeypatch.setattr(Checkpoints, 'commit', committed)
    monkeypatch.setattr(Checkpoints, 'fail', failed)
    server = EmbeddingServer()

    async def exercise():
        task = asyncio.create_task(k1_embed.embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                                         transport=server.transport()))
        try:
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                while not stop_events[0].is_set():
                    await asyncio.sleep(.001)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(exercise())
    assert encodes == [], 'a stopped worker encoded an image whose result was discarded'
    if boundary == 'load':
        assert loads == [], 'a stopped worker started pixel loading (including PNG EXIF loading)'
    else:
        assert loads, 'the labelled hold must occur after actual decoding'
    assert checkpoint_events == [] and server.requests == []
    with store.writer() as writer:
        assert not [row for row in writer._state_rows().values() if row['stage'] == k1_embed.IMAGE_STAGE]


def outcome(operation):
    try:
        return ('ok', operation())
    except (model_image.ImageRejected, model_image.UndecodableImage, Image.DecompressionBombError) as error:
        return (type(error).__name__, str(error), getattr(error, 'status', None), getattr(error, 'info', None))


@settings(max_examples=100, deadline=None)
@given(width=st.integers(1, 24), height=st.integers(1, 24), source_format=st.sampled_from(['PNG', 'JPEG', 'TIFF', 'WEBP']),
       alpha=st.booleans(), orientation=st.sampled_from([1, 2, 3, 5, 6, 8]),
       limit=st.one_of(st.just('none'), st.integers(1, 24)), target=st.sampled_from([None, 'PNG', 'JPEG', 'WEBP']),
       flatten=st.booleans(), quality=st.integers(1, 100), at_cap=st.booleans())
def test_absent_callback_matches_frozen_bytes_and_info(width, height, source_format, alpha, orientation,
                                                     limit, target, flatten, quality, at_cap):
    mode = 'RGBA' if alpha and source_format != 'JPEG' else 'RGB'
    image = Image.new(mode, (width, height))
    pixels = [(i * 17 % 256, i * 31 % 256, i * 47 % 256) for i in range(width * height)]
    image.putdata([(*pixel, 128 if i % 2 else 255) for i, pixel in enumerate(pixels)] if mode == 'RGBA' else pixels)
    raw = BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    image.save(raw, format=source_format, exif=exif)
    data = raw.getvalue()
    cap = width * height if at_cap else 24 * 24
    policy = {'mode': 'flatten', 'background': '#123456'} if flatten else {'mode': 'refuse'}
    kwargs = {'alpha_policy': policy, 'output_format': target, 'quality': quality}
    expected = outcome(lambda: original_preparation(data, limit, cap, **kwargs))
    assert outcome(lambda: model_image.prepare_image_with_info(data, limit, cap, **kwargs)) == expected
    vision = {'max_image_side': limit, 'format': target, 'quality': quality, 'alpha_policy': policy}
    assert outcome(lambda: model_image.prepare_vision_image_with_info(data, vision, cap)) == expected
    expected_bytes = ('ok', expected[1][0]) if expected[0] == 'ok' else expected
    assert outcome(lambda: model_image.prepare_image(data, limit, cap, **kwargs)) == expected_bytes
    assert outcome(lambda: model_image.prepare_vision_image(data, vision, cap)) == expected_bytes


def test_decode_stopped_remains_distinct_from_file_refusal():
    error = model_image.DecodeStopped('labelled cooperative stop')
    assert isinstance(error, Exception) and not isinstance(error, asyncio.CancelledError)
    with pytest.raises(model_image.DecodeStopped) as caught:
        with model_image._decoding_file_bytes():
            raise error
    assert caught.value is error


def test_k1_callback_uses_dedicated_exception_and_clears_test_context():
    stop = threading.Event()
    stop.set()
    k1_embed._DECODE_STOP.event = stop
    try:
        with pytest.raises(model_image.DecodeStopped):
            k1_embed._check_decode_stop()
    finally:
        del k1_embed._DECODE_STOP.event
    k1_embed._check_decode_stop()  # an ordinary caller has no worker-local stop context


def test_cooperative_stop_without_task_cancel_has_no_failure_or_checkpoint(project_root, tmp_path,
                                                                         model_test_profile, monkeypatch):
    source = make_source(tmp_path / 'source', {'image.png': image_bytes()})
    store = make_store(project_root, tmp_path / 'store')
    handoff = k0_handoff(store, source)
    config = k1_profile(model_test_profile)
    config['runtime']['cpu_workers'] = 1
    stops = []
    original_size = model_image._stored_size

    def event_factory():
        stop = threading.Event()
        stops.append(stop)
        return stop

    def size(image):
        stops[0].set()  # labelled cooperative signal at a native-operation boundary, without cancelling the task
        return original_size(image)

    monkeypatch.setattr(k1_embed, 'Event', event_factory)
    monkeypatch.setattr(model_image, '_stored_size', size)
    server = EmbeddingServer()
    result = asyncio.run(k1_embed.embed_images(store, config, handoff, knowledge_root=source.resolve(),
                                               transport=server.transport()))
    assert result['report']['failed'] == result['report']['decode_failed'] == {}
    assert result['report']['embedded'] == 0 and result['image_embeddings'].num_rows == 0
    assert result['images'] == handoff['images'] and server.requests == []
    with store.writer() as writer:
        assert not [row for row in writer._state_rows().values() if row['stage'] == k1_embed.IMAGE_STAGE]


def test_callback_can_stop_before_open_and_threads_keep_independent_callbacks(monkeypatch):
    data = image_bytes()
    expected = model_image.prepare_image_with_info(data, 'none', 100, alpha_policy={'mode': 'refuse'})

    def stop():
        raise model_image.DecodeStopped('labelled stop')

    original = Image.open
    opened = []

    def counted(*args, **kwargs):
        opened.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, 'open', counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopped = pool.submit(model_image.prepare_image_with_info, data, 'none', 100,
                              alpha_policy={'mode': 'refuse'}, check_stop=stop)
        normal = pool.submit(model_image.prepare_image_with_info, data, 'none', 100,
                             alpha_policy={'mode': 'refuse'})
        with pytest.raises(model_image.DecodeStopped):
            stopped.result(timeout=5)
        assert normal.result(timeout=5) == expected
    assert opened == [True]
