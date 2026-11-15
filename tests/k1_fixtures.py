"""Shared K1 test fixtures: synthetic images, a K0 handoff and a labelled HTTP embedding-server double."""

import base64
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import struct

import httpx
from PIL import Image

from prompt_enhancer.k0_sync import sync


def image_bytes(size=(6, 4), color=(10, 20, 30), mode='RGB', fmt='PNG', orientation=None):
    buffer = BytesIO()
    image = Image.new(mode, size, color)
    if orientation is None:
        image.save(buffer, fmt)
    else:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(buffer, fmt, exif=exif)
    return buffer.getvalue()


def bigtiff_with_strip_offset(offset):
    """A synthetic 3x2 BigTIFF whose StripOffsets entry is rewritten as one LONG8 value (retest 1, RT1)."""
    buffer = BytesIO()
    Image.new('RGB', (3, 2)).save(buffer, format='TIFF', big_tiff=True)
    data = bytearray(buffer.getvalue())
    directory = struct.unpack_from('<Q', data, 8)[0]  # BigTIFF header: first IFD offset at byte 8
    for index in range(struct.unpack_from('<Q', data, directory)[0]):
        entry = directory + 8 + index * 20  # BigTIFF IFD entry: tag, type, count (8 bytes), value (8 bytes)
        if struct.unpack_from('<H', data, entry)[0] == 273:  # StripOffsets
            struct.pack_into('<HQQ', data, entry + 2, 16, 1, offset)  # type 16 is LONG8
            return bytes(data)
    raise AssertionError('the synthetic BigTIFF has no StripOffsets entry')


def make_source(root, files):
    """One concept folder holding the given image files (name -> bytes)."""
    folder = root / 'folder'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'info.txt').write_text('Kimono\ndescription', encoding='utf-8')
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return root


def k0_handoff(store, source, stage='k1', extensions=('.png', '.jpg')):
    """The real K0 sync result for the source, so K1 consumes the handoff K0 actually produces."""
    result = sync(store, source, info_names=['info.txt'], encoding='utf-8', extensions=list(extensions),
                  separator='|', workers=1, threshold=0.5)
    return result['handoff'][stage]


def k1_profile(model_test_profile, **embedding):
    """The conftest profile with embedding overrides; nested `request` members merge."""
    profile = deepcopy(model_test_profile)
    request = embedding.pop('request', {})
    profile['embedding'].update(embedding)
    profile['embedding']['request'].update(request)
    return profile


class EmbeddingServer:
    """Test double of an embedding server (no network): records requests, answers with content-derived vectors.

    `length` is the vector length it returns; `status` forces an HTTP status; `payload` replaces the response.
    """

    def __init__(self, length=4, status=200, payload=None):
        self.length, self.status, self.payload = length, status, payload
        self.requests = []

    @staticmethod
    def items(body):
        return body['input'] if 'input' in body else [body['messages']]

    def vector(self, item):
        digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode('utf-8')).digest()
        return [(digest[index % 32] + index) / 300 + 0.1 for index in range(self.length)]

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), body))
        if self.status != 200:
            return httpx.Response(self.status, json={'error': 'test double'})
        if self.payload is not None:
            return httpx.Response(200, json=self.payload)
        data = [{'object': 'embedding', 'index': index, 'embedding': self.vector(item)}
                for index, item in enumerate(self.items(body))]
        return httpx.Response(200, json={'object': 'list', 'data': list(reversed(data))})

    def transport(self):
        return httpx.MockTransport(self.handler)

    def input_counts(self):
        return [len(self.items(body)) for _, body in self.requests]


def data_uri_image(part):
    """Decode an `image_url` data URI part back to a Pillow image."""
    url = part['image_url']['url']
    header, encoded = url.split(',', 1)
    return header, Image.open(BytesIO(base64.b64decode(encoded)))
