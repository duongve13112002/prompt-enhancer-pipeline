"""The adapters of the embedding server observed at their consumers (T-018 fix round 1, review R1).

Every `server.*` value must change what the adapter does. No real torch or transformers is imported: the doubles
below are LABELLED test doubles (a numpy-backed tensor, a recording processor and model, recording `from_pretrained`
functions). They prove the settings reach the library calls; whether the real libraries take them as intended is the
reference check of phase P2 (`not_run` here).
"""

import contextlib
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from prompt_enhancer.embed_server.models import ServerStartError, TransformersEngine, load_engine
from prompt_enhancer.embed_server.pooling import l2_normalize
from prompt_enhancer.embed_server.protocol import EmbedItem, EngineError
from test_embed_server_http import make_settings


class Tensor(np.ndarray):
    """Labelled double of the few torch tensor operations the adapter uses."""

    def float(self):
        return self.astype(np.float32).view(Tensor)

    def unsqueeze(self, dim):
        return np.expand_dims(self, dim).view(Tensor)

    def to(self, target):
        return self.astype(target).view(Tensor) if isinstance(target, type) else self

    def sum(self, dim=None):
        return np.ndarray.sum(self.view(np.ndarray), axis=dim).view(Tensor)

    @property
    def device(self):
        return 'cpu'


def tensor(values, dtype=None):
    return np.array(values, dtype=dtype).view(Tensor)


FAKE_TORCH = SimpleNamespace(
    float32=np.float32, inference_mode=contextlib.nullcontext, tensor=lambda data, device=None: tensor(data),
    arange=lambda count, device=None: tensor(np.arange(count)),
    cuda=SimpleNamespace(OutOfMemoryError=MemoryError, empty_cache=lambda: None))


class Batch(dict):
    def to(self, device):
        self.device = device
        return self


class Pipeline:
    """A recording processor and model: the processor turns a text into token ids (its characters), the model maps
    token id t at position p to the hidden row [t, p, 1, 0]."""

    def __init__(self, image_tokens=3):
        self.processor_calls, self.templates, self.model_calls = [], [], []
        self.image_tokens = image_tokens

    def processor(self, **kwargs):
        self.processor_calls.append(kwargs)
        texts = kwargs['text']
        if kwargs.get('images'):
            rows = [[ord('i')] * self.image_tokens for _ in texts]
        else:
            rows = [[ord(char) for char in text] for text in texts]
        if kwargs.get('truncation') and 'max_length' in kwargs:
            rows = [row[:kwargs['max_length']] for row in rows]
        width = max(len(row) for row in rows)
        ids = [row + [0] * (width - len(row)) for row in rows]  # right padding
        mask = [[1] * len(row) + [0] * (width - len(row)) for row in rows]
        return Batch(input_ids=tensor(ids), attention_mask=tensor(mask))

    def chat(self, messages, **kwargs):
        self.templates.append(messages)
        return ''.join(part['text'] for message in messages for part in message['content'] if part['type'] == 'text')

    def model(self, input_ids, attention_mask):
        self.model_calls.append(input_ids.shape)
        hidden = np.zeros(input_ids.shape + (4,), dtype=np.float32)
        hidden[..., 0] = np.asarray(input_ids)
        hidden[..., 1] = np.arange(input_ids.shape[1])[None, :]
        hidden[..., 2] = 1
        return SimpleNamespace(last_hidden_state=hidden.view(Tensor))


def engine(settings, pipeline=None):
    pipeline = pipeline or Pipeline()
    processor = type('P', (), {'__call__': lambda self, **kw: pipeline.processor(**kw),
                               'apply_chat_template': lambda self, messages, **kw: pipeline.chat(messages, **kw)})()
    return TransformersEngine(settings, FAKE_TORCH, pipeline.model, processor, 4), pipeline


def image_item(instruction=None):
    return EmbedItem(image=Image.new('RGB', (2, 2)), instruction=instruction)


