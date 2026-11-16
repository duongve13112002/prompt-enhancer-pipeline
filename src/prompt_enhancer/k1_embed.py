"""K1: embed knowledge-base images (part 1) and concept text (part 2) through the configured embedder (T-008).

The embedder is a named `vlm.backends` entry reached over HTTP (R-018). Every image is read read-only and verified
against its content hash, decoded once, prepared by the shared resize, embedded in batches and checkpointed one
artifact per input, so a rerun skips finished inputs and redoes the rest (R-017). K1 returns its tables; it never
publishes a snapshot (the caller does, as with K0).
"""

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct

import httpx
import PIL
from PIL import Image

from .config import (ConfigurationError, NO_MEDIA_TYPE, TEXT_PLACEHOLDER, _validate_embedding, image_media_type,
                     resolve_backend_engine, validate_embedder_reads)
from .k0_scan import read_source_file
from .model_image import (ImageRejected, UndecodableImage, check_vision_settings, configure_decode_backstop,
                          prepare_vision_image_with_info)
from .model_resources import BackendConcurrency
from .model_transport import ModelTransport, endpoint_url
from .storage import StorageError
from .storage.identity import artifact_key, canonical_json, is_concept_id, is_image_id
from .storage.tables import TEXT_KIND, check_embedding_space

LAYOUT_VERSION = 1  # artifact-bound: JSON layout of one K1 checkpoint artifact
# artifact-bound: the rule that derives the stored image metadata; it keys image artifacts only, so a rule change
# recomputes them instead of reusing old metadata. 2: a TIFF stores its own size tags (fix round 2, RT2).
IMAGE_METADATA_RULE = 2
IMAGE_STAGE = 'k1_image'  # artifact-bound: sync_state stage and artifact kind of part 1
TEXT_STAGE = 'k1_text'  # artifact-bound: sync_state stage and artifact kind of part 2
ENCODING_FORMAT = 'float'  # protocol-bound: embeddings `encoding_format` both engines document (Step 0, A1 and A2)
NO_INSTRUCTION = 'none'  # profile value of embedding.request.image_instruction that sends no instruction
DECODE_OK = 'ok'  # images.decode_status of a decoded image (contracts/README.md enums)
UNDECODABLE = 'undecodable'
EXCEEDS = 'exceeds_max_decode_pixels'
SOURCE_CHANGED = 'SOURCE_CHANGED'  # report code: no member path still holds the bytes of the image_id
REUSED_REFERENCED = 'REUSED_REFERENCED'  # report code: T-008 Q5


class EmbeddingError(RuntimeError):
    """A request or response failed; `code` is the stable reason recorded in sync_state."""

    def __init__(self, code, detail):
        super().__init__(f'{code}: {detail}')
        self.code = code


@dataclass(frozen=True)
class EmbedderSettings:
    backend_name: str
    backend: dict
    engine: dict
    shape: str
    image_instruction: str
    extra_body: dict
    text_template: str
    dimension: int
    dimension_mode: str
    batch_size: int
    vision: dict
    max_decode_pixels: int
    revision: str


@dataclass(frozen=True)
class ConceptText:
    """One concept text to embed (part 2); its inputs come from K3 and K4."""

    concept_id: str
    text_kind: str
    text: str


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def embedder_settings(config, path='configuration'):
    """Resolve K1's read set, refusing an unresolved or inconsistent value before any read or call (R-015)."""
    validate_embedder_reads(config, path, {})  # the same check the loader's kb sync runs (fix round 3, R2F2)
    _validate_embedding(config, path, {})
    embedding, vision = config['embedding'], config['vision']
    name = embedding['backend']
    backend = config['vlm']['backends'][name]
    if backend['provider'] != 'openai_compatible':
        raise ConfigurationError(f'{path}: embedding.backend: the embedder must be a local openai_compatible backend')
    engine = resolve_backend_engine(backend, f'vlm.backends.{name}')
    urls = backend['base_urls']
    try:  # the request URL, checked once here so an unusable endpoint never surfaces mid-stage
        endpoint_url(urls[0] if isinstance(urls, list) and urls else '', backend['endpoints']['embeddings'])
    except (ValueError, TypeError) as error:
        raise ConfigurationError(f'{path}: vlm.backends.{name}: base_urls[0] and endpoints.embeddings do not form '
                                 'a loopback or HTTPS URL with a relative route') from error
    max_decode_pixels = backend['image']['max_decode_pixels']
    try:
        check_vision_settings(vision, max_decode_pixels)
    except ValueError as error:
        raise ConfigurationError(f'{path}: vision: {error}') from error
    request = embedding['request']
    identity = {'engine': engine, 'model': {'id': backend['model']['id'], 'revision': backend['model']['revision']},
                'chosen': embedding['chosen'], 'revision': embedding['revision'], 'shape': request['shape'],
                'dimension': embedding['dimension'], 'dimension_mode': embedding['dimension_mode']}
    if engine['name'] == 'embed_server':
        # The project's own server turns these settings into vectors (T-018, review R2): they are part of the space.
        # Other engines keep exactly their earlier identity, so their checkpoints stay valid.
        from .embed_server.families import output_fingerprint

        identity['server'] = output_fingerprint(backend['server'])
    # Instructions and image settings stay out of the revision, so image and text rows share one embedding space
    # (tables.check_embedding_space); they enter the artifact keys instead.
    return EmbedderSettings(
        backend_name=name, backend=backend, engine=engine, shape=request['shape'],
        image_instruction=request['image_instruction'], extra_body=dict(request['extra_body']),
        text_template=request['text_template'], dimension=embedding['dimension'],
        dimension_mode=embedding['dimension_mode'], batch_size=embedding['batch_size'],
        vision={key: vision[key] for key in ('max_image_side', 'format', 'quality', 'alpha_policy', 'transport')},
        max_decode_pixels=max_decode_pixels, revision='emb-' + _digest(identity))


