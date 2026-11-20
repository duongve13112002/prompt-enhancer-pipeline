"""Synthetic P bundles, configurations and labelled test doubles (test-only)."""

from copy import deepcopy
from pathlib import Path
import shutil

import yaml

from prompt_enhancer import load_config


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'configs/enhancer.example.yaml'
EXAMPLE = ROOT / 'configs/profiles/_example'

SECTIONED = ('[META]\nthinking_mode: disabled\n\n[SYSTEM]\nYou write one caption. Keep café as is.\n\n'
             '[USER]\nRATIO: {{aspect_ratio}}\nIdea: {{original_prompt}}\n').encode('utf-8')


def install_prompts(project_root):
    """Copy the pipeline-owned prompt files into the temporary project root."""
    target = Path(project_root) / 'prompts'
    target.mkdir(exist_ok=True)
    for path in (ROOT / 'prompts').glob('*.txt'):
        shutil.copyfile(path, target / path.name)


def config_data(tmp_path):
    """The example configuration with the drafter role and its backend resolved (test-only values)."""
    config = yaml.safe_load(BASE.read_bytes())
    config['storage']['lock'] = {'timeout_s': 5, 'poll_s': 0.05}
    role = config['vlm']['roles']['manifest_drafter']
    role['max_tokens'] = 128
    role['sampling'] = dict(temperature=0.2, top_p=0.9, top_k=20, min_p=0.0, presence_penalty=0.0,
                            frequency_penalty=0.0, repetition_penalty=1.0, seed='none', stop='none')
    role['reasoning'] = dict(mode='off', effort='none', budget_tokens='none', template_kwargs_key='enable_thinking')
    backend = config['vlm']['backends']['main']
    backend['engine'] = {'name': 'llama_cpp', 'version': 'test-version'}
    backend['base_urls'] = ['http://127.0.0.1:8080']
    backend['endpoints'] = {'chat': 'chat', 'health': 'health'}
    backend['model'].update(id='local-model', revision='revision')
    backend['max_concurrency'] = 1
    backend['http'] = dict(connect_timeout_s=1, read_timeout_s=1, write_timeout_s=1, pool_timeout_s=1,
                           max_retries=0, backoff_initial_s=0, backoff_max_s=0, retry_statuses=[])
    backend['probe'] = dict(max_requests=1, timeout_s=1, image_attempts=1)
    backend['image'] = dict(max_decode_pixels=1000, max_side='none', max_images_per_call=1)
    config['edit']['max_source_images'] = 1  # test-only (decision 179)
    backend['addendum_layout'] = 'separate_system_message'  # test-only layout (T017-FIX)
    backend.pop('addendum_separator', None)
    config['profiles'].update(root=str(tmp_path / 'profiles'), exclude=['_example'], default_profile_id='p1')
    return config


def load(tmp_path, project_root, data, command):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
    return load_config(path, command, project_root=project_root)


def bundle(tmp_path, profile_id, prompt):
    folder = tmp_path / 'profiles' / profile_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'system_prompt.txt').write_bytes(prompt)
    return folder


def draft(**changes):
    """A synthetic drafted manifest, as the drafter model would return it."""
    value = {
        'schema_version': 1, 'profile_id': 'p1', 'status': 'draft', 'system_prompt_hash': 'computed_at_registration',
        'template_format': 'sectioned', 'meta': {'thinking_mode': 'disabled'},
        'supported_tasks': ['generate'], 'output_kind': 'prose', 'output_schema_id': 'none',
        'output_language': 'English', 'edit_output_semantics': None,
        'template_variables': ['aspect_ratio', 'original_prompt'], 'template_values': {'aspect_ratio': 'auto'},
        'quote_semantics': 'literal_text', 'renderer': {'accepts_reference_images': False,
                                                        'image_reference_syntax': 'none'},
        'validator': {'checks': ['single_line']},
        'conflicts': [{'id': 'c1', 'profile_rule': 'Populate scenes', 'conflicts_with': 'creativity limits',
                       'config_keys': ['creativity.max_new_entities'], 'decision': 'undecided',
                       'decided_by_admin': False}],
        'golden_tests': [],
    }
    value.update(deepcopy(changes))
    return value


def example_draft():
    """The example manifest as a drafter would return it (status draft, hash placeholder)."""
    return yaml.safe_load((EXAMPLE / 'manifest.yaml').read_bytes())


class DrafterDouble:
    """Labelled test double for the T-009 bound `manifest_drafter` client."""

    label = 'test-double:manifest_drafter'

    def __init__(self, value=None, record=None, error=None):
        self.value, self.record, self.error = value, record or {'status': 'OK'}, error
        self.calls = []

    async def call(self, system_prompt, user_text, *, manifest=None, images=()):
        self.calls.append((system_prompt, user_text))
        if self.error is not None:
            raise self.error
        return deepcopy(self.value), deepcopy(self.record)


def envelope(status):
    return {'status': status, 'output': 'text', 'attachments': [], 'source_binding': None,
            'retrieval': {'state': 'disabled', 'snapshot_id': None, 'error': None}, 'warnings': [], 'trace_id': 't1'}


class RunnerDouble:
    """Labelled test double for the S0-S6 pipeline runner (T-013 to T-015 provide the real one)."""

    identity = 'test-double:pipeline-runner'

    def __init__(self, statuses=None, default='success', error=None, raw=None):
        self.statuses, self.default, self.error, self.raw = statuses or {}, default, error, raw
        self.calls = []

    async def run(self, view, request):
        self.calls.append((view, request))
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return envelope(self.statuses.get(request['prompt'], self.default))


def write_golden(folder, case_id, covers, *, prompt=None, task='generate', expect=('success',), source='none',
                 retrieval='disabled', grounding='not_required'):
    """A golden_case.v1 file (T017-FIX3). The defaults match `envelope()`: retrieval off, no grounding required."""
    path = folder / 'golden' / f'{case_id}.yaml'
    path.parent.mkdir(exist_ok=True)
    case = {'schema_version': 2, 'id': case_id, 'covers': list(covers),
            'request': {'task': task, 'prompt': prompt or f'prompt for {case_id}', 'source_image': source,
                        'retrieval': retrieval},
            'expect': {'status': list(expect), 'grounding': grounding}}
    path.write_text(yaml.safe_dump(case, sort_keys=False, allow_unicode=True), encoding='utf-8')


def set_manifest(folder, **changes):
    """Edit the manifest like an admin would, by hand (test-only)."""
    path = folder / 'manifest.yaml'
    manifest = yaml.safe_load(path.read_bytes())
    manifest.update(changes)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return manifest