@pytest.fixture
def gemma_settings(project_root, model_test_profile):
    return make_settings(project_root, model_test_profile)[0]


@pytest.fixture
def qwen_settings(project_root, model_test_profile):
    return make_settings(project_root, model_test_profile, family='qwen3_vl_embedding')[0]


# --- max_length: the review's R1 -----------------------------------------------------------------------------

@pytest.mark.parametrize('limit', [3, 17])
def test_gemma_text_is_cut_at_the_configured_length(gemma_settings, limit):
    built, pipe = engine(replace(gemma_settings, max_length=limit))
    result = built.embed([EmbedItem(text='x' * 40)])
    assert pipe.processor_calls[0]['truncation'] is True and pipe.processor_calls[0]['max_length'] == limit
    assert result.prompt_tokens == limit  # the processor cut the text, the model saw `limit` tokens


@pytest.mark.parametrize('limit', [3, 17])
def test_qwen_text_is_cut_at_the_configured_length(qwen_settings, limit):
    built, pipe = engine(replace(qwen_settings, max_length=limit))
    built.embed([EmbedItem(text='x' * 40)])
    assert pipe.processor_calls[0]['truncation'] is True and pipe.processor_calls[0]['max_length'] == limit


@pytest.mark.parametrize('family', ['gemma', 'qwen'])
def test_an_image_is_never_cut_and_one_over_the_limit_is_refused(gemma_settings, qwen_settings, family):
    settings = gemma_settings if family == 'gemma' else qwen_settings
    exact = engine(replace(settings, max_length=3), Pipeline(image_tokens=3))[0]  # exactly the limit: accepted
    assert len(exact.embed([image_item()]).vectors) == 1
    built, pipe = engine(replace(settings, max_length=3), Pipeline(image_tokens=4))
    with pytest.raises(EngineError, match='max_length'):
        built.embed([image_item()])
    assert 'truncation' not in pipe.processor_calls[0] and 'max_length' not in pipe.processor_calls[0]


# --- instructions, pooling, chunking ---------------------------------------------------------------------------

def test_qwen_uses_the_item_instruction_else_the_profile_default(qwen_settings):
    settings = replace(qwen_settings, default_instruction='Profile default.')
    built, pipe = engine(settings)
    built.embed([EmbedItem(text='plain text'), image_item('Item instruction.'), image_item()])
    system = [[m for m in messages if m['role'] == 'system'][0]['content'][0]['text'] for messages in pipe.templates]
    assert system == ['Profile default.', 'Item instruction.', 'Profile default.']
    assert pipe.templates[0][1] == {'role': 'user', 'content': [{'type': 'text', 'text': 'plain text'}]}  # data stays user


def test_a_gemma_image_is_rendered_through_the_model_chat_template(gemma_settings):
    # P2 GPU run: an empty text carries no image placeholder and the real processor refused the image; the card
    # recipe renders a lone user image part through the model's template, which writes the placeholder.
    class Placeholder(Pipeline):  # labelled double: its template writes a marker per image part, as the real one does
        def chat(self, messages, **kwargs):
            self.templates.append(messages)
            return ''.join(part.get('text', '<image>') for message in messages for part in message['content'])

    built, pipe = engine(replace(gemma_settings, forward_batch=2), Placeholder())
    first, second = image_item(), image_item()
    built.embed([first, second])
    assert pipe.templates == [[{'role': 'user', 'content': [{'type': 'image', 'image': item.image}]}]
                              for item in (first, second)]
    assert len(pipe.processor_calls) == 1 and pipe.processor_calls[0]['images'] == [first.image, second.image]
    assert pipe.processor_calls[0]['text'] == ['<image>', '<image>']  # the template's rendering reaches the processor


def test_a_family_without_an_instruction_sends_no_system_message(gemma_settings):
    built, pipe = engine(gemma_settings)
    built.embed([EmbedItem(text='abc')])
    assert pipe.templates == [] and pipe.processor_calls[0]['text'] == ['abc']


