"""Reference check of the embedding server against its model's documented recipe (T-018 P2; R-016, R-018, R-020).

Two steps, so one model is resident at a time:

- `reference` (GPU environment): the model card's own sentence-transformers recipe embeds the prepared inputs under
  the variations the recipe itself allows: each batch size given on the command line, the input order forward and
  reversed, and the dtypes the card names (the profile's `server.dtype` plus `Family.reference_extra_dtypes`, e.g.
  EG2 float32: "torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32"). It writes `reference.json` (every run's vectors, the prepared
  inputs' hashes, versions, the processor's own image limits) and `fit.json`: the lowest cosine between any two runs
  for any input, floored to `FIT_DECIMALS`, and the largest distance of any run's vector length from 1, rounded up
  to `FIT_DECIMALS`: the measured candidates for `vlm.backends.<name>.server.reference_check.min_cosine` and
  `.max_norm_deviation` (profile command `embed serve`: neither value is read).
- `compare` (any environment): the running server embeds the same prepared bytes sent through K1's own client
  (`k1_embed.Embedder`); an item passes when its cosine to the baseline run (the profile's dtype, batch size 1,
  forward order) is at least `min_cosine`, its length is the native dimension and its norm is finite and within
  `max_norm_deviation` of 1 (profile command `embed reference-check`: both refused while a sentinel). The checker never
  normalises: cosine ignores scale, so the norm is what catches a server that skips its L2 step (P2 fix round 1, N1).
  It writes `check.json`. The reference file is validated whole before any HTTP request (E2).

A plumbing check (smoke), never a quality claim. The pure functions here need no torch; `sentence_transformers` and
`torch` are imported inside `run_reference` only, so the server never imports them.
"""

import asyncio
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path

FIT_DECIMALS = 4  # artifact-bound: the fit floors the lowest observed cosine to this many decimals (recorded in fit.json)
FORMAT = 'embed_reference.v0'  # artifact-bound: layout of reference.json, fit.json and check.json
BASELINE_ORDER = 'forward'
ORDERS = ('forward', 'reversed')


class ReferenceCheckError(ValueError):
    """The inputs, the reference file or the measured vectors cannot support the check."""


@dataclass(frozen=True)
class Input:
    kind: str  # 'image' or 'text'
    label: str  # a file name or the text's position, never a path
    sha256: str  # of the prepared image bytes, or of the UTF-8 text
    data: bytes | None  # prepared image bytes (K1's own preparation)
    text: str | None


def vector_norm(vector):
    """Euclidean length without squaring (`math.hypot`): finite values near 1e154 would overflow a sum of squares
    (`math.fsum` raises), so this is the one length of the module (P3 class fix); infinite only when the true
    length overflows."""
    return math.hypot(*vector)


def usable(norm):
    """A length that admits a cosine: finite and non-zero."""
    return math.isfinite(norm) and norm > 0.0


def unit_vector(vector):
    """`vector` at unit length. Scaled by its largest magnitude first, as K1's `_reduce` and the server's
    `l2_normalize` do: the scaled length lies in [1, sqrt(len)], so a subnormal vector, whose `hypot` rounds to a
    neighbouring subnormal, is still divided by an accurate length (P3 fix round 1, F3)."""
    largest = max(abs(value) for value in vector)
    scaled = [value / largest for value in vector]
    length = math.hypot(*scaled)
    return [value / length for value in scaled]


def cosine(a, b):
    if len(a) != len(b) or not a:
        raise ReferenceCheckError('vectors of different or zero length')
    if not (usable(vector_norm(a)) and usable(vector_norm(b))):
        raise ReferenceCheckError('a zero or non-finite vector')
    # Unit vectors first, so every product lies in [-1, 1] and the sum cannot overflow.
    return math.fsum(x * y for x, y in zip(unit_vector(a), unit_vector(b)))


def floor_to(value, decimals=FIT_DECIMALS):
    """`value` rounded down to `decimals` places, exactly (Decimal of the float's shortest repr)."""
    return float(Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_FLOOR))


def ceil_to(value, decimals=FIT_DECIMALS):
    """`value` rounded up to `decimals` places, exactly: the outward rounding of a fitted maximum."""
    return float(Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_CEILING))


