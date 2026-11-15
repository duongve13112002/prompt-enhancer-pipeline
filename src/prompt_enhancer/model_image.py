"""Shared image preparation for model clients and image embedders."""

from io import BytesIO
from threading import Lock

from PIL import Image, ImageOps

from .config import SENTINELS

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


def prepare_vision_image(data, vision, max_decode_pixels):
    """Apply shared vision settings for a caller without a role override."""
    for field in ('max_image_side', 'format', 'quality'):
        value = vision[field]
        if isinstance(value, str) and value in SENTINELS:
            raise ValueError(f'vision.{field}: unresolved sentinel {value}')
    return prepare_image(data, vision['max_image_side'], max_decode_pixels, alpha_policy=vision['alpha_policy'],
                         output_format=vision['format'], quality=vision['quality'])


def prepare_image(data, max_side, max_decode_pixels, *, alpha_policy, output_format=None, quality=None):
    """Apply the alpha policy, orient and downscale, preserving unchanged bytes.

    An alpha channel or palette transparency counts even when every pixel is opaque.
    Flatten composites onto the configured RGB color before resize and encoding.
    """
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
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > max_decode_pixels:
            raise ValueError('image exceeds max_decode_pixels')
        orientation = image.getexif().get(274, 1)
        original_format = image.format
        image.load()
        image = ImageOps.exif_transpose(image)
        transparent = image.getbands()[-1] in ('A', 'a') or 'transparency' in image.info
        if transparent:
            if alpha_policy['mode'] == 'refuse':
                raise ValueError('alpha_policy refuses transparent image')
            rgba = image.convert('RGBA')
            rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
            image = Image.alpha_composite(Image.new('RGBA', rgba.size, (*rgb, 255)), rgba).convert('RGB')
        width, height = image.size
        limit = max(width, height)
        resized = max_side != 'none' and limit > max_side
        if resized:
            dimensions = (max(1, width * max_side // limit), max(1, height * max_side // limit))
            image = image.resize(dimensions, Image.Resampling.LANCZOS)
        if not resized and orientation == 1 and output_format is None and not transparent:
            return data
        fmt = output_format or original_format
        if not fmt:
            raise ValueError('image format is unavailable')
        kwargs = {} if quality is None else {'quality': quality}
        result = BytesIO()
        try:
            image.save(result, format=fmt, **kwargs)
        except OSError as error:
            raise ValueError(f'image mode {image.mode} cannot be encoded as {fmt}') from error
        return result.getvalue()
