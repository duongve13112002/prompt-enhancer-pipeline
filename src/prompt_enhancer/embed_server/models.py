"""Model adapters of the embedding server: the only module that imports torch and transformers (T-018).

Written from the model cards and the `transformers` v5.23.0 source (docs/stages/K1-embed.md, task file T-018), and
checked on the dev GPU by the P2 reference check against each card's sentence-transformers path (EmbeddingGemma 2
and Qwen3-VL-Embedding-2B, 2 images and 2 texts each, every item at or above the fitted `min_cosine`): a smoke
check, not a quality claim. Every import of a heavy library is inside a function, so the CPU
suite (no torch) imports this module without cost and tests the engine through a labelled double.

Request path: group the items by kind, run each group in chunks of `server.forward_batch`, pool and normalise on
the host with `pooling.py`, return one vector per item in input order.
"""

from dataclasses import dataclass
import gc
import json
from pathlib import Path

from .families import check_weights_config
from .pooling import l2_normalize, last_token_index, mean_from_sums
from .protocol import EngineError


class ServerStartError(RuntimeError):
    """The model cannot be loaded as configured (wrong weights, no GPU, budget above the card's memory)."""


@dataclass(frozen=True)
class EmbedResult:
    vectors: list
    prompt_tokens: int


class TransformersEngine:
    """One loaded model; `embed` is called under the server's single GPU lock."""

    def __init__(self, settings, torch, model, processor, native_dimension):
        self.settings, self._torch, self._model, self._processor = settings, torch, model, processor
        self.native_dimension = native_dimension
        self.device = f'cuda:{settings.gpu}'

    def close(self):
        self._model = None
        gc.collect()
        self._torch.cuda.empty_cache()

    def _text_limit(self):
        """`server.max_length` for a text input: the tokenizer cuts a longer text. An image input is never cut (a cut
        would drop image tokens the model needs); `_run` refuses one that does not fit instead."""
        return {'truncation': True, 'max_length': self.settings.max_length}

    def _gemma_inputs(self, items):
        if items[0].image is None:
            return self._processor(text=[item.text for item in items], padding=True, return_tensors='pt',
                                   **self._text_limit())
        # The card recipe (sentence-transformers' message modality) renders an image through the model's own chat
        # template, which writes the image placeholder the processor then expands; an empty text carries none, and
        # the processor refuses the image (P2 GPU run). The template, a model artifact, decides the placeholder.
        texts = [self._processor.apply_chat_template([{'role': 'user', 'content': [{'type': 'image', 'image': item.image}]}],
                                                     tokenize=False) for item in items]
        return self._processor(text=texts, images=[item.image for item in items], padding=True, return_tensors='pt')

    def _qwen_inputs(self, items):
        texts, images = [], []
        for item in items:
            instruction = item.instruction or self.settings.default_instruction
            messages = [] if instruction is None else [{'role': 'system', 'content': [{'type': 'text', 'text': instruction}]}]
            user = [{'type': 'text', 'text': item.text}] if item.image is None else [{'type': 'image', 'image': item.image}]
            messages.append({'role': 'user', 'content': user})
            texts.append(self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True))
            if item.image is not None:
                images.append(item.image)
        return self._processor(text=texts, images=images or None, padding=True, return_tensors='pt',
                               **({} if images else self._text_limit()))

    def _pool(self, hidden, mask):
        torch = self._torch
        rows = mask.tolist()
        if self.settings.family.pooling == 'mean':
            sums = (hidden.float() * mask.unsqueeze(-1).to(torch.float32)).sum(dim=1).tolist()
            return [l2_normalize(mean_from_sums(total, sum(row))) for total, row in zip(sums, rows)]
        positions = torch.tensor([last_token_index(row) for row in rows], device=hidden.device)
        picked = hidden[torch.arange(len(rows), device=hidden.device), positions].float().tolist()
        return [l2_normalize(vector) for vector in picked]

    def _run(self, items):
        build = self._gemma_inputs if self.settings.family.pooling == 'mean' else self._qwen_inputs
        batch = build(items).to(self.device)
        if items[0].image is not None and batch['input_ids'].shape[1] > self.settings.max_length:
            raise EngineError('the image needs more tokens than server.max_length allows')
        with self._torch.inference_mode():
            hidden = self._model(**batch).last_hidden_state
        mask = batch['attention_mask']
        return self._pool(hidden, mask), int(mask.sum())

    def embed(self, items):
        vectors, tokens = [None] * len(items), 0
        step = self.settings.forward_batch
        try:
            for wanted in (False, True):  # text first, then images: a chunk never mixes the two input kinds
                indices = [index for index, item in enumerate(items) if (item.image is not None) is wanted]
                for start in range(0, len(indices), step):
                    chunk = indices[start:start + step]
                    pooled, count = self._run([items[index] for index in chunk])
                    tokens += count
                    for index, vector in zip(chunk, pooled):
                        vectors[index] = vector
        except self._torch.cuda.OutOfMemoryError as error:
            self._torch.cuda.empty_cache()
            raise EngineError('out of memory on the GPU') from error
        except ValueError as error:  # a pooling refusal: no real token, a zero or non-finite vector
            raise EngineError(str(error)) from error
        if any(len(vector) != self.native_dimension for vector in vectors):
            raise EngineError('a vector does not have the native dimension')
        return EmbedResult(vectors, tokens)


