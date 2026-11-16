"""Pooling arithmetic of the embedding server, in plain Python so the CPU suite tests it (T-018).

The device does only the part that needs it (masked sums, one row per sequence); this module does the part that
decides the numbers: the mean, the position of the last real token and the L2 normalisation, exactly as the model
cards state them (EmbeddingGemma 2: "Mean Pooling" then Normalize; Qwen3-VL-Embedding: `lasttoken` then Normalize).
"""

import math


def mean_from_sums(sums, count):
    """Mean pooling from the masked sum of token vectors and the number of real tokens."""
    if type(count) is not int or count <= 0:
        raise ValueError('mean pooling needs at least one real token')
    return [value / count for value in sums]


def last_token_index(mask_row):
    """Position of the last real token of one sequence (right- or left-padded alike)."""
    positions = [index for index, flag in enumerate(mask_row) if flag]
    if not positions:
        raise ValueError('the sequence holds no real token')
    return positions[-1]


def l2_normalize(values):
    """Unit-length copy of a vector. Scaling by the largest magnitude first keeps the norm finite and non-zero."""
    values = [float(value) for value in values]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError('the vector is empty or not finite')
    largest = max(abs(value) for value in values)
    if largest == 0:
        raise ValueError('a zero vector cannot be normalised')
    scaled = [value / largest for value in values]
    norm = math.hypot(*scaled)
    return [value / norm for value in scaled]