def norm_deviation(vector):
    """|‖vector‖ - 1|; infinite when the length overflows (never a pass)."""
    return abs(vector_norm(vector) - 1.0)


def fit_max_norm_deviation(runs):
    """The recipe's own distance from unit length: the largest `norm_deviation` of any vector of any run, rounded up
    (`FIT_DECIMALS`). Fitted on the documented recipe, never on the server under test, as `min_cosine` is."""
    vectors = [vector for label in sorted(runs) for vector in runs[label]]
    if not vectors:
        raise ReferenceCheckError('the fit needs at least one vector')
    raw = max(norm_deviation(vector) for vector in vectors)
    if not math.isfinite(raw):
        raise ReferenceCheckError('a non-finite vector length in the reference runs')
    return {'raw_max_norm_deviation': raw, 'max_norm_deviation': ceil_to(raw)}


def run_label(dtype, batch_size, order):
    return f'{dtype}/b{batch_size}/{order}'


def fit_min_cosine(runs):
    """The spread across runs: per input the lowest pairwise cosine, and that minimum floored (`FIT_DECIMALS`).

    `runs` maps a run label to the list of vectors of every input, in input order. At least two runs are needed, or
    there is no spread to measure."""
    labels = sorted(runs)
    if len(labels) < 2:
        raise ReferenceCheckError('the fit needs at least two runs')
    count = len(runs[labels[0]])
    if count == 0 or any(len(runs[label]) != count for label in labels):
        raise ReferenceCheckError('every run must hold one vector per input')
    per_input = []
    for index in range(count):
        lowest, pair = None, None
        for first in range(len(labels)):
            for second in range(first + 1, len(labels)):
                value = cosine(runs[labels[first]][index], runs[labels[second]][index])
                if lowest is None or value < lowest:
                    lowest, pair = value, (labels[first], labels[second])
        per_input.append({'index': index, 'min_cosine': lowest, 'pair': list(pair)})
    raw = min(row['min_cosine'] for row in per_input)
    return {'per_input': per_input, 'raw_min_cosine': raw, 'min_cosine': floor_to(raw), 'decimals': FIT_DECIMALS}


def judge(inputs, reference, served, min_cosine, max_norm_deviation, native_dimension):
    """Per-item verdicts of the served vectors against the reference vectors (same input order). Every measured
    quantity decides: cosine, length and norm (the served vector as received, never normalised here). The norm is
    classified first: a served vector with no usable length (zero) fails its item with `cosine: null` instead of
    stopping the check (P3, RT1-N1); the reference vectors were admitted with a usable length (`_check_runs`)."""
    if not len(inputs) == len(reference) == len(served):
        raise ReferenceCheckError('the reference and the served vectors do not cover the same inputs')
    items = []
    for item, expected, got in zip(inputs, reference, served):
        norm = vector_norm(got)
        comparable = len(got) == len(expected) and usable(norm) and usable(vector_norm(expected))
        value = cosine(expected, got) if comparable else None
        unit = math.isfinite(norm) and abs(norm - 1.0) <= max_norm_deviation
        passed = value is not None and value >= min_cosine and len(got) == native_dimension and unit
        items.append({'kind': item.kind, 'label': item.label, 'sha256': item.sha256, 'cosine': value,
                      'dimension': len(got), 'norm': norm, 'verdict': 'pass' if passed else 'fail'})
    return {'min_cosine': min_cosine, 'max_norm_deviation': max_norm_deviation, 'native_dimension': native_dimension,
            'items': items, 'passed': all(item['verdict'] == 'pass' for item in items)}


def _inside(root, path, what):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise ReferenceCheckError(f'{what} must lie inside the project root (after resolving links)')
    return resolved


PROTECTED_DIRS = ('.secrets', '.git', 'models')  # safety-bound: the project's protected roots (storage/store.py)
PROTECTED_PREFIX = '.venv'  # safety-bound: every environment directory (`.venv`, `.venv-gpu`, ...)


