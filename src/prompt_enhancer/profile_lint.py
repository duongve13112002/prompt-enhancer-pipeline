"""Structural conflict lint, template checks and the runtime addendum (P).

Finding which rules clash is the drafter model's job and the admin's confirmation; this module only
checks shapes, decisions and the configuration keys a conflict cites (AGENTS.md section 5, R-015).
"""

import json

from .config import SENTINELS
from .profile_prompt import placeholders, sha256_tag


# Protocol-bound decision enum of profile_manifest.v0 (conflicts.items.decision).
DECISIONS = ('undecided', 'profile_wins', 'config_wins', 'block_activation')
ADDENDUM_ID = 'runtime_addendum_v0'  # artifact-bound: prompts/runtime_addendum_v0.txt
_MISSING = object()


def resolve_key(config, key):
    """Return the configured value at a dotted key, or `_MISSING`."""
    value = config
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _holds_sentinel(value):
    if isinstance(value, str):
        return value in SENTINELS
    if isinstance(value, dict):
        return any(_holds_sentinel(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_sentinel(item) for item in value)
    return False


def key_findings(conflicts, config):
    """Cited keys that are missing or unresolved; these are configuration refusals (R-015)."""
    findings = []
    for conflict in conflicts if isinstance(conflicts, list) else ():
        if not isinstance(conflict, dict) or not isinstance(conflict.get('config_keys', []), list):
            continue
        for key in conflict.get('config_keys', []):
            if not isinstance(key, str) or not key:
                continue  # reported as a shape finding
            value = resolve_key(config, key)
            if value is _MISSING:
                findings.append(('CONFLICT_KEY_UNKNOWN', conflict.get('id'), key))
            elif _holds_sentinel(value):
                findings.append(('CONFLICT_KEY_SENTINEL', conflict.get('id'), key))
    return findings


def conflict_findings(conflicts):
    """Shape and decision findings, as (code, conflict_id, detail) in input order.

    `CONFLICT_UNDECIDED` and `CONFLICT_BLOCKS` are valid states that block activation; every other code
    is an invalid manifest.
    """
    if not isinstance(conflicts, list):
        return [('CONFLICT_SHAPE', None, 'conflicts must be a list')]
    findings, seen = [], set()
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict):
            findings.append(('CONFLICT_SHAPE', None, f'conflict {index} is not a mapping'))
            continue
        cid = conflict.get('id')
        if not isinstance(cid, str) or not cid:
            findings.append(('CONFLICT_SHAPE', None, f'conflict {index} has no id'))
            continue
        if cid in seen:
            findings.append(('CONFLICT_DUPLICATE_ID', cid, 'id appears more than once'))
        seen.add(cid)
        decision, by_admin = conflict.get('decision'), conflict.get('decided_by_admin')
        keys = conflict.get('config_keys', [])
        if not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys) \
                or len(set(map(str, keys))) != len(keys):
            findings.append(('CONFLICT_SHAPE', cid, 'config_keys must be distinct non-empty strings'))
            keys = []
        if decision not in DECISIONS or type(by_admin) is not bool:
            findings.append(('CONFLICT_SHAPE', cid, 'decision or decided_by_admin is invalid'))
        elif decision == 'undecided' and by_admin:
            findings.append(('CONFLICT_DECISION_INVALID', cid, 'undecided conflict marked as decided by the admin'))
        elif decision != 'undecided' and not by_admin:
            findings.append(('CONFLICT_UNCONFIRMED', cid, f'{decision} was not decided by the admin'))
        elif decision == 'undecided':
            findings.append(('CONFLICT_UNDECIDED', cid, 'needs an admin decision'))
        elif decision == 'block_activation':
            findings.append(('CONFLICT_BLOCKS', cid, 'the admin blocked activation'))
        elif decision == 'config_wins' and not keys:
            findings.append(('CONFLICT_KEY_REQUIRED', cid, 'config_wins needs at least one config key'))
    return findings


def template_findings(manifest, structure, user_template):
    """Check the manifest's template declaration against the prompt's structure."""
    findings = []
    fmt = manifest.get('template_format')
    variables = manifest.get('template_variables', [])
    values = manifest.get('template_values', {})
    if fmt == 'plain':
        if variables or values:
            findings.append(('TEMPLATE_INVALID', 'plain prompts have no template variables or values'))
        return findings
    if structure.template_format != 'sectioned':
        return [('TEMPLATE_INVALID', 'manifest says sectioned but the prompt has no sections')]
    found = placeholders(user_template)
    if len(set(variables)) != len(variables) or set(variables) != set(found):
        findings.append(('TEMPLATE_INVALID', f'template_variables {variables} differ from the [USER] placeholders {found}'))
    extra = sorted(set(values) - set(found))
    if extra:
        findings.append(('TEMPLATE_INVALID', f'template_values name unknown variables {extra}'))
    inputs = [name for name in found if name not in values]
    if len(inputs) != 1:
        findings.append(('TEMPLATE_INVALID', f'exactly one variable must lack a template value, found {inputs}'))
    return findings


def input_variable(manifest):
    """The template variable the Enhancement Brief fills: the one without a template value."""
    if manifest['template_format'] == 'plain':
        return None
    values = manifest.get('template_values', {})
    (name,) = [item for item in manifest['template_variables'] if item not in values]
    return name


def render_addendum(conflicts, config, template):
    """Render the addendum for `config_wins` conflicts, or None when there is none.

    The text is the template bytes followed by one JSON line of data; its hash versions it in the trace.
    """
    entries = []
    for conflict in conflicts:
        if conflict['decision'] != 'config_wins' or not conflict['decided_by_admin']:
            continue
        entries.append({'conflict': conflict['id'], 'profile_rule': conflict['profile_rule'],
                        'configuration': {key: resolve_key(config, key) for key in conflict['config_keys']}})
    if not entries:
        return None
    data = json.dumps(entries, ensure_ascii=False, allow_nan=False).encode('utf-8')
    text = template + data + b'\n'
    return {'id': ADDENDUM_ID, 'text': text, 'sha256': sha256_tag(text)}
