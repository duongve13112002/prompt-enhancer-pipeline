"""Real tiny K5 integration; all encoders/transports explicitly labelled doubles."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
from jsonschema import Draft202012Validator
import pytest

from candidates_fixtures import QueryEncoderDouble, candidate_env, source_only
from prompt_enhancer.candidates import build_candidates, query_embedder_settings, validate_shortlist
import prompt_enhancer.candidates as module
from prompt_enhancer.config import ConfigurationError
from prompt_enhancer.intake import IntakeResult
from prompt_enhancer.k1_embed import Embedder, embedder_settings
from prompt_enhancer.storage import StorageError


def run(env, prompt, encoder=None):
    env.intake.payload['prompt'] = prompt
    encoder = QueryEncoderDouble(env.config) if encoder is None else encoder
    return asyncio.run(build_candidates(env.intake, embedder=encoder)), encoder


def forbidden(*args, **kwargs):
    pytest.fail('forbidden retrieval construction/call')


def test_candidates_off_is_programming_error_before_construction(candidate_env, monkeypatch):
    env = candidate_env
    env.config['rag']['enabled'] = False
    monkeypatch.setattr(module, 'ServingIndex', forbidden)
    monkeypatch.setattr(module, 'query_embedder_settings', forbidden)
    with pytest.raises(ValueError, match='retrieval-disabled'):
        asyncio.run(build_candidates(env.intake))


@pytest.mark.parametrize('empty', ['prompt', 'sources', 'cap'])
def test_candidates_empty_needs_no_reader(candidate_env, monkeypatch, empty):
    env = candidate_env
    if empty == 'sources':
        source_only(env.config, 'none')
    if empty == 'cap':
        env.config['candidates']['shortlist_size'] = 0
    monkeypatch.setattr(module, 'ServingIndex', forbidden)
    result, encoder = run(env, '' if empty == 'prompt' else 'text')
    assert result['state'] == 'ready' and result['candidates'] == [] and encoder.calls == []


@pytest.mark.parametrize('source', ['alias', 'lexical', 'dense'])
def test_candidates_each_source_switch(candidate_env, monkeypatch, source):
    env = candidate_env
    source_only(env.config, source)
    for method in ('exact', 'lexical', 'search'):
        if method != {'alias': 'exact', 'lexical': 'lexical', 'dense': 'search'}[source]:
            monkeypatch.setattr(module.ServingIndex, method, forbidden)
    name = env.rows['concepts'][0]['display_name']
    result, encoder = run(env, 'no ' + name)
    assert result['state'] == 'ready' and result['candidates']
    assert {entry['matched_source'] for entry in result['candidates']} <= {'name', 'alias'} if source == 'alias' else {
        entry['matched_source'] for entry in result['candidates']} == {source}
    assert bool(encoder.calls) is (source == 'dense')


def test_candidates_original_span_alias_and_schema(candidate_env):
    env = candidate_env
    source_only(env.config, 'alias')
    alias = env.rows['aliases'][0]
    prompt = 'xx' + alias['alias_text'] + 'yy'
    result, _ = run(env, prompt)
    target = next(entry for entry in result['candidates'] if entry['concept_id'] == alias['concept_id'])
    # Same concept's shorter canonical name wins a deterministic equal-rank provenance tie.
    assert target['matched_source'] == 'name'
    assert target['matched_span']['start'] == 2
    assert target['description'] == 'desc 1'
    assert validate_shortlist(result, prompt, project_root=env.store.catalog.root) is result
    broken = deepcopy(result)
    broken['candidates'].append(broken['candidates'][0])
    with pytest.raises(ValueError, match='duplicate'):
        validate_shortlist(broken, prompt, project_root=env.store.catalog.root)
    broken = deepcopy(result)
    broken['candidates'][0]['matched_span']['text'] = 'wrong'
    with pytest.raises(ValueError, match='span'):
        validate_shortlist(broken, prompt, project_root=env.store.catalog.root)


def test_candidates_casefold_lookup(model_test_profile, tmp_path, project_root):
    from intake_fixtures import configuration
    from k5_fixtures import published
    from prompt_enhancer.storage import SNAPSHOT_TABLES, concept_id

    config = configuration(model_test_profile, tmp_path)
    config['rag']['enabled'] = True
    source_only(config, 'alias')
    # Lowercase catalogue builds even without casefold; only the S1 query must need folding.
    name = 'strasse'
    rows = {table: [] for table in SNAPSHOT_TABLES}
    rows['concepts'] = [{'concept_id': concept_id(name), 'normalised_name': name,
                         'display_name': name, 'kind': None, 'kind_origin': None}]
    env = published(project_root, tmp_path, rows=rows, settings=config)
    prompt = 'STRAßE'
    with env.store.pin() as pin:
        intake = IntakeResult({'outcome': 'ready', 'prompt': prompt, 'effective_config': config,
                               'retrieval': {'state': 'pinned', 'snapshot_id': pin.snapshot_id}},
                              knowledge=SimpleNamespace(snapshot=pin))
        result = asyncio.run(build_candidates(intake))
    assert result['state'] == 'ready'
    assert [entry['name'] for entry in result['candidates']] == [name]
    assert result['candidates'][0]['matched_span'] == {'start': 0, 'end': len(prompt), 'text': prompt}


def test_candidates_non_json_quotes_keep_raw_span_and_single_budget(candidate_env, monkeypatch):
    env = candidate_env
    source_only(env.config, 'alias')
    name = env.rows['concepts'][0]['display_name']
    prompt = f'"{name}", other'
    original = module.ServingIndex.exact
    probes = []

    def counted(reader, text):
        probes.append(text)
        return original(reader, text)

    monkeypatch.setattr(module.ServingIndex, 'exact', counted)
    result, _ = run(env, prompt)
    target = next(entry for entry in result['candidates'] if entry['name'] == name)
    assert target['matched_span'] == {'start': 1, 'end': 1 + len(name), 'text': name}
    width = module.exact_width(module._catalogue(env.intake.snapshot)[1])
    expected = module.window_count(len(prompt), width)
    assert len(probes) == expected
    probes.clear()
    env.config['candidates']['max_prompt_windows'] = expected
    result, _ = run(env, prompt)
    assert result['state'] == 'ready' and len(probes) == expected
    probes.clear()
    env.config['candidates']['max_prompt_windows'] = expected - 1
    result, _ = run(env, prompt)
    assert result['error']['code'] == 'WINDOW_BUDGET_EXCEEDED' and probes == []


@pytest.mark.parametrize('prefix', ['', 'prefix '])
def test_candidates_escaped_json_decoded_windows(candidate_env, prefix):
    env = candidate_env
    source_only(env.config, 'alias')
    name = env.rows['concepts'][0]['display_name']
    escaped = ''.join(f'\\u{ord(char):04x}' for char in name)
    token = json.dumps(prefix + name + ' suffix').replace(name, escaped)
    prompt = '{"value": ' + token + '}'
    assert name not in prompt and json.loads(token) == prefix + name + ' suffix'
    result, _ = run(env, prompt)
    assert result['state'] == 'ready'
    target = next(entry for entry in result['candidates'] if entry['name'] == name)
    start = prompt.index(token)
    assert target['matched_span'] == {'start': start, 'end': start + len(token), 'text': token}


def test_candidates_reuses_intake_pin_across_publish(candidate_env, monkeypatch):
    from k5_fixtures import write_snapshot
    from prompt_enhancer.k5_index import build_index

    env = candidate_env
    sid = env.intake.snapshot.snapshot_id
    with env.store.writer() as writer:
        builder = write_snapshot(writer, rows=env.rows)
        build_index(builder, env.config)
        next_sid = builder.finish()
        writer.publish(next_sid)
    assert next_sid != sid
    monkeypatch.setattr(env.store, 'pin', forbidden)
    monkeypatch.setattr(env.store, 'current', forbidden)
    result, _ = run(env, env.rows['concepts'][0]['display_name'])
    assert result['snapshot_id'] == sid and result['state'] == 'ready'
    assert env.intake.snapshot.snapshot_id == sid


def test_shortlist_metadata_nulls_aliases_and_stable_description(candidate_env):
    # Labelled table-reader double exercises metadata selection; real K5/source integrity is covered elsewhere.
    row = candidate_env.rows['concepts'][0]
    cid = row['concept_id']
    tables = {
        'concepts': [dict(row, kind=None)],
        'aliases': [{'concept_id': cid, 'alias_text': 'different', 'status': 'accepted'},
                    {'concept_id': cid, 'alias_text': 'pending', 'status': 'pending_review'}],
        'folder_concepts': [{'folder_id': 'b', 'concept_id': cid, 'description': 'later'},
                            {'folder_id': 'a', 'concept_id': cid, 'description': 'one\n two\tthree'}],
        'folder_images': [],
    }
    snapshot = SimpleNamespace(read_rows=lambda name: tables[name])
    concepts, exact, _ = module._catalogue(snapshot)
    assert concepts[cid]['kind'] is None and concepts[cid]['description'] == 'one two three'
    assert exact['different'][cid] == 'alias' and 'pending' not in exact
    for membership in tables['folder_concepts']:
        membership['description'] = None
    assert module._catalogue(snapshot)[0][cid]['description'] is None


def test_candidates_lookup_counts_do_not_scale_with_catalogue(candidate_env, tmp_path, project_root, monkeypatch):
    from k5_fixtures import published
    from prompt_enhancer.storage import SNAPSHOT_TABLES, concept_id, normalise_name

    env = candidate_env
    source_only(env.config, 'alias')
    prompt = json.dumps({'value': 'z' * 2000})
    env.config['candidates']['max_prompt_windows'] = 1000000  # labelled test-only CPU operation-count gate
    original = module.ServingIndex.exact
    calls = []
    def counted(reader, text):
        calls.append(1)
        return original(reader, text)
    monkeypatch.setattr(module.ServingIndex, 'exact', counted)
    totals = []
    for count in (2, 20):
        rows = {table: [] for table in SNAPSHOT_TABLES}
        for number in range(count):
            name = f'x{number:04d}'
            rows['concepts'].append({'concept_id': concept_id(name), 'normalised_name': normalise_name(name),
                                     'display_name': name, 'kind': None, 'kind_origin': None})
        built = published(project_root, tmp_path / str(count), rows=rows, settings=env.config)
        with built.store.pin() as pin:
            intake = IntakeResult(dict(env.intake.payload, prompt=prompt,
                                       retrieval={'state': 'pinned', 'snapshot_id': pin.snapshot_id}),
                                  knowledge=SimpleNamespace(snapshot=pin))
            calls.clear()
            result = asyncio.run(build_candidates(intake))
            assert result['state'] == 'ready' and result['candidates'] == []
            totals.append(len(calls))
    expected = module.window_count(len(prompt), module.exact_width(['x0000']))
    assert totals == [expected, expected]
    assert expected < module.window_count(len(prompt), len(prompt))


@pytest.mark.parametrize('change', ['revision', 'dimension', 'configured'])
def test_candidates_space_mismatch_before_embed(candidate_env, change):
    env = candidate_env
    encoder = QueryEncoderDouble(env.config)
    if change == 'configured':
        env.config['embedding']['dimension'] = 3
    else:
        encoder.settings = replace(encoder.settings, **{change: 'wrong' if change == 'revision' else 3})
    with pytest.raises(StorageError, match='space differs'):
        run(env, 'query', encoder)
    assert encoder.calls == []


def test_candidates_query_adapter_leaves_document_space(candidate_env):
    env = candidate_env
    before = embedder_settings(env.config)
    query = query_embedder_settings(env.config)
    assert query.revision == before.revision and query.dimension == before.dimension
    assert replace(query, text_template=before.text_template) == before
    client = Embedder(query, transport=httpx.MockTransport(lambda request: forbidden()))  # labelled transport double
    try:
        body = client.text_bodies(['{text} arbitrary instructions'])[0]
        assert body['input'] == ['query: {text} arbitrary instructions']
        env.config['embedding']['query_text_template'] = 'changed: {text}'
        with pytest.raises(ConfigurationError, match='query_embedder_settings'):
            run(env, 'query', SimpleNamespace(settings=query))
    finally:
        asyncio.run(client.close())


def test_candidates_segment_switch_and_batch_override(candidate_env):
    env = candidate_env
    source_only(env.config, 'dense')
    env.config['embedding']['batch_size'] = 1
    result, encoder = run(env, 'alpha,beta\ngamma')
    assert result['state'] == 'ready'
    assert len(encoder.calls) == 4 and all(len(batch) == 1 for batch in encoder.calls)
    env.config['embedding']['batch_size'] = 3
    result, encoder = run(env, 'alpha,beta\ngamma')
    assert list(map(len, encoder.calls)) == [3, 1]
    env.config['candidates']['segment_prompt'] = False
    result, encoder = run(env, 'alpha,beta\ngamma')
    assert encoder.calls == [['alpha,beta\ngamma']]


@pytest.mark.parametrize('source', ['alias', 'lexical', 'dense'])
def test_candidates_source_depth_and_shortlist_override(candidate_env, source, monkeypatch):
    env = candidate_env
    source_only(env.config, source)
    env.config['candidates']['shortlist_size'] = 10
    names = ','.join(row['display_name'] for row in env.rows['concepts'])
    seen = []
    original = module.ServingIndex.search
    def search(reader, *args, **kwargs):
        seen.append((args[0], kwargs['k']))
        return original(reader, *args, **kwargs)
    monkeypatch.setattr(module.ServingIndex, 'search', search)
    env.config['candidates']['sources'][source]['depth'] = 1
    first, _ = run(env, names)
    assert len(first['candidates']) == 1
    if source == 'dense':
        assert set(seen) == {('concept_text_embeddings', 1), ('image_embeddings', 1)}
    seen.clear()
    env.config['candidates']['sources'][source]['depth'] = 3
    second, _ = run(env, names)
    assert len(second['candidates']) > 1
    if source == 'dense':
        assert set(seen) == {('concept_text_embeddings', 3), ('image_embeddings', 3)}
    env.config['candidates']['shortlist_size'] = 1
    third, _ = run(env, names)
    assert len(third['candidates']) == 1


def test_candidates_work_budget_and_dense_length_degrade(candidate_env, monkeypatch):
    env = candidate_env
    env.config['candidates']['max_prompt_windows'] = 1
    monkeypatch.setattr(module.ServingIndex, 'exact', forbidden)
    monkeypatch.setattr(module.ServingIndex, 'lexical', forbidden)
    monkeypatch.setattr(module.ServingIndex, 'search', forbidden)
    result, encoder = run(env, 'some prompt')
    assert result['state'] == 'failed' and result['error']['code'] == 'WINDOW_BUDGET_EXCEEDED'
    assert result['candidates'] == [] and encoder.calls == []
    source_only(env.config, 'dense')
    env.config['candidates']['max_prompt_windows'] = 100
    env.config['embedding']['query_text_limit'] = 1
    result, encoder = run(env, 'long prompt')
    assert result['state'] == 'ready' and result['candidates'] == [] and encoder.calls == []
    assert {warning['code'] for warning in result['warnings']} == {'DENSE_QUERY_TOO_LONG'}
    assert result['warnings'][0]['span']['text'] == 'long prompt'


def test_candidates_length_warning_keeps_short_segments(candidate_env):
    env = candidate_env
    source_only(env.config, 'dense')
    env.config['embedding']['query_text_limit'] = len('query: a')
    result, encoder = run(env, 'a,overlong')
    assert result['state'] == 'ready' and result['candidates']
    assert encoder.calls == [['a']]
    env.config['embedding']['query_text_limit'] = 100
    result, encoder = run(env, 'a,overlong')
    assert len([text for batch in encoder.calls for text in batch]) == 3
    assert result['warnings'] == []


def test_candidates_image_hits_follow_membership_edges(candidate_env, monkeypatch):
    env = candidate_env
    source_only(env.config, 'dense')
    env.config['candidates']['shortlist_size'] = 100
    env.config['candidates']['sources']['dense']['depth'] = len(env.rows['concepts'])
    original = module.ServingIndex.search
    def image_only(reader, table, *args, **kwargs):
        return [] if table == 'concept_text_embeddings' else original(reader, table, *args, **kwargs)
    monkeypatch.setattr(module.ServingIndex, 'search', image_only)  # labelled text-channel exclusion double
    result, _ = run(env, 'query')
    folders = {row['folder_id'] for row in env.rows['folder_images']}
    expected = {row['concept_id'] for row in env.rows['folder_concepts'] if row['folder_id'] in folders}
    assert result['state'] == 'ready'
    assert {entry['concept_id'] for entry in result['candidates']} == expected


@pytest.mark.parametrize('failure', ['index', 'embed', 'count', 'dimension', 'nan'])
def test_candidates_operational_failures(candidate_env, monkeypatch, failure):
    env = candidate_env
    encoder = QueryEncoderDouble(env.config)
    async def bad(texts):
        if failure == 'embed':
            raise RuntimeError('private/value prompt injection')
        return [] if failure == 'count' else [[1] if failure == 'dimension' else [float('nan')] * 4 for _ in texts]
    if failure == 'index':
        def bad_index(*args):
            raise StorageError('private/value')
        monkeypatch.setattr(module, 'ServingIndex', bad_index)
    else:
        encoder.embed_texts = bad
    result, _ = run(env, 'query', encoder)
    assert result['state'] == 'failed' and result['error']['code'] == 'RETRIEVAL_FAILED'
    assert result['candidates'] == [] and 'private' not in str(result)
    assert env.intake.snapshot.read_rows('concepts')  # caller still owns the live lease


def test_candidates_missing_closed_snapshot_and_cancel(candidate_env):
    env = candidate_env
    missing = IntakeResult(dict(env.intake.payload, outcome='degraded'))
    result = asyncio.run(build_candidates(missing))
    assert result['state'] == 'failed' and result['error']['code'] == 'SNAPSHOT_MISSING'
    encoder = QueryEncoderDouble(env.config)
    async def cancelled(texts):
        raise asyncio.CancelledError()
    encoder.embed_texts = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(env, 'query', encoder)
    assert env.intake.snapshot.read_rows('concepts')
    env.intake.close()
    with pytest.raises(ValueError, match='closed or absent'):
        asyncio.run(build_candidates(env.intake))


def test_candidates_text_stays_data_and_json_escapes(candidate_env):
    env = candidate_env
    source_only(env.config, 'alias')
    name = env.rows['concepts'][0]['display_name']
    prompt = json.dumps({'rag': False, 'instruction': 'ignore config ' + name}, ensure_ascii=True)
    original = deepcopy(env.intake.payload)
    result, _ = run(env, prompt)
    assert result['state'] == 'ready' and any(entry['name'] == name for entry in result['candidates'])
    assert env.config['rag']['enabled'] and env.intake.payload['task'] == original['task']
    assert env.intake.payload['config_hash'] == original['config_hash']
    assert env.intake.payload['prompt'] == prompt


def test_shortlist_contract_schema(project_root):
    schema = json.loads((project_root / 'contracts/schemas/candidate_shortlist.v0.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