def image_revisions(settings):
    return {'model': settings.revision,
            'prompt': _digest({'shape': settings.shape, 'image_instruction': settings.image_instruction,
                               'extra_body': settings.extra_body}),
            'schema': _digest({'layout': LAYOUT_VERSION, 'metadata_rule': IMAGE_METADATA_RULE,
                               'vision': settings.vision, 'max_decode_pixels': settings.max_decode_pixels,
                               'pillow': PIL.__version__})}


def text_revisions(settings):
    return {'model': settings.revision,
            'prompt': _digest({'shape': settings.shape, 'text_template': settings.text_template,
                               'extra_body': settings.extra_body}),
            'schema': _digest({'layout': LAYOUT_VERSION})}


def image_key(settings, image_id):
    """Content hash plus embedder identity and every setting that shapes the request (R-017)."""
    return artifact_key(IMAGE_STAGE, image_id, image_revisions(settings))


def text_key(settings, text):
    return artifact_key(TEXT_STAGE, text_hash(text), text_revisions(settings))


def text_hash(text):
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def pack_vector(values):
    """float32 little-endian bytes: an exact, platform-independent round trip of the stored vector."""
    return struct.pack(f'<{len(values)}f', *values)


def unpack_vector(data):
    return list(struct.unpack(f'<{len(data) // 4}f', data))


def _reduce(values, dimension, mode):
    if mode == 'client_prefix_l2':
        # EmbeddingGemma 2 card: "The shortened vector must be L2-normalized" (Step 0, A3).
        if len(values) < dimension:
            raise EmbeddingError('DIMENSION_MISMATCH', f'{len(values)} values for dimension {dimension}')
        values = values[:dimension]
        # Scale by the largest magnitude first: every scaled value lies in [-1, 1] and one is exactly +-1, so the norm
        # lies in [1, sqrt(dimension)] and never overflows or vanishes, whatever finite floats came (fix round 1, F1).
        largest = max(abs(value) for value in values)
        if largest == 0:
            raise EmbeddingError('RESPONSE_INVALID', 'vector cannot be normalised')
        values = [value / largest for value in values]
        norm = math.hypot(*values)
        values = [value / norm for value in values]
    elif len(values) != dimension:
        raise EmbeddingError('DIMENSION_MISMATCH', f'{len(values)} values for dimension {dimension}')
    try:
        packed = pack_vector(values)
    except (struct.error, OverflowError) as error:
        raise EmbeddingError('RESPONSE_INVALID', 'value outside float32') from error
    vector = unpack_vector(packed)
    if not all(math.isfinite(value) for value in vector):
        raise EmbeddingError('RESPONSE_INVALID', 'non-finite value')
    return vector


def parse_embeddings(payload, count, dimension, mode):
    """Vectors in input order from an embeddings response (`data[].index`, `data[].embedding`, protocol-bound)."""
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != count:
        raise EmbeddingError('RESPONSE_INVALID', f'expected {count} embeddings')
    vectors = [None] * count
    for item in data:
        index = item.get('index') if isinstance(item, dict) else None
        if type(index) is not int or not 0 <= index < count or vectors[index] is not None:
            raise EmbeddingError('RESPONSE_INVALID', 'missing, repeated or out-of-range index')
        values = item.get('embedding')
        if not isinstance(values, list) or not values or any(type(value) not in (int, float) for value in values):
            raise EmbeddingError('RESPONSE_INVALID', 'embedding is not a list of numbers')
        try:
            values = [float(value) for value in values]  # a JSON integer may exceed the float range
        except OverflowError as error:
            raise EmbeddingError('RESPONSE_INVALID', 'embedding value outside the float range') from error
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingError('RESPONSE_INVALID', 'embedding is not a list of finite numbers')
        vectors[index] = _reduce(values, dimension, mode)
    return vectors


