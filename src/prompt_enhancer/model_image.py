"""Shared image preparation for model clients and image embedders."""

from contextlib import contextmanager
from io import BytesIO
from threading import Lock

from PIL import Image, ImageOps, TiffImagePlugin

from .config import SENTINELS, installed_encoder

_BACKSTOP_LOCK = Lock()


def configure_decode_backstop(limits):
    """Raise Pillow's process-wide backstop when configured limits require it."""
    limits = tuple(limits)
    if not limits:
        return
    with _BACKSTOP_LOCK:
        current = Image.MAX_IMAGE_PIXELS
        if current is None or max(limits) > current:
            Image.MAX_IMAGE_PIXELS = max(limits)


class ImageRejected(ValueError):
    """A decodable image the configured policy refuses; `status` names the reason, `info` the decoded metadata."""

    def __init__(self, message, status, info):
        super().__init__(message)
        self.status = status
        self.info = info


class UndecodableImage(OSError):
    """The file's bytes cannot be decoded; the decoder's own exception is the `__cause__`."""


@contextmanager
def _decoding_file_bytes():
    """The one decode boundary: whatever the decoder raises on the file's bytes means the file is undecodable.

    Pillow's openers and decoders are not a closed set and raise many types on bad bytes (T-008 fix round 3, audit 1:
    OSError, ValueError, SyntaxError, IndexError, RuntimeError, ZeroDivisionError, NotImplementedError), so the
    boundary is structural rather than a type list: only calls that read the bytes run inside it, never a setting, a
    policy or the encoder. Pillow's decompression-bomb refusal keeps its own status; memory exhaustion is not a file
    fault; interrupts are BaseException and are never caught.
    """
    try:
        yield
    except (Image.DecompressionBombError, MemoryError):
        raise
    except Exception as error:
        raise UndecodableImage(f'the image file cannot be decoded ({type(error).__name__})') from error


def _vision_args(vision):
    for field in ('max_image_side', 'format', 'quality'):
        value = vision[field]
        if isinstance(value, str) and value in SENTINELS:
            raise ValueError(f'vision.{field}: unresolved sentinel {value}')
    return vision['max_image_side'], {'alpha_policy': vision['alpha_policy'], 'output_format': vision['format'],
                                      'quality': vision['quality']}


def prepare_vision_image(data, vision, max_decode_pixels):
    """Apply shared vision settings for a caller without a role override."""
    max_side, kwargs = _vision_args(vision)
    return prepare_image(data, max_side, max_decode_pixels, **kwargs)


def check_vision_settings(vision, max_decode_pixels):
    """Refuse invalid shared settings once, before any image, so a setting error is never mistaken for a bad file.

    K1 checks the numeric domains against the configuration schema first (config.validate_embedder_reads)."""
    max_side, kwargs = _vision_args(vision)
    _check_settings(max_side, max_decode_pixels, kwargs['alpha_policy'], kwargs['output_format'])


def prepare_vision_image_with_info(data, vision, max_decode_pixels):
    """Shared vision settings, one decode: the prepared bytes and the stored file's metadata."""
    max_side, kwargs = _vision_args(vision)
    return prepare_image_with_info(data, max_side, max_decode_pixels, **kwargs)


def prepare_image(data, max_side, max_decode_pixels, *, alpha_policy, output_format=None, quality=None):
    """Apply the alpha policy, orient and downscale, preserving unchanged bytes.

    An alpha channel or palette transparency counts even when every pixel is opaque.
    Flatten composites onto the configured RGB color before resize and encoding.
    """
    return prepare_image_with_info(data, max_side, max_decode_pixels, alpha_policy=alpha_policy,
                                   output_format=output_format, quality=quality)[0]


