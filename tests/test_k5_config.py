"""Every K5 profile key has a visible override or singleton refusal; no deployment values fitted here."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from k5_fixtures import config, published
from prompt_enhancer.config import ConfigurationError, load_config, validate_index_reads
from prompt_enhancer.k5_index import ServingIndex, build_index, _allocation_bound
from prompt_enhancer.storage import StorageError
from test_k2_config import set_key


KEYS = ['index', 'index.engine', 'index.metric', 'index.build', 'index.build.block_rows',
        'index.lexical', 'index.lexical.kind', 'index.lexical.ngram_size']


@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('failure', ['missing', 'sentinel', 'wrong_type'])
def test_each_index_key_refused_before_activity(project_root, key, failure):
    changed = config()
    set_key(changed, key, 'fit_from_bench' if failure == 'sentinel' else True, remove=failure == 'missing')
    builder = SimpleNamespace(store=SimpleNamespace(catalog=SimpleNamespace(root=project_root)))
    with pytest.raises(ConfigurationError):
        build_index(builder, changed)  # no writer/tables: any activity before refusal would fail
    with pytest.raises(ConfigurationError):
        ServingIndex(builder, changed)


@pytest.mark.parametrize(('key', 'value'), [
    ('index.engine', 'faiss'), ('index.metric', 'l2'), ('index.lexical.kind', 'words'),
    ('index.build.block_rows', 0), ('index.build.block_rows', -1), ('index.build.block_rows', 2.0),
    ('index.build.block_rows', sys.maxsize + 1), ('index.lexical.ngram_size', 0),
    ('index.lexical.ngram_size', 1.0), ('index.lexical.ngram_size', sys.maxsize + 1),
    ('index.extra', 1), ('index.build.extra', 1),
])
def test_invalid_index_values_refused(project_root, key, value):
    changed = config()
    set_key(changed, key, value)
    with pytest.raises(ConfigurationError):
        validate_index_reads(changed, project_root=project_root)


@pytest.mark.parametrize('key', ['ann', 'exact_search_below_count'])
def test_legacy_index_keys_have_migration_message(k2_env, key):
    changed = deepcopy(k2_env.config)
    changed['index'][key] = {}
    with pytest.raises(ConfigurationError, match='obsolete index configuration; migrate'):
        validate_index_reads(changed, project_root=k2_env.root)
    path = k2_env.root / 'k5-migration.yaml'
    path.write_text(yaml.safe_dump(changed), encoding='utf-8')
    with pytest.raises(ConfigurationError, match='obsolete index configuration; migrate'):
        load_config(path, 'kb sync', project_root=k2_env.root)


@pytest.mark.parametrize('key', KEYS[1:])
def test_loader_index_refusals(k2_env, key):
    for remove in (True, False):
        changed = deepcopy(k2_env.config)
        set_key(changed, key, 'design_required', remove=remove)
        path = k2_env.root / 'k5-refusal.yaml'
        path.write_text(yaml.safe_dump(changed), encoding='utf-8')
        with pytest.raises(ConfigurationError):
            load_config(path, 'kb sync', project_root=k2_env.root)


@pytest.mark.parametrize(('block', 'n', 'metric'), [(1, 1, 'cosine'), (3, 3, 'inner_product')])
def test_each_index_key_override_observed(project_root, tmp_path, block, n, metric):
    env = published(project_root, tmp_path, settings=config(block=block, n=n, metric=metric))
    with env.store.pin() as pin:
        reader = ServingIndex(pin, env.config)
        assert reader.metadata['settings'] == env.config['index']
        assert all(part['rows'] <= block for part in reader.metadata['parts']
                   if part['kind'].endswith('embeddings'))
        expected_grams = {'kimono'[i:i+n] for i in range(len('kimono')-n+1)}
        assert reader.lexical('Kimono')[0]['matched_grams'] == len(expected_grams)
        hits = reader.search('image_embeddings', [0.5, -0.25, 1, 0], k=1,
                             embedder_revision='emb-r1', dimension=4)
        assert hits[0]['score'] == pytest.approx(1 if metric == 'cosine' else 1.3125)


def test_base_and_dev_keep_unresolved_index_lineage(project_root):
    root = Path(__file__).resolve().parents[1]
    base = yaml.safe_load((root / 'configs/enhancer.example.yaml').read_bytes())
    dev = yaml.safe_load((root / 'configs/enhancer.dev.example.yaml').read_bytes())
    assert 'index' not in dev
    assert dev['extends'] == 'enhancer.example.yaml'
    assert base['index'] == {'engine': 'design_required', 'metric': 'design_required',
                             'build': 'fit_from_bench', 'lexical': {'kind': 'char_ngram', 'ngram_size': 'fit_from_gt'}}
    with pytest.raises(ConfigurationError, match='index.engine: unresolved sentinel'):
        validate_index_reads(base, project_root=project_root)


def test_real_example_lineages_refuse_each_index_sentinel(k2_env):
    repo = Path(__file__).resolve().parents[1]
    profiles = k2_env.root / 'configs'
    profiles.mkdir(exist_ok=True)
    for name in ('enhancer.example.yaml', 'enhancer.dev.example.yaml'):
        (profiles / name).write_bytes((repo / 'configs' / name).read_bytes())
    resolved = deepcopy(k2_env.config)
    del resolved['index']  # unrelated fields are labelled synthetic; index inherits the actual examples
    # T017-FIX: the dev parent sets joined_system_message with a separator, which a child cannot delete.
    resolved['vlm']['backends']['main'].update(addendum_layout='joined_system_message', addendum_separator='\n\n')
    for parent in ('enhancer.example.yaml', 'enhancer.dev.example.yaml'):
        for overrides, refused in [({}, 'engine'), ({'engine': 'numpy_exact'}, 'metric'),
                                   ({'engine': 'numpy_exact', 'metric': 'inner_product'}, 'build'),
                                   ({'engine': 'numpy_exact', 'metric': 'inner_product', 'build': {'block_rows': 2}},
                                    'lexical.ngram_size')]:
            path = profiles / 'k5-lineage-test.yaml'
            child = dict(resolved, extends=parent)
            if overrides:
                child['index'] = overrides
            path.write_text(yaml.safe_dump(child), encoding='utf-8')
            with pytest.raises(ConfigurationError, match='index.' + refused + ': unresolved sentinel'):
                load_config(path, 'kb sync', project_root=k2_env.root)


@settings(max_examples=25, deadline=None)
@given(st.one_of(st.integers(max_value=0), st.booleans(), st.floats(allow_nan=True, allow_infinity=True)))
def test_index_integer_validator_property(value):
    root = Path(__file__).resolve().parents[1]
    for key in ('index.build.block_rows', 'index.lexical.ngram_size'):
        changed = config()
        set_key(changed, key, value)
        with pytest.raises(ConfigurationError):
            validate_index_reads(changed, project_root=root)


def test_allocation_product_refused_before_native_allocation():
    with pytest.raises(StorageError, match='allocation size'):
        _allocation_bound(sys.maxsize, 4)


def test_k5_properties_have_no_deadlines():
    import test_k5_properties
    modules = (sys.modules[__name__], test_k5_properties)
    properties = [test for module in modules for test in vars(module).values()
                  if getattr(test, 'is_hypothesis_test', False)]
    assert properties
    assert all(test._hypothesis_internal_use_settings.deadline is None for test in properties)
