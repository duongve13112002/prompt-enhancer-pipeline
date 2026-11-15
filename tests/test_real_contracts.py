import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from prompt_enhancer import ConfigurationError, load_config, load_profile, resolve_writer_format


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
    manifest_schema = json.loads((ROOT / 'contracts/schemas/profile_manifest.v0.schema.json').read_bytes())
    jsonschema.Draft202012Validator(manifest_schema).validate(manifest)
    assert manifest['output_schema_id'] == 'none'


def test_real_full_configuration_schema():
    schema = json.loads((ROOT / 'contracts/schemas/enhancer_config.v0.schema.json').read_bytes())
    jsonschema.Draft202012Validator.check_schema(schema)
    config = yaml.safe_load((ROOT / 'configs/enhancer.example.yaml').read_bytes())
    def leaves(error):
        if not error.context:
            return [(list(error.absolute_path), error.message)]
        return [item for child in error.context for item in leaves(child)]

    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(config))
    assert not errors, [item for error in errors for item in leaves(error) if 'is not one of' not in item[1]][:20]


def test_real_loader_examples():
    base = ROOT / 'configs/enhancer.example.yaml'
    dev = ROOT / 'configs/enhancer.dev.example.yaml'
    overlay = ROOT / 'configs/overlays/deploy-vllm.example.yaml'
    # The examples keep storage.lock at deploy_required, so a store command refuses them; profile check reads no storage.
    for args in ((base,), (dev,), (base, overlay), (overlay,)):
        with pytest.raises(ConfigurationError, match='storage.lock.timeout_s: unresolved sentinel deploy_required'):
            load_config(args[0], 'kb status', overlay=args[1] if len(args) > 1 else None)
    full = load_config(base, 'profile check')
    smoke = load_config(dev, 'profile check')
    deployed = load_config(base, 'profile check', overlay=overlay)
    direct_overlay = load_config(overlay, 'profile check')
    assert full['storage']['kb_store'] == 'data/kb'
    assert smoke['vision']['max_image_side'] == 1024
    assert deployed['vlm']['backends']['main']['unsupported_param_policy'] == 'warn'
    assert direct_overlay['vlm']['backends']['main']['unsupported_param_policy'] == 'warn'
    profile_path = ROOT / 'configs/profiles/_example'
    manifest, prompt = load_profile(profile_path)
    assert prompt == (profile_path / 'system_prompt.txt').read_bytes()
    writer = resolve_writer_format(full['vlm']['roles']['writer'], manifest)
    assert writer['output']['format'] == 'none'
