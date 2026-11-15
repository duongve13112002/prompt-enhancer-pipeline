from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from hypothesis import given, strategies as st
from PIL import Image
from PIL.PngImagePlugin import PngInfo
import pytest

from prompt_enhancer.model_image import configure_decode_backstop, prepare_image as _prepare_image, prepare_vision_image


def prepare_image(*args, **kwargs):
    kwargs.setdefault('alpha_policy', {'mode': 'refuse'})  # Explicit test fixture policy.
    return _prepare_image(*args, **kwargs)


@given(st.integers(min_value=1, max_value=100), st.integers(min_value=1, max_value=100),
       st.integers(min_value=1, max_value=100))
def test_resize_dimensions(width, height, limit):
    source = BytesIO()
    Image.new('RGB', (width, height)).save(source, format='PNG')
    raw = source.getvalue()
    if max(width, height) <= limit:
        assert prepare_image(raw, limit, 10000) == raw
    result = prepare_image(raw, limit, 10000)
    with Image.open(BytesIO(result)) as image:
        scale = max(width, height)
        assert image.size == ((max(1, width * limit // scale), max(1, height * limit // scale))
                              if scale > limit else (width, height))
    assert prepare_image(raw, 'none', 10000) == raw


def test_decode_limit_and_bad_limits():
    source = BytesIO()
    Image.new('RGB', (10, 10)).save(source, format='PNG')
    with pytest.raises(ValueError):
        prepare_image(source.getvalue(), 'none', 99)
    with pytest.raises(ValueError):
        prepare_image(source.getvalue(), 0, 100)


def test_unchanged_image_preserves_original_metadata_bytes():
    source = BytesIO()
    metadata = PngInfo()
    metadata.add_text('fixture', 'preserve metadata')
    Image.new('RGB', (4, 4)).save(source, format='PNG', pnginfo=metadata)
    assert prepare_image(source.getvalue(), 'none', 16) == source.getvalue()


def test_exif_orientation_applied():
    source = BytesIO()
    image = Image.new('RGB', (8, 4))
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format='JPEG', exif=exif)
    result = prepare_image(source.getvalue(), 'none', 100)
    with Image.open(BytesIO(result)) as oriented:
        assert oriented.size == (4, 8)
        assert oriented.getexif().get(274, 1) == 1


def test_configured_decode_bound_leaves_pillow_limit_unchanged(monkeypatch):
    source = BytesIO()
    Image.new('RGB', (10, 10)).save(source, format='PNG')
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1000)
    assert prepare_image(source.getvalue(), 'none', 100) == source.getvalue()
    assert Image.MAX_IMAGE_PIXELS == 1000
    with pytest.raises(ValueError, match='max_decode_pixels'):
        prepare_image(source.getvalue(), 'none', 99)
    assert Image.MAX_IMAGE_PIXELS == 1000


def test_concurrent_images_use_independent_limits(monkeypatch):
    source = BytesIO()
    Image.new('RGB', (10, 10)).save(source, format='PNG')
    raw = source.getvalue()
    opened = Barrier(2, timeout=2)
    original = Image.open

    def synchronized_open(*args, **kwargs):
        image = original(*args, **kwargs)
        opened.wait()
        return image

    monkeypatch.setattr(Image, 'open', synchronized_open)
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        low = pool.submit(prepare_image, raw, 'none', 99)
        high = pool.submit(prepare_image, raw, 'none', 100)
        with pytest.raises(ValueError, match='max_decode_pixels'):
            low.result(timeout=3)
        assert high.result(timeout=3) == raw
    assert Image.MAX_IMAGE_PIXELS == 1000


def test_process_decode_backstop_only_increases(monkeypatch):
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 100)
    configure_decode_backstop([150, 200])
    assert Image.MAX_IMAGE_PIXELS == 200
    configure_decode_backstop([50])
    assert Image.MAX_IMAGE_PIXELS == 200
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', None)
    configure_decode_backstop([300])
    assert Image.MAX_IMAGE_PIXELS == 300


def test_shared_vision_quality_changes_encoded_image():
    source = BytesIO()
    Image.effect_noise((32, 32), 80).convert('RGB').save(source, format='PNG')
    settings = {'max_image_side': 'none', 'format': 'jpeg', 'quality': 10, 'alpha_policy': {'mode': 'refuse'}}
    low = prepare_vision_image(source.getvalue(), settings, 2048)
    settings['quality'] = 90
    high = prepare_vision_image(source.getvalue(), settings, 2048)
    assert low != high
    settings['quality'] = 'design_required'
    with pytest.raises(ValueError, match='vision.quality'):
        prepare_vision_image(source.getvalue(), settings, 2048)


