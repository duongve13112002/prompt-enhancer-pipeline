"""T-018 P3 fix round 1, F7: K1's read set of an `embed_server` embedder, per caller of `validate_embedder_reads`.

K1 folds six `server` settings into its embedding-space identity (`families.output_fingerprint`); the serving limits
and `reference_check` belong to `embed serve` and `embed reference-check`. For each caller (`embed dev-compare`,
`kb sync`, K1's standalone `embedder_settings`, S1's dense gate) a sentinel in an unread leaf no longer refuses, and
a sentinel in a read leaf (or in place of the whole block) still does (PM answer 1). Test-only profiles; no model, no
request.
"""

from copy import deepcopy
import re

import pytest

from embed_server_fixtures import server_profile
from intake_fixtures import configuration
from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer.config import validate_candidates_reads
from prompt_enhancer.embed_server import dev_compare as dc
from prompt_enhancer.k1_embed import embedder_settings
from test_embed_server_dev_compare import dev_profile, kb, write
from test_k0_config import config as kb_sync_config  # noqa: F401  (the resolved synthetic `kb sync` profile)
from test_k0_config import load as load_kb_sync

SERVER = 'vlm.backends.embedder.server'
# Read by K1 (families.output_fingerprint): each with a sentinel its schema admits.
READ = {'family': 'deploy_required', 'dtype': 'deploy_required', 'attn_implementation': 'deploy_required',
        'max_length': 'deploy_required', 'image.min_pixels': 'deploy_required', 'image.max_pixels': 'deploy_required',
        'image.max_soft_tokens': 'deploy_required', 'default_instruction': 'design_required',
        # Read, not hashed (RES-1 PM answer 4): the client's max_concurrency must fit the server's admission slots.
        'max_pending_requests': 'deploy_required'}
# Read by the server or the reference check only: outside K1's read set.
UNREAD = {'forward_batch': 'deploy_required', 'idle_exit_s': 'deploy_required', 'max_request_bytes': 'deploy_required',
          'max_image_bytes': 'deploy_required',
          'max_connections': 'deploy_required', 'max_discard_bytes': 'deploy_required',
          'read_timeout_s': 'deploy_required', 'reference_check.min_cosine': 'fit_from_bench',
          'reference_check.max_norm_deviation': 'fit_from_bench'}


def _with(profile, leaf, value):
    """`profile` with `server.<leaf>` set to `value` (a measured `reference_check` first, so one leaf differs)."""
    profile = deepcopy(profile)
    server = profile['vlm']['backends']['embedder']['server']
    server['reference_check'] = {'min_cosine': 0.9, 'max_norm_deviation': 0.001}  # test-only tolerances
    if leaf is None:
        profile['vlm']['backends']['embedder']['server'] = value
        return profile
    *parents, name = leaf.split('.')
    for part in parents:
        server = server[part]
    server[name] = value
    return profile


@pytest.fixture
def callers(project_root, tmp_path, model_test_profile, kb_sync_config):
    """Each caller as (profile, check): `check(profile)` runs the caller's own validation."""
    kb(tmp_path)
    dev = server_profile(dev_profile(model_test_profile, project_root, tmp_path))
    sync = server_profile(kb_sync_config)
    s1 = configuration(deepcopy(model_test_profile), tmp_path)
    s1['rag']['enabled'] = True
    s1['candidates']['sources']['dense']['enabled'] = True  # the dense source runs K1's check
    s1 = server_profile(s1)
    s1['embedding'].update(chosen=sync['embedding']['chosen'], revision=sync['embedding']['revision'])

    def dev_compare(profile):
        embedder_settings(load_config(write(tmp_path, profile, 'rs-dev.yaml'), dc.COMMAND, project_root=project_root))

    return {'embed dev-compare': (dev, dev_compare),
            'kb sync': (sync, lambda profile: load_kb_sync(project_root, profile)),
            'K1 embedder_settings': (sync, embedder_settings),
            'S1 dense gate': (s1, lambda profile: validate_candidates_reads(profile, project_root=project_root))}


CALLERS = ('embed dev-compare', 'kb sync', 'K1 embedder_settings', 'S1 dense gate')


@pytest.mark.parametrize('caller', CALLERS)
@pytest.mark.parametrize('leaf', sorted(UNREAD))
def test_a_sentinel_in_an_unread_server_leaf_does_not_refuse(callers, caller, leaf):
    profile, check = callers[caller]
    check(_with(profile, leaf, UNREAD[leaf]))  # refused before the fix: the whole block was sentinel-checked


@pytest.mark.parametrize('caller', CALLERS)
@pytest.mark.parametrize('leaf', [*sorted(READ), None])
def test_a_sentinel_in_a_read_server_leaf_still_refuses(callers, caller, leaf):
    profile, check = callers[caller]
    key = SERVER if leaf is None else f'{SERVER}.{leaf}'
    with pytest.raises(ConfigurationError, match=re.escape(key)):
        check(_with(profile, leaf, READ.get(leaf, 'deploy_required')))


@pytest.mark.parametrize('caller', CALLERS)
def test_the_resolved_profile_passes_every_caller(callers, caller):
    profile, check = callers[caller]
    check(_with(profile, 'idle_exit_s', 0))


def _capacity(profile, concurrency, pending):
    profile = _with(profile, 'max_pending_requests', pending)
    profile['vlm']['backends']['embedder']['max_concurrency'] = concurrency
    return profile


@pytest.mark.parametrize('caller', CALLERS)
def test_client_concurrency_above_the_server_admission_slots_is_refused(callers, caller):
    """RES-1 PM answer 4: requests past `server.max_pending_requests` are refused as overloaded by the server itself,
    so a client allowed more in flight would only retry against refusals it caused; refused at load instead."""
    profile, check = callers[caller]
    with pytest.raises(ConfigurationError, match=re.escape('vlm.backends.embedder.max_concurrency: exceeds server.'
                                                           'max_pending_requests')):
        check(_capacity(profile, 3, 2))


@pytest.mark.parametrize('caller', CALLERS)
@pytest.mark.parametrize('concurrency, pending', [(3, 3), (2, 5)])
def test_client_concurrency_within_the_server_admission_slots_is_accepted(callers, caller, concurrency, pending):
    profile, check = callers[caller]
    check(_capacity(profile, concurrency, pending))  # non-default override: equal is allowed