class Embedder:
    """Request builder and transport for one embedder backend; bounded by the backend's max_concurrency."""

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        configure_decode_backstop([settings.max_decode_pixels])
        fmt = settings.vision['format']
        # The registered type of the encoded bytes (R4F1); preflight already refused a format without one.
        self._media_type = image_media_type(fmt)
        if self._media_type is None:
            raise ConfigurationError(f'vision.format: {NO_MEDIA_TYPE.format(fmt)}')
        name = settings.backend_name
        self._transport = ModelTransport(name, settings.backend,
                                         BackendConcurrency({name: settings.backend['max_concurrency']}),
                                         transport=transport)

    async def close(self):
        await self._transport.close()

    def _base(self):
        body = {'model': self.settings.backend['model']['id'], 'encoding_format': ENCODING_FORMAT}
        if self.settings.dimension_mode == 'request_field':
            body['dimensions'] = self.settings.dimension  # vLLM `dimensions` (Step 0, A2)
        return body

    def _image_part(self, data):
        encoded = base64.b64encode(data).decode('ascii')  # vision.transport: data_uri
        return {'type': 'image_url', 'image_url': {'url': f'data:{self._media_type};base64,{encoded}'}}

    def _messages(self, system, user):
        messages = [] if system is None else [{'role': 'system', 'content': [{'type': 'text', 'text': system}]}]
        messages.append({'role': 'user', 'content': user})
        if self.settings.extra_body.get('continue_final_message'):
            # continue_final_message continues a final assistant turn, as the vLLM example sends it (A2).
            messages.append({'role': 'assistant', 'content': [{'type': 'text', 'text': ''}]})
        return messages

    def image_bodies(self, images):
        """One request per batch (llama.cpp `input` objects, A1) or per image (vLLM `messages`, A2)."""
        instruction = self.settings.image_instruction
        system = None if instruction == NO_INSTRUCTION else instruction
        if self.settings.shape == 'openai_input_content':
            prefix = [] if system is None else [{'type': 'text', 'text': system}]
            return [dict(self._base(), input=[{'content': prefix + [self._image_part(data)]} for data in images])]
        return [dict(self._base(), messages=self._messages(system, [self._image_part(data), {'type': 'text', 'text': ''}]),
                     **self.settings.extra_body) for data in images]

    def text_bodies(self, texts):
        rendered = [self.settings.text_template.replace(TEXT_PLACEHOLDER, text) for text in texts]
        if self.settings.shape == 'openai_input_content':
            return [dict(self._base(), input=rendered)]
        return [dict(self._base(), messages=self._messages(None, [{'type': 'text', 'text': text}]),
                     **self.settings.extra_body) for text in rendered]

    async def _post(self, body, count):
        route = self.settings.backend['endpoints']['embeddings']
        try:
            response = await self._transport.post(route, body, {})
        except httpx.HTTPError as error:
            raise EmbeddingError('HTTP_ERROR', type(error).__name__) from error
        if not response.is_success:
            raise EmbeddingError('HTTP_ERROR', f'status {response.status_code}')
        try:
            payload = response.json()
        except (ValueError, RecursionError) as error:  # RecursionError: nesting deeper than the decoder allows
            raise EmbeddingError('RESPONSE_INVALID', 'response is not JSON') from error
        return parse_embeddings(payload, count, self.settings.dimension, self.settings.dimension_mode)

    async def _embed(self, bodies, count):
        per_body = count // len(bodies)
        results = await asyncio.gather(*(self._post(body, per_body) for body in bodies))
        return [vector for vectors in results for vector in vectors]

    async def embed_images(self, images):
        return await self._embed(self.image_bodies(images), len(images))

    async def embed_texts(self, texts):
        return await self._embed(self.text_bodies(texts), len(texts))


async def _in_batches(items, batch_size, embed):
    """Embed batches concurrently; return (batch, vectors or EmbeddingError) pairs, re-raising anything else."""
    batches = [items[start:start + batch_size] for start in range(0, len(items), batch_size)]
    results = await asyncio.gather(*(embed(batch) for batch in batches), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, EmbeddingError):
            raise result
    return list(zip(batches, results))


