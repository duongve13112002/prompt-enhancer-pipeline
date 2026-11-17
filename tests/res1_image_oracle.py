"""Test-only frozen pre-extension image-preparation oracle (RES-1, Pillow 11.3.0).

The transformation body is copied before the cooperative stop edit. Unchanged policy/metadata helpers are shared;
no callback or concurrent path enters this oracle. Keep it independent of the edited transformation body.
"""
from io import BytesIO

from PIL import Image, ImageOps

from prompt_enhancer.model_image import ImageRejected, _check_settings, _decoding_file_bytes, _stored_size


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