def _check_settings(max_side, max_decode_pixels, alpha_policy, output_format=None):
    # None keeps the source format (a caller that configures none); a configured name must be an installed
    # encoder, so an empty or unknown value is a setting error, never a silent fallback (T-008 fix round 4, R3F1).
    if output_format is not None and not installed_encoder(output_format):
        raise ValueError(f'output_format: no installed encoder for {output_format!r}')
    if max_side != 'none' and (type(max_side) is not int or max_side < 1):
        raise ValueError('max_side must be none or a positive integer')
    if type(max_decode_pixels) is not int or max_decode_pixels < 1:
        raise ValueError('max_decode_pixels must be a positive integer')
    if not isinstance(alpha_policy, dict) or alpha_policy.get('mode') not in ('refuse', 'flatten'):
        raise ValueError('alpha_policy must specify refuse or flatten')
    if alpha_policy['mode'] == 'flatten':
        color = alpha_policy.get('background')
        if set(alpha_policy) != {'mode', 'background'} or not isinstance(color, str) or len(color) != 7 or color[0] != '#' or any(c not in '0123456789abcdefABCDEF' for c in color[1:]):
            raise ValueError('alpha_policy.background must be #RRGGBB')
    elif set(alpha_policy) != {'mode'}:
        raise ValueError('alpha_policy refuse accepts only mode')


def prepare_image_with_info(data, max_side, max_decode_pixels, *, alpha_policy, output_format=None, quality=None):
    """`prepare_image` from a single decode, also returning the stored file's format and pixel size.

    The size is the file's own, before any orientation (T-008 Q4). A policy refusal raises ImageRejected; a file
    whose bytes cannot be decoded raises UndecodableImage (an OSError); Pillow's decompression-bomb refusal raises
    Image.DecompressionBombError. Settings and encoding errors propagate unchanged.
    """
    _check_settings(max_side, max_decode_pixels, alpha_policy, output_format)
    stream = BytesIO(data)
    # Each `with _decoding_file_bytes()` block holds only calls that read the file's bytes (T-008 fix round 3).
    with _decoding_file_bytes():
        source = Image.open(stream)
    try:
        with _decoding_file_bytes():
            width, height = _stored_size(source)
            original_format = source.format
        info = {'format': original_format, 'width': width, 'height': height}
        if width * height > max_decode_pixels:
            raise ImageRejected('image exceeds max_decode_pixels', 'exceeds_max_decode_pixels', info)
        with _decoding_file_bytes():
            orientation = source.getexif().get(274, 1)
            source.load()
            image = ImageOps.exif_transpose(source)  # a new image, so closing the file below keeps it
            transparent = image.getbands()[-1] in ('A', 'a') or 'transparency' in image.info
            # The file's own transparency, converted whatever the policy, so no setting enters this block.
            rgba = image.convert('RGBA') if transparent else None
    finally:
        source.close()
    if transparent:
        if alpha_policy['mode'] == 'refuse':
            raise ImageRejected('alpha_policy refuses transparent image', 'alpha_refused', info)
        color = alpha_policy['background']
        rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        image = Image.alpha_composite(Image.new('RGBA', rgba.size, (*rgb, 255)), rgba).convert('RGB')
    width, height = image.size
    limit = max(width, height)
    resized = max_side != 'none' and limit > max_side
    if resized:
        dimensions = (max(1, width * max_side // limit), max(1, height * max_side // limit))
        image = image.resize(dimensions, Image.Resampling.LANCZOS)
    if not resized and orientation == 1 and output_format is None and not transparent:
        return data, info
    fmt = original_format if output_format is None else output_format
    if not fmt:  # only a source whose opener reports no format: a configured name is checked above
        raise ImageRejected('image format is unavailable', 'encode_failed', info)
    kwargs = {} if quality is None else {'quality': quality}
    result = BytesIO()
    try:
        image.save(result, format=fmt, **kwargs)
    except OSError as error:
        raise ImageRejected(f'image mode {image.mode} cannot be encoded as {fmt}', 'encode_failed', info) from error
    return result.getvalue(), info


def _stored_size(image):
    """The file's own pixel grid. Pillow's TIFF opener already swaps `size` for orientations 5 to 8
    (TiffImagePlugin.py:1463-1467), so a TIFF reads its ImageWidth and ImageLength tags, which that opener has
    checked are integers; every other installed opener reports the stored grid (T-008 fix round 2, measured)."""
    if isinstance(image, TiffImagePlugin.TiffImageFile):
        return image.tag_v2[256], image.tag_v2[257]
    return image.width, image.height