def _window(settings):
    """Inputs prepared at once: enough to keep every allowed request busy, bounded in memory."""
    return settings.batch_size * settings.backend['max_concurrency']


def _load_record(checkpoints, key, expected, settings):
    try:
        record = json.loads(checkpoints.load(key))
    except ValueError as error:
        raise StorageError(f'{checkpoints.stage}: artifact {key} is not JSON') from error
    vector = record.get('vector')
    if (record.get('version') != LAYOUT_VERSION or any(record.get(field) != value for field, value in expected.items())
            or record.get('embedder_revision') != settings.revision
            or (vector is not None and len(base64.b64decode(vector)) != 4 * settings.dimension)):
        raise StorageError(f'{checkpoints.stage}: artifact {key} does not match its key')
    return record


def _skip_or_reuse(checkpoints, key, expected, settings, report, label):
    """A finished record, or None when the input must be (re)computed."""
    if checkpoints.done(key) is not None:
        report['skipped_done'] += 1
        return _load_record(checkpoints, key, expected, settings)
    if checkpoints.reusable(key) is not None:
        report['reused_referenced'].append(label)
        return _load_record(checkpoints, key, expected, settings)
    return None


def _commit_record(checkpoints, key, kind, record, revisions):
    checkpoints.commit(key, kind, canonical_json(record).encode('utf-8'), revisions=revisions)


def _read_verified(root, locations, image_id):
    for folder_id, relative_path in locations:
        try:
            data = read_source_file(root, folder_id.split('/') + relative_path.split('/'))
        except OSError:
            continue
        if 'sha256:' + hashlib.sha256(data).hexdigest() == image_id:
            return data
    return None


def _decode(data, settings):
    """One decode: (metadata, prepared bytes), with bytes None when the image is refused deterministically."""
    empty = {'format': None, 'width': None, 'height': None}
    try:
        prepared, info = prepare_vision_image_with_info(data, settings.vision, settings.max_decode_pixels)
    except ImageRejected as error:
        return dict(error.info, decode_status=error.status), None
    except Image.DecompressionBombError:
        return dict(empty, decode_status=EXCEEDS), None
    except UndecodableImage:
        # model_image's one decode boundary: only the file's bytes raise this; settings, policy and encoding faults
        # propagate and fail the stage (fix round 3, R2F1).
        return dict(empty, decode_status=UNDECODABLE), None
    return dict(info, decode_status=DECODE_OK), prepared


async def embed_images(store, config, handoff, *, knowledge_root, transport=None):
    """Part 1: embed every image of the K0 handoff under the current embedder; finished keys cost nothing.

    `handoff` is `sync()['handoff']['k1']`; `knowledge_root` is the resolved, read-only knowledge root.
    Returns the `images` rows with K1 metadata, the `image_embeddings` table and a report.
    """
    settings = embedder_settings(config)
    root = Path(knowledge_root)
    locations = {}
    for row in sorted(handoff['folder_images'], key=lambda row: (row['folder_id'], row['relative_path'])):
        locations.setdefault(row['image_id'], []).append((row['folder_id'], row['relative_path']))
    report = {'embedded': 0, 'skipped_done': 0, 'reused_referenced': [], 'decode_failed': {}, 'failed': {}}
    revisions = image_revisions(settings)
    records = {}
    embedder = Embedder(settings, transport=transport)
    try:
        with store.writer() as writer:
            checkpoints = writer.checkpoints(IMAGE_STAGE)
            pending = []
            for row in sorted(handoff['images'], key=lambda row: row['image_id']):
                image_id = row['image_id']
                if not is_image_id(image_id):
                    raise ValueError(f'invalid image_id {image_id!r}')
                key = image_key(settings, image_id)
                record = _skip_or_reuse(checkpoints, key, {'image_id': image_id}, settings, report, image_id)
                if record is None:
                    pending.append((image_id, key))
                else:
                    records[image_id] = record
            window = _window(settings)
            for start in range(0, len(pending), window):
                prepared = []
                for image_id, key in pending[start:start + window]:
                    data = _read_verified(root, locations.get(image_id, ()), image_id)
                    if data is None:
                        checkpoints.fail(key, SOURCE_CHANGED, revisions=revisions)
                        report['failed'][image_id] = SOURCE_CHANGED
                        continue
                    metadata, image = _decode(data, settings)
                    if image is None:
                        record = dict(metadata, version=LAYOUT_VERSION, image_id=image_id,
                                      embedder_revision=settings.revision, dim=settings.dimension, vector=None)
                        _commit_record(checkpoints, key, IMAGE_STAGE, record, revisions)
                        records[image_id] = record
                        report['decode_failed'][image_id] = metadata['decode_status']
                        continue
                    prepared.append((image_id, key, metadata, image))
                results = await _in_batches(prepared, settings.batch_size,
                                            lambda batch: embedder.embed_images([item[3] for item in batch]))
                for batch, result in results:
                    if isinstance(result, EmbeddingError):
                        for image_id, key, _, _ in batch:
                            checkpoints.fail(key, result.code, revisions=revisions)
                            report['failed'][image_id] = result.code
                        continue
                    for (image_id, key, metadata, _), vector in zip(batch, result):
                        record = dict(metadata, version=LAYOUT_VERSION, image_id=image_id,
                                      embedder_revision=settings.revision, dim=settings.dimension,
                                      vector=base64.b64encode(pack_vector(vector)).decode('ascii'))
                        _commit_record(checkpoints, key, IMAGE_STAGE, record, revisions)
                        records[image_id] = record
                        report['embedded'] += 1
    finally:
        await embedder.close()
    images = []
    for row in handoff['images']:
        row = dict(row)
        record = records.get(row['image_id'])
        if record is not None:
            row.update({field: record[field] for field in ('format', 'width', 'height', 'decode_status')})
        images.append(row)
    vectors = [{'image_id': image_id, 'embedder_revision': settings.revision, 'dim': settings.dimension,
                'vector': unpack_vector(base64.b64decode(record['vector']))}
               for image_id, record in sorted(records.items()) if record['vector'] is not None]
    table = store.catalog.build('image_embeddings', vectors, settings.dimension)
    check_embedding_space({'image_embeddings': table})
    return {'images': images, 'image_embeddings': table, 'embedder_revision': settings.revision,
            'dim': settings.dimension, 'report': report}