def check_output_dir(root, path, apart=()):
    """The resolved report directory (links followed): inside the project root, outside its protected directories
    and outside every `(name, directory)` of `apart` (the read-only knowledge root, the store), so a report never
    creates or replaces a file there (R-014; P3 fix round 1, F2). Windows paths compare case-insensitively."""
    root = Path(root).resolve()
    resolved = _inside(root, path, '--out')
    parts = resolved.relative_to(root).parts
    first = os.path.normcase(parts[0]) if parts else ''
    if first in {os.path.normcase(name) for name in PROTECTED_DIRS} or first.startswith(PROTECTED_PREFIX):
        raise ReferenceCheckError('--out lies in a protected project directory (after resolving links)')
    for name, directory in apart:
        directory = Path(directory).resolve()
        if resolved == directory or directory in resolved.parents:
            raise ReferenceCheckError(f'--out lies inside {name} (after resolving links)')
    return resolved


def prepare_inputs(project_root, image_paths, texts_path, k1_settings):
    """Images prepared exactly as K1 prepares them (`model_image.prepare_vision_image_with_info` with the profile's
    `vision` settings), then the texts of a JSON list of strings. Paths must lie inside the project root."""
    from PIL import Image

    from ..model_image import prepare_vision_image_with_info

    inputs = []
    for path in image_paths:
        resolved = _inside(project_root, path, '--image')
        try:
            prepared, _ = prepare_vision_image_with_info(resolved.read_bytes(), k1_settings.vision,
                                                         k1_settings.max_decode_pixels)
        except (ValueError, OSError, Image.DecompressionBombError) as error:  # K1 refuses these images too
            raise ReferenceCheckError(f'--image {resolved.name}: refused by the K1 preparation ({type(error).__name__})')
        inputs.append(Input('image', resolved.name, hashlib.sha256(prepared).hexdigest(), prepared, None))
    if texts_path is not None:
        try:
            texts = json.loads(_inside(project_root, texts_path, '--texts').read_text(encoding='utf-8'))
        except ValueError as error:
            raise ReferenceCheckError(f'--texts is not JSON ({type(error).__name__})') from error
        if not isinstance(texts, list) or not all(isinstance(text, str) and text for text in texts):
            raise ReferenceCheckError('--texts must hold a JSON list of non-empty strings')
        for index, text in enumerate(texts):
            inputs.append(Input('text', f'text-{index}', hashlib.sha256(text.encode('utf-8')).hexdigest(), None, text))
    if not inputs:
        raise ReferenceCheckError('no inputs: give --image and/or --texts')
    return inputs


def processor_limits(settings):
    """The image limits of the profile, in the shape the processor reports them (`reference.json` records the
    processor's own values; `compare` refuses a profile whose limits differ, since that compares different images)."""
    if settings.family.pooling == 'mean':
        return {'max_soft_tokens': settings.max_soft_tokens}
    return {'size': {'shortest_edge': settings.min_pixels, 'longest_edge': settings.max_pixels}}


def _identity(settings):
    return {'model_id': settings.model_id, 'revision': settings.revision, 'family': settings.family.name,
            'dtype': settings.dtype, 'attn_implementation': settings.attn_implementation,
            'default_instruction': settings.default_instruction, 'max_length': settings.max_length}


def _write(settings, path, payload):
    """Write a report through the anchored storage layer below the project root (each component opened relative
    to its verified parent, atomic rename): `path` was resolved inside the root by `_inside`; a component outside
    the portable segment rule or a link swapped in below the root is refused (PM integration fix, T-018 P2)."""
    from ..storage._anchored import atomic_write_bytes
    from ..storage._fs import StorageError, check_segment

    root = Path(settings.project_root).resolve()
    try:
        parts = Path(path).relative_to(root).parts
        for part in parts:
            check_segment(part)
        atomic_write_bytes(root, '/'.join(parts), (json.dumps(payload, indent=1, sort_keys=True) + '\n').encode())
    except (ValueError, StorageError, OSError) as error:  # OSError text holds the path: the type only (F4)
        raise ReferenceCheckError(f'cannot write {path.name} ({type(error).__name__})') from None