@pytest.mark.parametrize('mode', ['1', 'L', 'LA', 'P', 'P-transparent', 'PA', 'RGB', 'RGBA',
                                  'CMYK', 'I;16', 'I;16B', 'I;16L', 'I', 'F', 'LAB', 'YCbCr'])
@pytest.mark.parametrize('target', ['PNG', 'JPEG', 'WEBP'])
@pytest.mark.parametrize('limit', ['none', 4])
@pytest.mark.parametrize('policy', [{'mode': 'refuse'}, {'mode': 'flatten', 'background': '#FFFFFF'}])
def test_image_mode_and_format(mode, target, limit, policy):
    source = BytesIO()
    source_mode = 'P' if mode == 'P-transparent' else mode
    image = Image.new(source_mode, (8, 6))
    if mode == 'P-transparent':
        image.info['transparency'] = 0
    source_format = ('IM' if mode in ('I;16L', 'YCbCr') else
                     'TIFF' if mode in ('CMYK', 'I', 'I;16B', 'F', 'PA', 'LAB') else 'PNG')
    image.save(source, format=source_format)
    with Image.open(BytesIO(source.getvalue())) as decoded:
        assert decoded.mode == source_mode
        if mode == 'P-transparent':
            assert 'transparency' in decoded.info
    transparent = mode in ('LA', 'P-transparent', 'PA', 'RGBA')
    incompatible = (mode == 'P' and target == 'JPEG' or mode in ('CMYK', 'YCbCr') and target == 'PNG'
                    or mode in ('I', 'I;16', 'I;16B') and target == 'JPEG'
                    or mode in ('F', 'LAB', 'I;16L') and target in ('PNG', 'JPEG'))
    if transparent and policy['mode'] == 'refuse':
        with pytest.raises(ValueError, match='alpha_policy refuses transparent image'):
            prepare_image(source.getvalue(), limit, 100, alpha_policy=policy, output_format=target)
    elif incompatible:
        with pytest.raises(ValueError, match=f'image mode {source_mode} cannot be encoded as {target}'):
            prepare_image(source.getvalue(), limit, 100, alpha_policy=policy, output_format=target)
    else:
        result = prepare_image(source.getvalue(), limit, 100, alpha_policy=policy, output_format=target)
        with Image.open(BytesIO(result)) as image:
            assert image.size == ((8, 6) if limit == 'none' else (4, 3))
            assert image.format == target
            if transparent:
                assert image.convert('RGB').getpixel((0, 0)) == (255, 255, 255)


def test_alpha_background_override_and_refusal():
    source = BytesIO()
    Image.new('RGBA', (2, 2), (0, 0, 0, 0)).save(source, 'PNG')
    output = prepare_image(source.getvalue(), 'none', 4, output_format='PNG',
                           alpha_policy={'mode': 'flatten', 'background': '#123456'})
    with Image.open(BytesIO(output)) as image:
        assert image.getpixel((0, 0)) == (18, 52, 86)
    partial = BytesIO()
    Image.new('RGBA', (2, 2), (0, 0, 0, 128)).save(partial, 'PNG')
    output = prepare_image(partial.getvalue(), 'none', 4, output_format='PNG',
                           alpha_policy={'mode': 'flatten', 'background': '#123456'})
    with Image.open(BytesIO(output)) as image:
        assert image.getpixel((0, 0)) == (9, 26, 43)
    for policy in ({}, {'mode': 'flatten'}, {'mode': 'flatten', 'background': 'white'}, {'mode': 'refuse', 'background': '#FFFFFF'}):
        with pytest.raises(ValueError, match='alpha_policy'):
            prepare_image(source.getvalue(), 'none', 4, alpha_policy=policy, output_format='PNG')


@given(st.integers(min_value=1, max_value=100), st.integers(min_value=1, max_value=100),
       st.integers(min_value=1, max_value=100))
def test_alpha_jpeg_refusal_at_any_size(width, height, limit):
    source = BytesIO()
    Image.new('RGBA', (width, height)).save(source, format='PNG')
    with pytest.raises(ValueError, match='alpha_policy refuses transparent image'):
        prepare_image(source.getvalue(), limit, 10000, output_format='JPEG')
