"""S4: verbatim admin system bytes and a scoped, data-only Enhancement Brief."""

import base64
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path

from jsonschema import ValidationError

from .assembly import CHANNEL, attached_note
from .config import ConfigurationError, resolve_writer_format, validate_writer_reads
from .model_client import ModelCallError, safe_model_code
from .model_json import check_json_depth
from .model_schemas import SchemaRegistry
from .profile_prompt import ProfileError, load_prompt, placeholders, sha256_tag
from .profile_validators import validate_artifact


RESULT_SCHEMA = 'writer_result.v0'  # artifact-bound stage contract
ROLE = 'writer'  # protocol-bound model role


@dataclass(frozen=True)
class PreparedWrite:
    registry: SchemaRegistry
    role: dict
    system: bytes
    text: str
    images: tuple
    addendum: bytes | None


def _substitute(template, values):
    """Replace original placeholders once; never rescan inserted user bytes."""
    placeholders(template)
    parts, offset = [], 0
    while True:
        start = template.find(b'{{', offset)
        if start < 0:
            parts.append(template[offset:])
            return b''.join(parts)
        end = template.index(b'}}', start)
        key = template[start + 2:end].decode('utf-8')
        if key not in values:
            raise ProfileError('PROFILE_TEMPLATE_INVALID', 'unresolved template variable')
        parts.extend((template[offset:start], values[key]))
        offset = end + 2


def _data_json(data):
    """One scoped fact per physical line, still inside one JSON data object."""
    def encode(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    fields = []
    for key, value in data.items():
        if key == 'knowledge':
            facts = ',\n'.join(encode(fact) for fact in value['facts'])
            attached = value.get('attachments')  # absent unless images are attached, so other briefs keep their bytes
            tail = '' if not attached else ',"attachments":[\n' + ',\n'.join(encode(item) for item in attached) + '\n]'
            fields.append('"knowledge":{"facts":[\n' + facts + '\n],"images":' + encode(value['images']) + tail + '}')
        else:
            fields.append(encode(key) + ':' + encode(value))
    return '{' + ','.join(fields) + '}'


def render_brief(intake_result, plan_result, assembly_result, template, *, repair=None):
    payload, profile = intake_result.payload, intake_result.profile
    config, intent = payload['effective_config'], plan_result['intent_ir']
    manifest = profile.manifest
    data = {'task': payload['task'], 'prompt': payload['prompt'], 'intent': deepcopy(intent),
            'creativity': deepcopy(config['creativity']), 'output_language': manifest['output_language'],
            'quote_semantics': manifest['quote_semantics']}
    if repair is not None:
        if not isinstance(repair, dict) or set(repair) != {'draft', 'failures'} or not isinstance(repair['failures'], list):
            raise ValueError('repair requires a draft and failures as data')
        data['repair'] = deepcopy(repair)
    if payload['task'] == 'edit':
        data['edit_output_semantics'] = manifest['edit_output_semantics']
    if config['rag']['enabled'] and assembly_result is not None and assembly_result['pack'] is not None:
        pack = assembly_result['pack']
        data['knowledge'] = {'facts': deepcopy(pack['facts']),
                             'images': [deepcopy(item) for item in pack['images'] if item['channel'] != CHANNEL]}
        attached = [item for item in pack['images'] if item['channel'] == CHANNEL]
        if attached:
            # Same selection and order as the S6 attachments (envelope.py), so a position names the delivered image.
            if payload['task'] != 'edit' or config['edit']['attach_kb_reference_images'] is not True:
                raise ValueError('S4: knowledge images are attached only for edit with attach_kb_reference_images')
            # Decision 177: a profile whose renderer accepts no reference images is refused with this configuration by
            # `profiles._view` before any request reaches here. This guard stays as defence; it is not a degraded mode.
            if not manifest['renderer']['accepts_reference_images']:
                raise ValueError('S4: the profile renderer accepts no reference images (renderer.accepts_reference_images '
                                 'is false) but knowledge images are attached (edit.attach_kb_reference_images is true)')
            # The note is decoded and re-checked against the kept facts, so it reaches the writer only as data (R-026).
            data['knowledge']['attachments'] = [
                {'position': position, 'image_id': item['image_id'], 'bound_entity': item['bound_entity'],
                 'facets': list(item['facets']), 'note': attached_note(item, pack['facts'])}
                for position, item in enumerate(attached, start=1)]
    if placeholders(template) != ['data']:
        raise ConfigurationError('vlm.roles.writer.prompt_id: brief requires exactly the data variable')
    rendered = _substitute(template, {'data': _data_json(data).encode('utf-8')})
    if profile.user_template is not None:
        values = {name: text.encode('utf-8') for name, text in profile.template_values.items()}
        values[profile.input_variable] = rendered
        rendered = _substitute(profile.user_template, values)
    return rendered.decode('utf-8')


def prepare_write(intake_result, plan_result, assembly_result, *, project_root, repair=None):
    """Validate and render the exact writer message before any writer capability probe."""
    payload, profile = intake_result.payload, intake_result.profile
    config, root = payload['effective_config'], Path(project_root).resolve()
    validate_writer_reads(config, project_root=root)
    if payload['outcome'] not in ('ready', 'degraded') or plan_result['state'] not in ('ready', 'degraded') \
            or plan_result['intent_ir'] is None:
        raise ValueError('S4 requires a usable intake and Intent IR')
    if plan_result['intent_ir']['task'] != payload['task'] or (plan_result['intent_ir']['needs_clarification']
                                                             and plan_result['state'] != 'degraded'):
        raise ValueError('S4 requires a matching task without pending clarification')
    if not config['rag']['enabled'] and assembly_result is not None:
        raise ValueError('S4: retrieval off has no assembly result')
    registry = SchemaRegistry(root / 'contracts' / 'schemas')
    registry.validate('plan_result.v0', plan_result)
    if config['rag']['enabled']:
        if assembly_result is None:
            raise ValueError('S4 requires the assembly result with retrieval on')
        registry.validate('assembly_result.v0', assembly_result)
        pack = assembly_result['pack']
        if pack is not None and (pack['request_id'] != payload['request_id']
                                 or pack['snapshot_id'] != payload['retrieval']['snapshot_id']):
            raise ValueError('S4: pack differs from the request or pinned snapshot')
    artifacts = profile.validator_artifacts
    if tuple(item['id'] for item in artifacts) != tuple(profile.validator_checks):
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'unresolved profile checks')
    for artifact in artifacts:
        validate_artifact(artifact, project_root=root)
    if profile.addendum is not None:
        if not config['writer']['runtime_addendum']:
            raise ConfigurationError('writer.runtime_addendum: a required addendum cannot be disabled')
    addendum = None if profile.addendum is None else profile.addendum['text']
    resolved_role = resolve_writer_format(config['vlm']['roles'][ROLE], profile.manifest, project_root=root)
    selected_backend = config['vlm']['backends'][resolved_role['backend']]
    if selected_backend['provider'] != 'openai_compatible' and resolved_role['output']['format'] != 'none':
        raise ConfigurationError('vlm.roles.writer.output: cloud structured output mapping is not verified')
    prompt_id = config['vlm']['roles'][ROLE]['prompt_id']
    confirmed = getattr(profile, 'writer_brief', None)
    if confirmed is not None and confirmed[0] != prompt_id:
        raise ProfileError('PROFILE_NOT_CHECKED', 'the writer brief ID differs from the checked profile')
    template = confirmed[1] if confirmed is not None else load_prompt(root, prompt_id)
    text = render_brief(intake_result, plan_result, assembly_result, template, repair=repair)
    system = profile.system_message
    images = ()
    if payload['task'] == 'edit':
        if config['vlm']['roles'][ROLE]['image'] == 'none':
            raise ConfigurationError('vlm.roles.writer.image: edit requires the source image')
        images = (('source_image', base64.b64decode(payload['source_image']['data'], validate=True)),)
    return PreparedWrite(registry, resolved_role, system, text, images, addendum)


