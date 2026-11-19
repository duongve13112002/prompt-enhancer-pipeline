"""Explicit synthetic S0 configuration and active profiles; model calls use labelled doubles."""

import asyncio
from copy import deepcopy
from pathlib import Path

from profile_fixtures import (
    SECTIONED, DrafterDouble, RunnerDouble, bundle, draft, install_prompts, set_manifest, write_golden,
)
from prompt_enhancer.profiles import check_profile, confirm_profile, register_profile


def configuration(model_test_profile, tmp_path):
    config = deepcopy(model_test_profile)
    config['profiles'].update(root=str(tmp_path / 'profiles'), default_profile_id='p1')
    config['rag']['enabled'] = False
    config['vision'].update(max_decode_pixels=10000, quality=80, format='png')
    config['runtime'].update(intake_deadline_s=60, vram_budget_gb=1)
    # Explicit test-only lifecycle capacities/time budgets, never deployment defaults.
    config['runtime'].update(request_deadline_s=60,
        trace={'retention_s': 60, 'queue_limit': 2, 'workers': 1, 'drain_timeout_s': 2},
        monitoring={'queue_limit': 2, 'workers': 1, 'drain_timeout_s': 2})
    config['storage'].update(kb_store=str(tmp_path / 'kb'), lock={'timeout_s': 1, 'poll_s': 0.01})
    config['index'].update(engine='numpy_exact', metric='cosine', build={'block_rows': 2},
                           lexical={'kind': 'char_ngram', 'ngram_size': 2})
    config['candidates']['shortlist_size'] = 2
    config['candidates'].update(rrf_constant=10, max_prompt_windows=10000)  # labelled test-only
    for source in config['candidates']['sources'].values():
        source['depth'] = 3
    config['embedding']['query_text_limit'] = 100
    config['embedding']['query_text_template'] = 'query: {text}'  # labelled test-only, not an E5 prefix
    config['assembly'].update(max_facts_per_concept=2, max_images_per_concept=1,
                              max_facts_per_request=4, fact_order='need_then_support')
    config['edit']['max_kb_reference_images'] = 0
    config['validation']['verifier']['sampled_fraction_for_rest'] = 0
    config['validation']['verifier']['require_calibration'] = True
    for name in ('planner', 'writer', 'verifier', 'manifest_drafter'):
        role = config['vlm']['roles'][name]
        prototype = config['vlm']['roles']['observer']
        for field in ('max_tokens', 'sampling', 'reasoning'):
            role[field] = deepcopy(prototype[field])
        if isinstance(role['image'], dict):
            role['image'].update(source='source_image', quality=80)
    config['vlm']['roles']['planner']['image']['source'] = 'edit_source_only'  # the only source S2 sends (T-014)
    config['vlm']['roles']['writer']['output'].update(strict=True, transport='response_format')
    return config


def activate(config, project_root, *, tasks=('generate', 'edit'), profile_id='p1', renderer=None):
    """Use real registration/check logic with labelled drafter and pipeline-runner doubles.

    A configuration that attaches knowledge images gets a renderer that accepts them unless `renderer` is given
    (decision 177 refuses the other pair).
    """
    install_prompts(project_root)
    folder = bundle(Path(config['profiles']['root']).parent, profile_id, SECTIONED)
    if renderer is None and config['edit']['attach_kb_reference_images'] is True:
        renderer = {'accepts_reference_images': True, 'image_reference_syntax': 'none'}
    extra = {} if renderer is None else {'renderer': renderer}
    value = draft(profile_id=profile_id, supported_tasks=list(tasks),
                  edit_output_semantics='whole_edited_image' if 'edit' in tasks else None, **extra)
    asyncio.run(register_profile(config, profile_id, DrafterDouble(value), project_root=project_root))
    confirm_profile(config, profile_id, {'c1': 'profile_wins'}, project_root=project_root)
    write_golden(folder, 'g1', ['c1'])
    set_manifest(folder, golden_tests=['g1'])
    asyncio.run(check_profile(config, profile_id, RunnerDouble(), project_root=project_root))
    return folder
