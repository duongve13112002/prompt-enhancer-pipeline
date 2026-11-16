"""The model families the embedding server can load: structural facts only, no torch import (T-018).

Each fact is a property of the family's published weights and card, read from its `config.json` and model card
(docs/stages/K1-embed.md): which `model_type` the family has, whether a system instruction exists, which dtype the card
forbids and where the native vector size sits in the weights' own configuration.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    name: str
    model_type: str  # protocol-bound: `model_type` of the family's config.json (checked against the local file)
    accepts_instruction: bool  # whether the family's input format has a system instruction (Qwen: yes; EG2: no)
    forbidden_dtypes: tuple  # EmbeddingGemma 2 card: "Do not use `float16`" (N104)
    native_dimension_path: tuple  # keys in config.json that hold the native vector size
    pooling: str  # `mean` (EG2 card: Mean Pooling) or `last_token` (Qwen 1_Pooling/config.json: lasttoken)


FAMILIES = {
    'embedding_gemma2': Family('embedding_gemma2', 'embedding_gemma2', False, ('float16',),
                               ('text_config', 'embedding_dim'), 'mean'),
    'qwen3_vl_embedding': Family('qwen3_vl_embedding', 'qwen3_vl', True, (), ('text_config', 'hidden_size'),
                                 'last_token'),
}


def check_weights_config(family, config, dimension, dimension_mode):
    """Check a loaded `config.json` against the profile before any weight is read; return the native dimension.

    The family must match the weights' own `model_type`, the file must declare no `auto_map` (remote code is never
    run, so a repository that gained one is refused here and again by `trust_remote_code=False`), and the profile's
    vector length must be reachable: equal to the native size in `native` mode, at most it in `client_prefix_l2`.
    """
    if not isinstance(config, dict) or config.get('model_type') != family.model_type:
        raise ValueError(f'config.json model_type is not {family.model_type!r} (the family {family.name})')
    if 'auto_map' in config:
        raise ValueError('config.json declares auto_map; the server never runs remote code')
    native = config
    for key in family.native_dimension_path:
        native = native.get(key) if isinstance(native, dict) else None
    if type(native) is not int or native <= 0:
        raise ValueError(f'config.json holds no native dimension at {".".join(family.native_dimension_path)}')
    if dimension_mode == 'native' and dimension != native:
        raise ValueError(f'embedding.dimension {dimension} differs from the native dimension {native}')
    if dimension_mode == 'client_prefix_l2' and not 0 < dimension <= native:
        raise ValueError(f'embedding.dimension {dimension} is not within 1..{native}')
    return native


def output_fingerprint(server):
    """The settings of a validated `server` block that change the vectors the server returns (R-017).

    K1 folds this into its embedding-space identity (`k1_embed.embedder_settings`), so a checkpoint made under other
    values is never reused. Left out on purpose, because they change no vector's meaning: host, port, routes,
    `idle_exit_s`, `max_request_bytes`, `max_image_bytes`, `max_pending_requests`, `max_connections`,
    `max_discard_bytes`, `read_timeout_s` (transport and admission), the GPU and its memory cap (placement) and `forward_batch` (it only moves bf16 rounding, and tuning it
    must not invalidate a corpus). `max_decode_pixels` is already in K1's schema revision.
    """
    family = FAMILIES[server['family']]
    return {'family': family.name, 'model_type': family.model_type, 'pooling': family.pooling,
            'dtype': server['dtype'], 'attn_implementation': server['attn_implementation'],
            'max_length': server['max_length'], 'image': dict(server['image']),
            'default_instruction': server['default_instruction']}
