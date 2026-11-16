"""The embedding server's wire format: parse a K1 `/v1/embeddings` request, build the response (T-018).

Pure Python plus Pillow, no torch. The accepted request is exactly what `k1_embed.Embedder` sends
(`image_bodies`, `text_bodies` with `shape: openai_input_content`): `{model, encoding_format: "float", input: [...]}`
where an item is a text string or `{content: [optional text part, image_url part]}` with a `data:` URI. Everything else
is refused with a stable error code, so a client bug never becomes a guess about what was meant.
"""

import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
import json

from PIL import Image

ENCODING_FORMAT = 'float'  # protocol-bound: the encoding_format K1 sends (k1_embed.ENCODING_FORMAT)
REQUEST_FIELDS = ('model', 'encoding_format', 'input')  # protocol-bound: the only top-level fields K1 sends
DATA_PREFIX = 'data:'  # safety-bound: images arrive as data URIs only; the server never fetches a URL
DATA_SUFFIX = ';base64'  # protocol-bound: how K1 states the encoding (`data:<media>;base64,<payload>`)
# safety-bound: the Pillow decoders the server runs, all raster and in-process. Left out on purpose: EPS (Pillow
# starts Ghostscript with temporary files, PIL/EpsImagePlugin.py) and every format that registers a handler for
# an external library or interpreter. A file-format rule, not a content rule; the audit is in docs/stages/K1-embed.md.
ALLOWED_FORMATS = ('PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'TIFF')


class RequestError(Exception):
    """A request the server refuses; `status` is the HTTP status, `code` a stable reason."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class EngineError(RuntimeError):
    """The model failed on a valid request (out of memory, a non-finite result, a wrong count)."""


@dataclass(frozen=True)
class Limits:
    model_id: str
    max_items: int
    max_image_bytes: int
    max_decode_pixels: int
    accepts_instruction: bool


@dataclass(frozen=True)
class EmbedItem:
    """One input: text, or an image with the optional instruction that preceded it."""

    text: str | None = None
    image: Image.Image | None = None
    instruction: str | None = None


def _bad(code, message):
    return RequestError(400, code, message)


def _decode_image(url, limits):
    if not isinstance(url, str):
        raise _bad('invalid_image', 'image_url.url is not a string')
    header, comma, payload = url.partition(',')
    if not comma or not header.startswith(DATA_PREFIX) or not header.endswith(DATA_SUFFIX):
        raise _bad('unsupported_image_url', 'only data:<image media type>;base64,<payload> URIs are accepted')
    media = header[len(DATA_PREFIX):-len(DATA_SUFFIX)]
    if not media.startswith('image/') or len(media) == len('image/') or ';' in media or ',' in media:
        raise _bad('unsupported_media_type', 'the data URI must state an image/* media type')
    if len(payload) > -(-limits.max_image_bytes // 3) * 4:  # refuse before decoding what could not fit
        raise _bad('image_too_large', 'the image exceeds the configured byte limit')
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _bad('invalid_image', 'the image payload is not valid base64') from error
    if not data or len(data) > limits.max_image_bytes:
        raise _bad('image_too_large' if data else 'invalid_image', 'the image is empty or exceeds the byte limit')
    try:  # the one decode boundary: anything the decoder raises on these bytes means they are not an image
        # `formats=` limits Pillow's detection to in-process raster decoders, so a payload that merely looks like
        # another format (PostScript, which Pillow renders by starting Ghostscript) is never dispatched.
        image = Image.open(BytesIO(data), formats=ALLOWED_FORMATS)
        if Image.MIME.get(image.format) != media:
            raise _bad('unsupported_media_type', 'the bytes are not the image type the data URI states')
        if image.width * image.height > limits.max_decode_pixels:
            raise _bad('image_too_large', 'the image exceeds the configured pixel limit')
        image.load()
        return image.convert('RGB')
    except RequestError:
        raise
    except MemoryError:
        raise
    except Exception as error:
        raise _bad('undecodable_image', f'the image cannot be decoded ({type(error).__name__})') from error


def _parse_content(content, limits):
    if not isinstance(content, list) or not content:
        raise _bad('invalid_input', 'content must be a non-empty list of parts')
    instruction = image = None
    for part in content:
        kind = part.get('type') if isinstance(part, dict) else None
        if kind == 'text' and set(part) == {'type', 'text'} and image is None and instruction is None:
            instruction = part['text']
            if not isinstance(instruction, str) or not instruction:
                raise _bad('invalid_input', 'a text part must hold a non-empty string')
            if not limits.accepts_instruction:
                raise _bad('instruction_unsupported', 'this model has no system instruction')
        elif (kind == 'image_url' and set(part) == {'type', 'image_url'} and image is None
              and isinstance(part['image_url'], dict) and set(part['image_url']) == {'url'}):
            image = _decode_image(part['image_url']['url'], limits)
        else:
            raise _bad('invalid_input', 'content is one optional text part followed by one image_url part')
    if image is None:
        raise _bad('invalid_input', 'content holds no image')
    return EmbedItem(image=image, instruction=instruction)


def parse_request(body, limits):
    """The ordered `EmbedItem`s of a request body (bytes), or a RequestError."""
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise _bad('invalid_json', 'the body is not valid JSON') from error
    if not isinstance(payload, dict):
        raise _bad('invalid_request', 'the body must be a JSON object')
    extra = sorted(set(payload) - set(REQUEST_FIELDS))
    if extra:
        raise _bad('unsupported_field', f'unsupported field {extra[0]!r}')
    if payload.get('model') != limits.model_id:
        raise _bad('unknown_model', 'model is not the one this server loaded')
    if payload.get('encoding_format', ENCODING_FORMAT) != ENCODING_FORMAT:
        raise _bad('unsupported_encoding', f'only encoding_format {ENCODING_FORMAT!r} is supported')
    inputs = payload.get('input')
    if not isinstance(inputs, list) or not inputs:
        raise _bad('invalid_input', 'input must be a non-empty list')
    if len(inputs) > limits.max_items:
        raise _bad('too_many_inputs', f'input holds more than {limits.max_items} items')
    items = []
    for value in inputs:
        if isinstance(value, str):
            if not value:
                raise _bad('invalid_input', 'a text input must not be empty')
            items.append(EmbedItem(text=value))
        elif isinstance(value, dict) and set(value) == {'content'}:
            items.append(_parse_content(value['content'], limits))
        else:
            raise _bad('invalid_input', 'an input is a string or an object with a content list')
    return items


def build_response(model_id, vectors, prompt_tokens):
    """The OpenAI-style response body (bytes): vectors in input order, `index` = input position."""
    try:
        return json.dumps({'object': 'list', 'model': model_id,
                           'data': [{'object': 'embedding', 'index': index, 'embedding': list(vector)}
                                    for index, vector in enumerate(vectors)],
                           'usage': {'prompt_tokens': prompt_tokens, 'total_tokens': prompt_tokens}},
                          allow_nan=False, separators=(',', ':')).encode('utf-8')
    except ValueError as error:
        raise EngineError('the model produced a non-finite value') from error


def error_body(code, message):
    return json.dumps({'error': {'code': code, 'message': message}}, separators=(',', ':')).encode('utf-8')