def _apply_image_limits(settings, processor):
    """Set the profile's image limits on the processor and refuse when the library did not take them."""
    image = processor.image_processor
    if settings.family.pooling == 'mean':
        image.max_soft_tokens = settings.max_soft_tokens
        taken = getattr(image, 'max_soft_tokens', None) == settings.max_soft_tokens
    else:
        image.size = {'shortest_edge': settings.min_pixels, 'longest_edge': settings.max_pixels}
        taken = getattr(image, 'size', None) == {'shortest_edge': settings.min_pixels, 'longest_edge': settings.max_pixels}
    if not taken:
        raise ServerStartError('the image processor did not take the configured image limits')


def load_engine(settings, model_dir):
    """Load the verified weights in `model_dir` on the configured GPU; refuse any mismatch before reading weights."""
    try:
        config = json.loads(Path(model_dir, 'config.json').read_bytes())
        native = check_weights_config(settings.family, config, settings.dimension, settings.dimension_mode)
    except (OSError, ValueError) as error:
        raise ServerStartError(f'{model_dir}: {error}') from error
    try:
        import torch  # heavy imports stay inside the function (module docstring)
        from transformers import AutoModel, AutoProcessor
    except ImportError as error:
        raise ServerStartError('torch and transformers are required; run the server in the GPU environment '
                               '(.venv-gpu, docs/dependencies-and-environments.md)') from error

    if not torch.cuda.is_available() or torch.cuda.device_count() <= settings.gpu:
        raise ServerStartError(f'runtime.gpus: GPU {settings.gpu} is not available to torch')
    total = torch.cuda.get_device_properties(settings.gpu).total_memory
    budget = settings.vram_budget_gb * 1024 ** 3
    if budget > total:
        raise ServerStartError('runtime.vram_budget_gb exceeds the memory of the configured GPU')
    torch.cuda.set_per_process_memory_fraction(budget / total, settings.gpu)
    kwargs = {'trust_remote_code': False}  # safety-bound: remote code is never run (families.check_weights_config)
    if settings.family.pooling == 'last_token':  # the Qwen reference script pads on the right; mean pooling ignores padding
        kwargs['padding_side'] = 'right'
    processor = AutoProcessor.from_pretrained(str(model_dir), **kwargs)
    _apply_image_limits(settings, processor)
    model = AutoModel.from_pretrained(str(model_dir), dtype=getattr(torch, settings.dtype),
                                      attn_implementation=settings.attn_implementation, trust_remote_code=False)
    model = model.to(f'cuda:{settings.gpu}').eval()
    return TransformersEngine(settings, torch, model, processor, native)