def test_the_family_chooses_mean_or_last_token_pooling(gemma_settings, qwen_settings):
    gemma, _ = engine(gemma_settings)
    [vector] = gemma.embed([EmbedItem(text='ab')]).vectors  # tokens a=97 (position 0) and b=98 (position 1)
    assert vector == pytest.approx(l2_normalize([(97 + 98) / 2, 0.5, 1, 0]))  # mean over the two tokens
    qwen, pipe = engine(replace(qwen_settings, default_instruction='x'))
    [vector] = qwen.embed([EmbedItem(text='ab')]).vectors  # the prompt is 'xab': the last token is b at position 2
    assert vector == pytest.approx(l2_normalize([98, 2, 1, 0]))


def test_padding_never_enters_the_pool(gemma_settings):
    built, _ = engine(gemma_settings)
    alone = built.embed([EmbedItem(text='ab')]).vectors[0]
    padded = built.embed([EmbedItem(text='ab'), EmbedItem(text='abcdefgh')]).vectors[0]
    assert padded == pytest.approx(alone)


def test_forward_batch_sets_the_chunk_size_and_the_order_is_kept(gemma_settings):
    built, pipe = engine(replace(gemma_settings, forward_batch=2))
    items = [EmbedItem(text=text) for text in ('a', 'bb', 'ccc', 'dddd', 'eeeee')]
    result = built.embed(items)
    assert [call[0] for call in pipe.model_calls] == [2, 2, 1]
    assert [len(call['text']) for call in pipe.processor_calls] == [2, 2, 1]
    assert all(len(vector) == 4 for vector in result.vectors)
    for item, vector in zip(items, result.vectors):  # the mean of identical characters is that character's id
        assert vector[0] / vector[2] == pytest.approx(ord(item.text[0]))
    mixed, pipe = engine(replace(gemma_settings, forward_batch=1))
    mixed.embed([EmbedItem(text='first'), image_item(), EmbedItem(text='third'), image_item()])
    assert [len(call['text']) for call in pipe.processor_calls] == [1, 1, 1, 1]  # text first, then images


# --- the loader: every call-site value comes from the settings ----------------------------------------------------

class ModelDouble:
    def __init__(self, records):
        self.records = records

    def to(self, device):
        self.records['device'] = device
        return self

    def eval(self):
        self.records['eval'] = True
        return self


def fake_libraries(monkeypatch, records, processor, total=10 * 1024 ** 3, gpus=2):
    cuda = SimpleNamespace(is_available=lambda: gpus > 0, device_count=lambda: gpus,
                           get_device_properties=lambda index: SimpleNamespace(total_memory=total),
                           set_per_process_memory_fraction=lambda fraction, index: records.update(memory=(fraction, index)))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda, float32='labelled-float32',
                                                              bfloat16='labelled-bfloat16'))

    def load_processor(path, **kwargs):
        records['processor'] = (path, kwargs)
        return processor

    def load_model(path, **kwargs):
        records['model'] = (path, kwargs)
        return ModelDouble(records)

    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoModel=SimpleNamespace(from_pretrained=load_model), AutoProcessor=SimpleNamespace(from_pretrained=load_processor)))


