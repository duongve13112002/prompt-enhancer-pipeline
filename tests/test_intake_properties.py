"""Bounded S0 parser/image invariants; arbitrary text stays verbatim (R-003)."""

# Function fixtures supply read-only schemas/active bundles; each example closes its result and copies mutable config.

import base64
from copy import deepcopy
from io import BytesIO

from hypothesis import HealthCheck, given, settings, strategies as st
from PIL import Image
import pytest

from intake_fixtures import activate, configuration
from prompt_enhancer.config import ConfigurationError, effective_request_config
from prompt_enhancer.intake import intake, validate_request


@pytest.fixture
def environment(model_test_profile, tmp_path, project_root):
    config = configuration(model_test_profile, tmp_path)
    activate(config, project_root)
    return config


@given(st.text(max_size=150))
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_request_prompt_is_verbatim(project_root, prompt):
    request = {'task': 'generate', 'prompt': prompt}
    assert validate_request(request, project_root=project_root)['prompt'] == prompt


@given(st.text(max_size=60))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_arbitrary_prompt_survives_intake(environment, project_root, prompt):
    with intake({'task': 'generate', 'prompt': prompt}, environment, project_root=project_root) as result:
        assert result.payload['outcome'] == 'ready'
        assert result.payload['prompt'] == prompt


@given(st.integers(1, 50), st.integers(1, 50), st.one_of(st.just('none'), st.integers(1, 60)))
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_intake_resize_never_upscales_and_keeps_ratio(environment, project_root, width, height, limit):
    source = BytesIO()
    Image.new('RGB', (width, height)).save(source, format='PNG')
    config = deepcopy(environment)
    config['request_overrides']['allowed_keys'] = ['vision.max_image_side']
    request = {'task': 'edit', 'prompt': '', 'source_image': base64.b64encode(source.getvalue()).decode(),
               'overrides': {'vision.max_image_side': limit}}
    with intake(request, config, project_root=project_root) as result:
        assert result.payload['outcome'] == 'ready'
        with Image.open(BytesIO(base64.b64decode(result.payload['source_image']['data']))) as decoded:
            assert decoded.width <= width and decoded.height <= height
            assert max(decoded.size) == (max(width, height) if limit == 'none' else min(limit, max(width, height)))
            assert abs(decoded.width * height - decoded.height * width) <= max(width, height)


@given(st.one_of(st.booleans(), st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=20)))
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_override_integer_types_are_strict(environment, project_root, value):
    config = deepcopy(environment)
    config['request_overrides']['allowed_keys'] = ['vision.max_decode_pixels']
    with pytest.raises(ConfigurationError):
        effective_request_config(config, {'vision.max_decode_pixels': value}, project_root=project_root)
