"""S5: structural checks, eligible risk-gated verification and bounded repairs."""
from copy import deepcopy
import json
from pathlib import Path
import random

from jsonschema import ValidationError

from .config import ConfigurationError, validate_validation_reads, validate_writer_reads
from .model_client import ModelCallError, safe_model_code
from .model_json import check_json_depth, strict_json
from .model_schemas import cached_registry, with_output_schema
from .profile_prompt import ProfileError, load_prompt, sha256_tag
from .profile_validators import check_output, decode_output, output_format, validate_artifact
from .verifier_policy import bind_verifier, provenance_matches, require_calibrated_verifier
from .writer import _substitute, write


RESULT_SCHEMA = 'validation_result.v1'  # artifact-bound stage contract


def _texts(value):
    """Decoded values only: an object's key is not rendered user text."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return [json.dumps(value, ensure_ascii=False, allow_nan=False)]


def _contains(output, text):
    return any(text in leaf for leaf in _texts(output))


def _decoded_output(output, profile):
    try:
        return decode_output(output, output_format(profile.manifest, profile.validator_artifacts))
    except ValueError:
        return output  # The profile contract records malformed JSON as a structural failure.


def _check(output, profile, root, validators=None):
    return check_output(output, profile.validator_artifacts if validators is None else validators,
                        project_root=root, output_format=output_format(profile.manifest, profile.validator_artifacts))


def validate_verifier_reply(value, ids, output, *, project_root=None):
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        cached_registry(root / 'contracts/schemas').validate('verifier_result.v0', value)
        entries = value['results']
        found = [entry['constraint_id'] for entry in entries]
        if len(found) != len(ids) or len(set(found)) != len(found) or set(found) != set(ids):
            raise ValueError('verifier coverage differs from requested obligations')
        for entry in entries:
            span = entry['evidence_span']
            if entry['verdict'] == 'pass' and (span is None or not span or span.isspace()):
                raise ValueError('passing verdict needs output evidence')
            if span is not None and (not span or not _contains(output, span)):
                raise ValueError('verifier evidence does not quote decoded output')
        return entries
    except (ValidationError, KeyError, TypeError, RecursionError) as error:
        raise ValueError('invalid verifier reply') from error


def _new_id(kind, used):
    index = 0
    while f'@s5:{kind}:{index}' in used:
        index += 1
    result = f'@s5:{kind}:{index}'
    used.add(result)
    return result


def _obligations(intent, profile, pack):
    entries = deepcopy(intent['constraint_ledger']['constraints'])
    used = {entry['id'] for entry in entries}
    if len(used) != len(entries):
        raise ValueError('S5: duplicate ledger constraint id')
    def add(kind, text, span='', required_text=None):
        item = {'id': _new_id(kind, used), 'type': kind, 'text': text, 'span': span, 'bound_entity': None}
        if required_text is not None:
            item['required_text'] = required_text
        entries.append(item)
    for literal in intent['literal_texts']:
        add('literal_text', literal['text'], literal['span'], literal['text'])
    for token in intent['passthrough_tokens']:
        add('passthrough_token', token['token'], token['span'], token['token'])
    for link in intent['concept_links']:
        add('concept_name', link['mention'], link['mention'], link['mention'])
    identity_facts = bool(pack and any(
        fact['concept_id'] == link['concept_id'] and fact['bound_entity'] == link['entity_id']
        and link['role'] == 'subject_identity' for fact in pack['facts'] for link in intent['concept_links']))
    types = {entry['type'] for entry in entries}
    if identity_facts and 'identity_grounding' not in types:
        add('identity_grounding', 'Every identity attribute is grounded in user evidence or granted facts for its entity.')
    if intent['task'] == 'edit':
        for kind, text in (
                ('edit_coverage', 'Every requested delta operation is present.'),
                ('edit_scope', 'No operation exceeds the delta; preservation constraints remain intact.'),
                ('edit_strength', 'Each requested change is specific enough to change the source image.')):
            if kind not in types:
                add(kind, text)
    if profile.has_known_conflict:
        add('profile_conflict', 'Follow the administrator-confirmed conflict resolutions and effective addendum.')
    return entries, identity_facts, used


def _verdict(identifier, verdict, *, method='structural', evidence=None, note=''):
    return {'id': identifier, 'verdict': verdict, 'method': method, 'evidence_span': evidence, 'note': note}


def _contract(output, profile, root, registry):
    if not isinstance(output, (str, dict, list)) or output == '':
        return False
    try:
        # Also reject nonfinite or otherwise non-JSON-safe values from injected clients.
        strict_json(json.dumps(output, ensure_ascii=False, allow_nan=False))
        schema_id = profile.manifest['output_schema_id']
        if schema_id != 'none':
            registry.validate(schema_id, output)
        return not _check(output, profile, root)
    except (ValueError, TypeError, ValidationError, RecursionError):
        return False


def _structural(output, profile, entries, ids, root, registry, *, complete):
    verdicts = [_verdict(ids[0], 'pass' if complete else 'fail', note='complete client response'),
                _verdict(ids[1], 'pass' if complete and _contract(output, profile, root, registry) else 'fail',
                         note='profile structural contract')]
    pending = []
    decoded = _decoded_output(output, profile) if complete else None
    failed_checks = set(_check(output, profile, root)) if complete else set()
    for artifact in profile.validator_artifacts:
        metadata = {key: artifact[key] for key in ('id', 'operator', 'parameters')}
        verdicts.append(_verdict(ids[2][artifact['id']],
                                  'unverified' if not complete else 'fail' if artifact['id'] in failed_checks else 'pass',
                                  note=json.dumps(metadata, ensure_ascii=False, allow_nan=False)))
    for entry in entries:
        identifier, kind = entry['id'], entry['type']
        required = entry.get('required_text')
        if not complete:
            verdicts.append(_verdict(identifier, 'unverified'))
        elif required is not None and not _contains(decoded, required):
            verdicts.append(_verdict(identifier, 'fail', note='verbatim value missing'))
        elif required is not None and (kind == 'passthrough_token' or kind != 'concept_name' and any(
                artifact['operator'] == 'exact_text' and artifact['parameters']['value'] == required
                and not _check(output, profile, root, [artifact]) for artifact in profile.validator_artifacts)):
            verdicts.append(_verdict(identifier, 'pass', evidence=required))
        else:
            verdicts.append(_verdict(identifier, 'unverified', method='verifier'))
            pending.append(entry)
    return verdicts, pending


def _gated(config, intent, profile, identity_facts, pending):
    rules = config['validation']['verifier']
    triggers = {'task_is_edit': intent['task'] == 'edit', 'identity_facts_supplied': identity_facts,
                'profile_has_known_conflict': profile.has_known_conflict, 'ledger_entry_needs_meaning': bool(pending)}
    return any(triggers[name] for name in rules['gating_rules']) or any(
        {'edit': intent['task'] == 'edit', 'identity_facts': identity_facts}[name] for name in rules['inline_for'])


def _fallback_template(artifacts):
    templates = [item['fallback'] for item in artifacts if 'fallback' in item]
    if templates and any(value != templates[0] for value in templates[1:]):
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'conflicting minimal fallback templates')
    return None if not templates else templates[0]['template']


def _minimal(template, prompt):
    count = 0
    def substitute(value):
        nonlocal count
        if isinstance(value, str):
            count += value.count('{{original_prompt}}')
            return _substitute(value.encode('utf-8'), {'original_prompt': prompt.encode('utf-8')}).decode('utf-8')
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value
    output = substitute(template)
    if count == 0:
        raise ValueError('minimal fallback has no original prompt insertion')
    return output


def _record_call(record):
    return {'status': record['status'], 'record_json': json.dumps(record, ensure_ascii=False, allow_nan=False)}


async def validate(intake_result, plan_result, assembly_result, writer_result, writer_client, verifier_client=None,
                   *, project_root):
    """No ownership/transport retries: one verifier per eligible draft, at most configured writer repairs."""
    root = Path(project_root).resolve()
    config, profile = intake_result.payload['effective_config'], intake_result.profile
    policy = validate_validation_reads(config, project_root=root)
    validate_writer_reads(config, project_root=root)
    may_repair = writer_result['state'] == 'draft' or (
        writer_result['calls'] and writer_result['calls'][-1]['status'] == 'SCHEMA_INVALID')
    if (writer_client is not None or may_repair) and getattr(writer_client, 'role_name', None) != 'writer':
        raise ValueError('S5 requires a writer-bound repair client')
    # T017-FIX3: the S5 contract check of every draft and repair uses the admin bundle schema the writer sent.
    registry = with_output_schema(cached_registry(root / 'contracts/schemas'), getattr(profile, 'output_schema', None))
    registry.validate('plan_result.v1', plan_result)
    try:
        check_json_depth(writer_result['output'])
    except ValueError:
        writer_result = {**writer_result, 'output': None, 'state': 'failed',
                         'calls': [*writer_result['calls'], _record_call({'status': 'SCHEMA_INVALID'})],
                         'warnings': [*writer_result['warnings'], 'SCHEMA_INVALID'],
                         'error': {'code': 'SCHEMA_INVALID', 'message': 'S4: JSON nesting exceeds the supported depth'}}
    registry.validate('writer_result.v1', writer_result)
    intent = plan_result['intent_ir']
    if intent is not None and (plan_result['state'] not in ('ready', 'degraded')
                              or intent['task'] != intake_result.payload['task']
                              or intent['needs_clarification'] and plan_result['state'] != 'degraded'):
        raise ValueError('S5 requires a matching Intent IR without pending clarification')
    if not config['rag']['enabled'] and assembly_result is not None:
        raise ValueError('S5: retrieval off has no assembly result')
    pack = None
    if config['rag']['enabled'] and (assembly_result is not None or intent is not None):
        if assembly_result is None:
            raise ValueError('S5 requires S3 with retrieval on')
        registry.validate('assembly_result.v0', assembly_result)
        pack = assembly_result['pack']
        if pack and (pack['request_id'] != intake_result.payload['request_id']
                     or pack['snapshot_id'] != intake_result.payload['retrieval']['snapshot_id']):
            raise ValueError('S5: knowledge pack differs from the pinned request')
    if tuple(item['id'] for item in profile.validator_artifacts) != tuple(profile.validator_checks):
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'unresolved profile checks')
    for artifact in profile.validator_artifacts:
        validate_artifact(artifact, project_root=root)
    template = _fallback_template(profile.validator_artifacts)
    if template is not None and any(item['id'] not in profile.validator_hashes
                                    for item in profile.validator_artifacts if 'fallback' in item):
        raise ProfileError('PROFILE_VALIDATOR_INVALID', 'minimal fallback is not hash-bound in the checked profile')
    entries, identity_facts, used = ([], False, set()) if intent is None else _obligations(intent, profile, pack)
    ids = (_new_id('complete', used), _new_id('contract', used),
           {item['id']: _new_id('validator:' + item['id'], used) for item in profile.validator_artifacts})
    calls, warnings, attempts, fallbacks = deepcopy(writer_result['calls']), list(writer_result['warnings']), [], []
    if intent is None:
        warnings.append('INTENT_UNAVAILABLE')
    repairs, current = 0, deepcopy(writer_result)
    degraded = intake_result.payload['outcome'] != 'ready' or plan_result['state'] != 'ready' or (
        assembly_result is not None and assembly_result['retrieval_state'] == 'failed')
    bound = None
    monitoring = False

    def finish(status, output, verdicts, fallback=None):
        result = {'status': status, 'output': output, 'verdicts': verdicts, 'repair_count': repairs,
                  'fallback': fallback, 'fallback_attempts': fallbacks, 'calls': calls,
                  'warnings': list(dict.fromkeys(warnings)), 'attempts': attempts, 'monitoring_selected': monitoring,
                  'error': {'code': 'VALIDATION_FAILED', 'message': 'S5: no permitted output'} if status == 'failed' else None}
        registry.validate(RESULT_SCHEMA, result)
        return result

    while True:
        output = current['output']
        last = current['calls'][-1] if current['calls'] else {'status': 'MODEL_CALL_FAILED'}
        expected_addendum = None if profile.addendum is None else sha256_tag(profile.addendum['text'])
        complete = (intent is not None and current['state'] == 'draft' and last['status'] == 'OK' and current['error'] is None
                    and current['system_prompt_hash'] == sha256_tag(profile.system_message)
                    and current['addendum_hash'] == expected_addendum and output is not None)
        verdicts, pending = _structural(output, profile, entries, ids, root, registry, complete=complete)
        unavailable = False
        structural_failure = any(item['verdict'] == 'fail' for item in verdicts)
        if not structural_failure and pending:
            if not _gated(config, intent, profile, identity_facts, pending):
                unavailable = True
            else:
                try:
                    if bound is None:
                        bound = bind_verifier(config, profile, project_root=root,
                                              require_calibration=policy['verifier']['require_calibration'])
                    provenance = strict_json(last['record_json'])
                    if not provenance_matches(provenance, bound['writer_models']):
                        raise ValueError('writer provenance is outside pinned routes')
                    if verifier_client is None:
                        verifier_client = await writer_client.for_role('verifier')
                    if getattr(verifier_client, 'role_name', None) != 'verifier':
                        raise ValueError('S5 requires a verifier-bound client')
                    if bound['calibration_hash'] is None:
                        warnings.append('VERIFIER_UNCALIBRATED')
                    decoded = _decoded_output(output, profile)
                    data = {'obligations': pending, 'candidate': decoded, 'prompt': intake_result.payload['prompt'],
                            'facts': [] if pack is None else deepcopy(pack['facts']), 'delta': intent['delta'],
                            'preserve': intent['preserve'], 'dependent_effects': intent['dependent_effects'],
                            'profile_conflicts': deepcopy(profile.manifest.get('conflicts', [])),
                            'addendum': None if profile.addendum is None else profile.addendum['text'].decode('utf-8')}
                    system = load_prompt(root, config['vlm']['roles']['verifier']['prompt_id'])
                    reply, record = await verifier_client.call(system, json.dumps(data, ensure_ascii=False, allow_nan=False))
                    calls.append(_record_call(record))
                    warnings.extend(item[0] if isinstance(item, (list, tuple)) else item for item in record.get('warnings', []))
                    if record['status'] != 'OK' or not provenance_matches(record, bound['verifier_models']):
                        raise ValueError('verifier did not complete on a pinned route')
                    checked = validate_verifier_reply(reply, [item['id'] for item in pending], decoded, project_root=root)
                    by_id = {item['constraint_id']: item for item in checked}
                    for item in verdicts:
                        if item['id'] in by_id:
                            check = by_id[item['id']]
                            item.update(verdict=check['verdict'], evidence_span=check['evidence_span'], note=check['note'])
                except ModelCallError as error:
                    calls.append(_record_call({'status': safe_model_code(error.code), 'warnings': []}))
                    unavailable = True
                except (ConfigurationError, ValueError, KeyError, TypeError):
                    unavailable = True
            if unavailable:
                warnings.append('VERIFIER_UNAVAILABLE')
        attempts.append({'round': repairs, 'verdicts': deepcopy(verdicts)})
        if all(item['verdict'] == 'pass' for item in verdicts):
            if not _gated(config, intent, profile, identity_facts, pending):
                fraction = policy['verifier']['sampled_fraction_for_rest']
                selection = random.Random(str(config['resolution']['random_seed']) + ':' + intake_result.payload['request_id']).random()
                if selection < fraction:
                    try:
                        require_calibrated_verifier(config, profile, project_root=root)
                        monitoring = True
                    except ConfigurationError:
                        warnings.append('MONITORING_UNAVAILABLE')
            status = 'degraded' if degraded or 'BUDGET_EXHAUSTED' in warnings else 'repaired' if repairs else 'success'
            return finish(status, output, verdicts)
        if intent is None or unavailable or repairs >= policy['max_repairs'] or (not complete and last['status'] != 'SCHEMA_INVALID'):
            break
        # T017-FIX2 (G4): repair round k sends attempt_seed(base, k). A backend that drops the seed still repairs: the
        # repair brief carries the draft and the failures, so it never repeats the earlier request (fix round 1, F1).
        repairs += 1
        current = await write(intake_result, plan_result, assembly_result, writer_client, project_root=root,
                              repair={'draft': output if complete else None,
                                      'failures': [item for item in verdicts if item['verdict'] != 'pass']},
                              attempt=repairs)
        calls.extend(deepcopy(current['calls']))
        warnings.extend(current['warnings'])

    for name in policy['fallbacks']:
        if name == 'failed':
            fallbacks.append({'name': name, 'available': True})
            return finish('failed', None, verdicts, name)
        candidate = None
        try:
            if name == 'minimal_output_valid_for_contract' and template is not None:
                candidate = _minimal(template, intake_result.payload['prompt'])
            elif name == 'user_prompt_with_status_degraded':
                candidate = intake_result.payload['prompt']
            available = candidate is not None and _contract(candidate, profile, root, registry)
        except (ValueError, ProfileError):
            available = False
        fallbacks.append({'name': name, 'available': available})
        if available:
            warnings.append('VALIDATION_FALLBACK')
            return finish('degraded', candidate, verdicts, name)
    return finish('failed', None, verdicts)