def run_reference(settings, model_dir, inputs, batch_sizes, out_dir):
    """Embed `inputs` with the card's sentence-transformers recipe under every allowed variation; write
    `reference.json` and `fit.json` under `out_dir`; return the fit. One model is loaded at a time and released
    before the next (and before the function returns)."""
    import gc

    from PIL import Image

    check_batch_sizes(batch_sizes)
    try:
        import sentence_transformers
        from sentence_transformers import SentenceTransformer
        import torch
        import transformers
    except ImportError as error:
        raise ReferenceCheckError('the reference step needs torch, transformers and sentence-transformers; run it in '
                                  'the GPU environment (.venv-gpu, requirements-gpu.txt)') from error
    device = f'cuda:{settings.gpu}'
    total = torch.cuda.get_device_properties(settings.gpu).total_memory
    torch.cuda.set_per_process_memory_fraction(settings.vram_budget_gb * 1024 ** 3 / total, settings.gpu)
    images = [item for item in inputs if item.kind == 'image']
    texts = [item for item in inputs if item.kind == 'text']
    runs, recipe = {}, None
    for dtype in reference_dtypes(settings):
        model = SentenceTransformer(str(model_dir), device=device, local_files_only=True, trust_remote_code=False,
                                    model_kwargs={'dtype': getattr(torch, dtype),
                                                  'attn_implementation': settings.attn_implementation})
        try:
            if recipe is None:
                recipe = _recipe(model)
            for batch_size in batch_sizes:
                for order in ORDERS:
                    runs[run_label(dtype, batch_size, order)] = _encode(model, images, texts, batch_size, order,
                                                                        settings.family, Image)
        finally:
            del model
            gc.collect()
            torch.cuda.empty_cache()
    baseline = run_label(settings.dtype, 1, BASELINE_ORDER)
    fit = dict(fit_min_cosine(runs), **fit_max_norm_deviation(runs))
    environment = {'torch': torch.__version__, 'cuda': torch.version.cuda, 'transformers': transformers.__version__,
                   'sentence_transformers': sentence_transformers.__version__,
                   'device': torch.cuda.get_device_name(settings.gpu)}
    common = {'format': FORMAT, 'identity': _identity(settings), 'environment': environment,
              'inputs': [{'kind': item.kind, 'label': item.label, 'sha256': item.sha256} for item in inputs]}
    _write(settings, out_dir / 'reference.json', dict(common, recipe=recipe, baseline=baseline, runs=runs))
    _write(settings, out_dir / 'fit.json', dict(common, fit=fit, runs=sorted(runs)))
    return fit


def reference_dtypes(settings):
    """The profile's dtype (the baseline) first, then the other dtypes the family's card sanctions."""
    return tuple(dict.fromkeys((settings.dtype, *settings.family.reference_extra_dtypes)))


def check_batch_sizes(batch_sizes):
    if not batch_sizes or any(type(size) is not int or size < 1 for size in batch_sizes):
        raise ReferenceCheckError('--batch-size must be positive integers')
    if 1 not in batch_sizes:
        raise ReferenceCheckError('--batch-size 1 is required: it is the baseline the server is compared with')
    if len(set(batch_sizes)) != len(batch_sizes):
        raise ReferenceCheckError('--batch-size repeats a value')


def _recipe(model):
    """What the loaded recipe does, recorded so `compare` can refuse a profile that would compare other inputs."""
    processor = getattr(model[0], 'processor', None)
    image = getattr(processor, 'image_processor', None)
    limits = {}
    for name in ('max_soft_tokens', 'size'):
        value = getattr(image, name, None)
        if value is not None:
            # transformers 5 holds `size` as a `SizeDict` (not a dict): dict() keeps its set fields (P2 stage B)
            limits[name] = value if isinstance(value, (bool, int, float, str)) else dict(value)
    return {'default_prompt_name': model.default_prompt_name, 'prompts': dict(model.prompts),
            'image_limits': limits, 'max_seq_length': model.max_seq_length,
            'modules': [type(module).__name__ for module in model]}


def _encode(model, images, texts, batch_size, order, family, image_module):
    """One run: images then texts, in `order`, at `batch_size`; vectors returned in input order."""
    def ordered(items):
        return list(reversed(items)) if order == 'reversed' else list(items)

    def restore(vectors):
        vectors = [list(map(float, vector)) for vector in vectors]
        return list(reversed(vectors)) if order == 'reversed' else vectors

    vectors = []
    if images:
        pictures = [{'image': image_module.open(io.BytesIO(item.data)).convert('RGB')} for item in ordered(images)]
        vectors += restore(model.encode(pictures, batch_size=batch_size, normalize_embeddings=True,
                                        convert_to_numpy=True))
    if texts:
        options = {} if family.reference_text_prompt_name is None else {'prompt_name': family.reference_text_prompt_name}
        vectors += restore(model.encode([item.text for item in ordered(texts)], batch_size=batch_size,
                                        normalize_embeddings=True, convert_to_numpy=True, **options))
    return vectors


