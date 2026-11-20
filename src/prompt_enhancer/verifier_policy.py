"""Pinned verifier routes and calibration evidence without a model call."""
from copy import deepcopy
import json
from pathlib import Path

from jsonschema import ValidationError

from .config import (
    ConfigurationError, _configured_path, _sentinel, resolve_writer_format,
    validate_validation_reads, validate_writer_reads,
)
from .model_client import semantic_role_settings
from .model_json import strict_json
from .model_schemas import cached_registry
from .profile_prompt import load_prompt, sha256_tag


def _tagged(value):
    return sha256_tag(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                 allow_nan=False).encode('utf-8'))


def reachable_models(config, role_name):
    """Include every configured fallback, not just a distinct role/backend alias."""
    role = config['vlm']['roles'][role_name]
    backends = config['vlm']['backends']
    selected = backends[role['backend']]
    names = [role['backend']]
    if selected['provider'] != 'openai_compatible' and (
            selected['on_budget_exhausted'] == 'fallback_to_default_backend'
            or isinstance(role['image'], dict) and not selected['allow_images']):
        names.append(config['vlm']['default_backend'])
    found = []
    for name in dict.fromkeys(names):
        backend = backends[name]
        identity = {'provider': backend['provider'], 'model': deepcopy(backend['model'])}
        if any(not isinstance(identity['model'][key], str) or identity['model'][key] in ('none', '')
               for key in ('id', 'revision')):
            raise ConfigurationError(f'vlm.backends.{name}.model: verifier eligibility needs pinned identity')
        if identity not in found:
            found.append(identity)
    return found


def _read_local(root, ref, key):
    if not isinstance(ref, str) or Path(ref).is_absolute():
        raise ConfigurationError(f'{key}: expected a project-relative evidence file')
    _sentinel(ref, root, key, {})
    _configured_path(ref, root, 'configuration', key)
    return (root / ref).resolve().read_bytes()


def bind_verifier(config, profile, *, project_root, require_calibration):
    """Require pinned routes and report whether their calibration evidence matches."""
    root = Path(project_root).resolve()
    validate_validation_reads(config, project_root=root, require_verifier=True,
                              require_calibration=require_calibration)
    validate_writer_reads(config, project_root=root)
    writer_models, verifier_models = reachable_models(config, 'writer'), reachable_models(config, 'verifier')
    calibration_hash = None
    try:
        raw = _read_local(root, config['validation']['verifier']['calibration_ref'],
                          'validation.verifier.calibration_ref')
        value = strict_json(raw)
        cached_registry(root / 'contracts/schemas').validate('verifier_calibration.v0', value)
        roles = config['vlm']['roles']
        writer_config = deepcopy(config)
        writer_config['vlm']['roles']['writer'] = resolve_writer_format(roles['writer'], profile.manifest, project_root=root,
                                                                     profile_schema_id=getattr(profile, 'output_schema_id', None))
        brief = getattr(profile, 'writer_brief', None)
        writer_prompt = brief[1] if brief else load_prompt(root, roles['writer']['prompt_id'])
        expected = {
            'writer_models': writer_models, 'verifier_models': verifier_models,
            'writer_settings_hash': _tagged(semantic_role_settings(writer_config, 'writer')),
            'verifier_settings_hash': _tagged(semantic_role_settings(config, 'verifier')),
            'writer_prompt_hash': sha256_tag(writer_prompt),
            'verifier_prompt_hash': sha256_tag(load_prompt(root, roles['verifier']['prompt_id'])),
            'system_prompt_hash': sha256_tag(profile.system_message),
            'addendum_hash': None if profile.addendum is None else sha256_tag(profile.addendum['text']),
            'ledger_schema_hash': sha256_tag((root / 'contracts/schemas/constraint_ledger.v0.schema.json').read_bytes()),
        }
        if any(value[key] != expected[key] for key in expected):
            raise ValueError('calibration identity or revision mismatch')
        report = _read_local(root, value['evidence_ref'], 'calibration.evidence_ref')
        if sha256_tag(report) != value['evidence_hash']:
            raise ValueError('calibration evidence changed')
        calibration_hash = sha256_tag(raw)
    except (OSError, ValueError, KeyError, TypeError, ValidationError, ConfigurationError) as error:
        if require_calibration:
            raise ConfigurationError('validation.verifier.calibration_ref: unavailable or mismatched evidence') from error
    return {'writer_models': writer_models, 'verifier_models': verifier_models, 'calibration_hash': calibration_hash}


def require_calibrated_verifier(config, profile, *, project_root):
    return bind_verifier(config, profile, project_root=project_root, require_calibration=True)


def provenance_matches(record, models):
    """A successful response must identify one of the pinned effective routes."""
    return isinstance(record, dict) and any(record.get('provider') == item['provider'] and record.get('model_id') == item['model']['id']
               and record.get('model_revision') == item['model']['revision'] for item in models)
