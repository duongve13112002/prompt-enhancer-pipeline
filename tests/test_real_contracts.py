import json
from pathlib import Path
import shutil

import jsonschema
import pytest
import yaml
from prompt_enhancer import ConfigurationError, load_config, load_profile, resolve_writer_format
from prompt_enhancer.config import _configuration_schema


ROOT = Path(__file__).resolve().parents[1]


def test_real_example_contracts():
    schema = json.loads((ROOT / 'contracts/schemas/vlm_role.v0.schema.json').read_bytes())
    jsonschema.Draft202012Validator.check_schema(schema)
    for section, key in [('sampling', 'seed'), ('sampling', 'stop'), ('reasoning', 'budget_tokens')]:
        validator = jsonschema.Draft202012Validator(schema['properties'][section]['properties'][key])
        assert validator.is_valid('none')
        assert not validator.is_valid(True)
    config_paths = ['configs/enhancer.example.yaml', 'configs/enhancer.dev.example.yaml']
    for path in config_paths:
        config = yaml.safe_load((ROOT / path).read_bytes())
        for backend in config['vlm']['backends'].values():
            assert backend['unsupported_param_policy'] == 'warn'
    overlay_path = ROOT / 'configs/overlays/deploy-vllm.example.yaml'
    overlay = yaml.safe_load(overlay_path.read_bytes())
    assert (overlay_path.parent / overlay['overlay_of']).is_file()
    assert 'unsupported_param_policy' not in overlay['vlm']['backends']['main']
    manifest = yaml.safe_load((ROOT / 'configs/profiles/_example/manifest.yaml').read_bytes())
    assert manifest['schema_version'] == 2  # MS-2 (Q5): the example moved to the multi-source renderer, profile_manifest.v1
    manifest_schema = json.loads((ROOT / 'contracts/schemas/profile_manifest.v1.schema.json').read_bytes())
    jsonschema.Draft202012Validator(manifest_schema).validate(manifest)
    assert manifest['output_schema_id'] == 'none'


def test_real_full_configuration_schema():
    config = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    schema = _configuration_schema(ROOT, config)
    jsonschema.Draft202012Validator.check_schema(schema)
    def leaves(error):
        if not error.context:
            return [(list(error.absolute_path), error.message)]
        return [item for child in error.context for item in leaves(child)]

    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(config))
    assert not errors, [item for error in errors for item in leaves(error) if 'is not one of' not in item[1]][:20]


def test_real_loader_examples(tmp_path):
    base = ROOT / 'configs/enhancer.example.yaml'
    dev = ROOT / 'configs/enhancer.dev.example.yaml'
    overlay = ROOT / 'configs/overlays/deploy-vllm.example.yaml'
    # The examples keep storage.lock at deploy_required, so a store command and `profile check` (which bounds its
    # check-lock wait by storage.lock since T-012 fix round 1) refuse them.
    for command in ('kb status', 'profile check'):
        for args in ((base,), (dev,), (base, overlay), (overlay,)):
            with pytest.raises(ConfigurationError, match='storage.lock.timeout_s: unresolved sentinel deploy_required'):
                load_config(args[0], command, overlay=args[1] if len(args) > 1 else None)
    # The whole example lineage still loads once the deploy-time lock wait is set (test-only values, scratch copy).
    project = tmp_path / 'project'
    shutil.copytree(ROOT / 'configs', project / 'configs')
    shutil.copytree(ROOT / 'contracts/schemas', project / 'contracts/schemas')
    line = 'lock: {timeout_s: deploy_required, poll_s: deploy_required}'
    for name in ('enhancer.example.yaml', 'overlays/deploy-vllm.example.yaml'):  # both set the deploy-time wait
        copied = project / 'configs' / name
        text = copied.read_text(encoding='utf-8')
        assert text.count(line) == 1
        copied.write_text(text.replace(line, 'lock: {timeout_s: 5, poll_s: 0.05}'), encoding='utf-8')
    paths = [project / path.relative_to(ROOT) for path in (base, dev, overlay)]
    full = load_config(paths[0], 'profile check', project_root=project)
    smoke = load_config(paths[1], 'profile check', project_root=project)
    deployed = load_config(paths[0], 'profile check', overlay=paths[2], project_root=project)
    direct_overlay = load_config(paths[2], 'profile check', project_root=project)
    assert full['storage']['kb_store'] == 'data/kb'
    assert smoke['vision']['max_image_side'] == 1024
    assert deployed['vlm']['backends']['main']['unsupported_param_policy'] == 'warn'
    assert direct_overlay['vlm']['backends']['main']['unsupported_param_policy'] == 'warn'
    profile_path = ROOT / 'configs/profiles/_example'
    manifest, prompt = load_profile(profile_path)
    assert prompt == (profile_path / 'system_prompt.txt').read_bytes()
    writer = resolve_writer_format(full['vlm']['roles']['writer'], manifest)
    assert writer['output']['format'] == 'none'