async def embed_concept_texts(store, config, items, *, transport=None):
    """Part 2: embed concept texts with the same encoder; identical text is embedded once.

    Over-long text is never truncated here: a server refusal fails the item with its reason.
    """
    settings = embedder_settings(config)
    items = list(items)
    for item in items:
        if (not isinstance(item, ConceptText) or not is_concept_id(item.concept_id) or item.text_kind not in TEXT_KIND
                or not isinstance(item.text, str) or not item.text):
            raise ValueError(f'invalid concept text {item!r}')
    report = {'embedded': 0, 'skipped_done': 0, 'reused_referenced': [], 'failed': {}}
    revisions = text_revisions(settings)
    texts = sorted({item.text for item in items})
    records = {}
    embedder = Embedder(settings, transport=transport)
    try:
        with store.writer() as writer:
            checkpoints = writer.checkpoints(TEXT_STAGE)
            pending = []
            for text in texts:
                key = text_key(settings, text)
                record = _skip_or_reuse(checkpoints, key, {'text_hash': text_hash(text)}, settings, report,
                                        text_hash(text))
                if record is None:
                    pending.append((text, key))
                else:
                    records[text] = record
            window = _window(settings)
            for start in range(0, len(pending), window):
                results = await _in_batches(pending[start:start + window], settings.batch_size,
                                            lambda batch: embedder.embed_texts([text for text, _ in batch]))
                for batch, result in results:
                    if isinstance(result, EmbeddingError):
                        for text, key in batch:
                            checkpoints.fail(key, result.code, revisions=revisions)
                            report['failed'][text_hash(text)] = result.code
                        continue
                    for (text, key), vector in zip(batch, result):
                        record = {'version': LAYOUT_VERSION, 'text_hash': text_hash(text),
                                  'embedder_revision': settings.revision, 'dim': settings.dimension,
                                  'vector': base64.b64encode(pack_vector(vector)).decode('ascii')}
                        _commit_record(checkpoints, key, TEXT_STAGE, record, revisions)
                        records[text] = record
                        report['embedded'] += 1
    finally:
        await embedder.close()
    rows = {}
    for item in items:
        record = records.get(item.text)
        if record is not None:
            row = {'concept_id': item.concept_id, 'text_kind': item.text_kind, 'embedder_revision': settings.revision,
                   'text_hash': record['text_hash'], 'dim': settings.dimension,
                   'vector': unpack_vector(base64.b64decode(record['vector']))}
            rows[(item.concept_id, item.text_kind, record['text_hash'])] = row
    table = store.catalog.build('concept_text_embeddings', [rows[key] for key in sorted(rows)], settings.dimension)
    check_embedding_space({'concept_text_embeddings': table})
    return {'concept_text_embeddings': table, 'embedder_revision': settings.revision, 'dim': settings.dimension,
            'report': report}