async def write(intake_result, plan_result, assembly_result, client, *, project_root, repair=None,
                prepared=None, binding_error=None):
    """One logical writer call with the same typed failure boundary for binding and generation."""
    if binding_error is None and getattr(client, 'role_name', None) != ROLE:
        raise ValueError('S4 requires a writer-bound client')
    if prepared is None:
        prepared = prepare_write(intake_result, plan_result, assembly_result, project_root=project_root, repair=repair)
    system, text, addendum = prepared.system, prepared.text, prepared.addendum
    try:
        if binding_error is not None:
            raise binding_error
        options = {} if addendum is None else {'addendum': addendum}
        value, record = await client.call(system, text, manifest=intake_result.profile.manifest,
                                          images=prepared.images, **options)
    except ModelCallError as error:
        value, record = None, {'status': safe_model_code(error.code), 'warnings': []}
    status = record['status']
    okay = status == 'OK' and isinstance(value, (str, dict, list)) and value != ''
    if okay:
        try:
            check_json_depth(value)
            schema_id = prepared.role['output']['schema_id']
            if schema_id != 'none':
                prepared.registry.validate(schema_id, value)
        except (ValueError, ValidationError, RecursionError):
            status, okay = 'SCHEMA_INVALID', False
    warnings = [entry[0] if isinstance(entry, (tuple, list)) else entry for entry in record.get('warnings', [])]
    result = {'state': 'draft' if okay else 'failed', 'output': value if okay else None,
              'calls': [{'status': status, 'record_json': json.dumps(record, ensure_ascii=False, allow_nan=False)}],
              'warnings': warnings, 'error': None if okay else {'code': status if status != 'OK' else 'OUTPUT_INVALID',
                                                               'message': 'S4: no complete writer draft'},
              'brief_hash': sha256_tag(text.encode('utf-8')), 'system_prompt_hash': sha256_tag(system),
              'addendum_hash': None if addendum is None else sha256_tag(addendum)}
    prepared.registry.validate(RESULT_SCHEMA, result)
    return result
