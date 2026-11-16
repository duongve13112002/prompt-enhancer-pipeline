"""The embedding server's settings: one frozen record resolved from a validated profile (T-018, R-015).

Nothing here is a default: every value comes from the profile through `validate_embed_server_reads`, so a missing
field or an unresolved sentinel is refused before the server binds a port or reads a weight.
"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config import NONE, embed_server_address, load_config, validate_embed_server_reads
from ..model_transport import endpoint_url
from .families import FAMILIES, Family

COMMAND = 'embed serve'  # the loader command whose read set the server checks


@dataclass(frozen=True)
class ServerSettings:
    backend_name: str
    model_id: str
    revision: str
    family: Family
    dtype: str
    attn_implementation: str
    forward_batch: int
    max_length: int
    min_pixels: int | None
    max_pixels: int | None
    max_soft_tokens: int | None
    default_instruction: str | None
    idle_exit_s: int
    max_request_bytes: int
    max_image_bytes: int
    max_pending_requests: int
    max_connections: int
    max_discard_bytes: int
    read_timeout_s: float
    max_decode_pixels: int
    host: str
    port: int
    embeddings_path: str
    health_path: str
    gpu: int
    vram_budget_gb: float
    batch_size: int
    dimension: int
    dimension_mode: str
    project_root: Path


def _optional(value):
    return None if value == NONE else value


def server_settings(config, project_root=None, path='configuration'):
    """Validate `config` for `embed serve` and resolve it; raises ConfigurationError on any refusal."""
    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    validate_embed_server_reads(config, path, {}, root)
    embedding = config['embedding']
    name = embedding['backend']
    backend = config['vlm']['backends'][name]
    server, image = backend['server'], backend['server']['image']
    base = backend['base_urls'][0]
    host, port = embed_server_address(base)
    instruction = server['default_instruction']
    return ServerSettings(
        backend_name=name, model_id=backend['model']['id'], revision=backend['model']['revision'],
        family=FAMILIES[server['family']], dtype=server['dtype'], attn_implementation=server['attn_implementation'],
        forward_batch=server['forward_batch'], max_length=server['max_length'], min_pixels=_optional(image['min_pixels']),
        max_pixels=_optional(image['max_pixels']), max_soft_tokens=_optional(image['max_soft_tokens']),
        default_instruction=_optional(instruction), idle_exit_s=server['idle_exit_s'],
        max_request_bytes=server['max_request_bytes'], max_image_bytes=server['max_image_bytes'],
        max_pending_requests=server['max_pending_requests'], max_connections=server['max_connections'],
        max_discard_bytes=server['max_discard_bytes'], read_timeout_s=server['read_timeout_s'],
        max_decode_pixels=backend['image']['max_decode_pixels'], host=host, port=port,
        embeddings_path=urlsplit(endpoint_url(base, backend['endpoints']['embeddings'])).path,
        health_path=urlsplit(endpoint_url(base, backend['endpoints']['health'])).path,
        gpu=config['runtime']['gpus'][0], vram_budget_gb=config['runtime']['vram_budget_gb'],
        batch_size=embedding['batch_size'], dimension=embedding['dimension'],
        dimension_mode=embedding['dimension_mode'], project_root=root)


def load_server_settings(config_path, overlay=None, project_root=None):
    """Load the profile through the loader (command `embed serve`) and resolve the server settings."""
    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    config = load_config(config_path, COMMAND, overlay=overlay, project_root=root)
    return server_settings(config, root, str(config_path))