def _check_runs(runs, count):
    """Structure of `runs`: a non-empty map of run label to one vector per input, every vector a non-empty list of
    finite floats (the reference step writes floats only) with a usable length (finite, non-zero: every baseline
    must admit a cosine, so an unusable one is refused before any request, P3 RT1-E2) and all of one size."""
    if not isinstance(runs, dict) or not runs:
        raise ReferenceCheckError('the reference file holds no runs')
    lengths = set()
    for vectors in runs.values():
        if not isinstance(vectors, list) or len(vectors) != count:
            raise ReferenceCheckError('a reference run does not hold one vector per input')
        for vector in vectors:
            if (not isinstance(vector, list) or not vector
                    or not all(type(value) is float and math.isfinite(value) for value in vector)):
                raise ReferenceCheckError('a reference vector is not a non-empty list of finite numbers')
            if not usable(vector_norm(vector)):
                raise ReferenceCheckError('a reference vector has a zero or non-finite length')
            lengths.add(len(vector))
    if len(lengths) != 1:
        raise ReferenceCheckError('the reference vectors differ in length')


def load_reference(path, settings, inputs):
    """Read `reference.json` and refuse it unless it was made for this profile's model and output settings and for
    exactly these prepared inputs. The whole file is validated before any request is sent (E2): a file that cannot be
    decoded or does not have the written structure is a `ReferenceCheckError`, never a `KeyError` or `TypeError`."""
    try:
        reference = json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError, RecursionError) as error:  # JSONDecodeError and UnicodeDecodeError are ValueErrors; nesting
        raise ReferenceCheckError(f'the reference file is not UTF-8 JSON ({type(error).__name__})') from error
    if not isinstance(reference, dict) or reference.get('format') != FORMAT:
        raise ReferenceCheckError(f'not a {FORMAT} reference file')
    if reference.get('identity') != _identity(settings):  # equality of plain JSON values never raises
        raise ReferenceCheckError('the reference was made for another model, revision or server output setting')
    expected = [{'kind': item.kind, 'label': item.label, 'sha256': item.sha256} for item in inputs]
    if reference.get('inputs') != expected:
        raise ReferenceCheckError('the reference was made for other inputs (prepared bytes or texts differ)')
    recipe = reference.get('recipe')
    if not isinstance(recipe, dict) or recipe.get('image_limits') != processor_limits(settings):
        raise ReferenceCheckError('the profile image limits differ from the reference processor\'s own limits')
    _check_runs(reference.get('runs'), len(expected))
    baseline = reference.get('baseline')
    if not isinstance(baseline, str) or baseline not in reference['runs']:
        raise ReferenceCheckError('the reference holds no baseline run')
    if settings.dimension_mode != 'native' or any(len(vector) != settings.dimension
                                                   for vector in reference['runs'][baseline]):
        raise ReferenceCheckError('the check compares native vectors: embedding.dimension_mode must be native and '
                                  'embedding.dimension the reference\'s vector length')
    return reference


def run_compare(config, settings, k1_settings, inputs, reference, min_cosine, max_norm_deviation, out_dir,
                transport=None):
    """Send the inputs to the running server through K1's client and judge each item; write `check.json`. A request
    or response K1 refuses raises its `k1_embed.EmbeddingError` (the command turns it into a refusal)."""
    from ..k1_embed import Embedder

    async def served():
        embedder = Embedder(k1_settings, transport=transport)
        try:
            images = [item.data for item in inputs if item.kind == 'image']
            texts = [item.text for item in inputs if item.kind == 'text']
            vectors = await embedder.embed_images(images) if images else []
            return vectors + (await embedder.embed_texts(texts) if texts else [])
        finally:
            await embedder.close()

    vectors = asyncio.run(served())
    verdict = judge(inputs, reference['runs'][reference['baseline']], vectors, min_cosine, max_norm_deviation,
                    settings.dimension)
    _write(settings, out_dir / 'check.json', {'format': FORMAT, 'identity': _identity(settings), 'baseline': reference['baseline'],
                                    'verdict': verdict, 'served': vectors})
    return verdict