def weights_directory(tmp_path, settings, **extra):
    directory = tmp_path / 'labelled-weights'
    directory.mkdir(exist_ok=True)
    config = {'model_type': settings.family.model_type, 'text_config': {settings.family.native_dimension_path[-1]: 4}}
    config.update(extra)
    (directory / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    return directory


@pytest.mark.parametrize('family', ['embedding_gemma2', 'qwen3_vl_embedding'])
def test_the_loader_passes_every_setting_to_the_library(project_root, model_test_profile, tmp_path, monkeypatch, family):
    settings = make_settings(project_root, model_test_profile, family=family, dtype='float32',
                             attn_implementation='eager')[0]
    settings = replace(settings, gpu=1, vram_budget_gb=2.5)
    records, processor = {}, SimpleNamespace(image_processor=SimpleNamespace())
    fake_libraries(monkeypatch, records, processor)
    built = load_engine(settings, weights_directory(tmp_path, settings))
    assert records['device'] == 'cuda:1' and records['memory'] == (0.25, 1) and built.device == 'cuda:1'
    assert records['model'][1] == {'dtype': 'labelled-float32', 'attn_implementation': 'eager',
                                   'trust_remote_code': False}
    assert records['processor'][1]['trust_remote_code'] is False
    if family == 'embedding_gemma2':
        assert 'padding_side' not in records['processor'][1]
        assert processor.image_processor.max_soft_tokens == settings.max_soft_tokens
    else:
        assert records['processor'][1]['padding_side'] == 'right'
        assert processor.image_processor.size == {'shortest_edge': settings.min_pixels,
                                                  'longest_edge': settings.max_pixels}


def test_the_loader_follows_a_second_dtype_and_other_limits(project_root, model_test_profile, tmp_path, monkeypatch):
    settings = make_settings(project_root, model_test_profile, family='qwen3_vl_embedding')[0]
    settings = replace(settings, dtype='bfloat16', attn_implementation='sdpa', min_pixels=111, max_pixels=2222, gpu=0,
                       vram_budget_gb=5)
    records, processor = {}, SimpleNamespace(image_processor=SimpleNamespace())
    fake_libraries(monkeypatch, records, processor)
    load_engine(settings, weights_directory(tmp_path, settings))
    assert records['model'][1]['dtype'] == 'labelled-bfloat16' and records['model'][1]['attn_implementation'] == 'sdpa'
    assert records['memory'] == (0.5, 0) and processor.image_processor.size == {'shortest_edge': 111,
                                                                               'longest_edge': 2222}


def test_a_processor_that_ignores_the_image_limits_is_refused(project_root, model_test_profile, tmp_path, monkeypatch):
    class Stubborn:
        max_soft_tokens = 999

        def __setattr__(self, name, value):
            pass

    settings = make_settings(project_root, model_test_profile)[0]
    fake_libraries(monkeypatch, {}, SimpleNamespace(image_processor=Stubborn()))
    with pytest.raises(ServerStartError, match='did not take'):
        load_engine(settings, weights_directory(tmp_path, settings))


def test_the_loader_refuses_a_gpu_that_is_missing_or_too_small(project_root, model_test_profile, tmp_path, monkeypatch):
    settings = make_settings(project_root, model_test_profile)[0]
    directory = weights_directory(tmp_path, settings)
    fake_libraries(monkeypatch, {}, SimpleNamespace(image_processor=SimpleNamespace()), gpus=1)
    with pytest.raises(ServerStartError, match='GPU 1'):
        load_engine(replace(settings, gpu=1), directory)
    fake_libraries(monkeypatch, {}, SimpleNamespace(image_processor=SimpleNamespace()), total=2 * 1024 ** 3)
    with pytest.raises(ServerStartError, match='vram_budget_gb'):
        load_engine(replace(settings, vram_budget_gb=3), directory)
    fake_libraries(monkeypatch, {}, SimpleNamespace(image_processor=SimpleNamespace()), gpus=0)
    with pytest.raises(ServerStartError, match='not available'):
        load_engine(settings, directory)


def test_the_loader_refuses_weights_of_another_family_or_with_remote_code(project_root, model_test_profile, tmp_path,
                                                                        monkeypatch):
    settings = make_settings(project_root, model_test_profile)[0]
    fake_libraries(monkeypatch, {}, SimpleNamespace(image_processor=SimpleNamespace()))
    with pytest.raises(ServerStartError):
        load_engine(settings, weights_directory(tmp_path, settings, auto_map={'AutoModel': 'x.Y'}))
    other = make_settings(project_root, model_test_profile, family='qwen3_vl_embedding')[0]
    with pytest.raises(ServerStartError):
        load_engine(other, weights_directory(tmp_path, settings))  # EmbeddingGemma 2 weights, Qwen family
