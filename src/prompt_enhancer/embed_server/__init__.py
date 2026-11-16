"""Project-owned embedding server: OpenAI-compatible `/v1/embeddings` on `transformers` (T-018).

Everything importable on the CPU `.venv` (protocol, settings, manifest, families) stays free of torch and
transformers; only `models.py` imports them, inside functions.
"""
