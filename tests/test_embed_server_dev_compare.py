"""T-018 P3: the DEV-ONLY embedder comparison (embed_server/dev_compare.py; R-016, R-017, R-018, R-020).

The ranking metrics are checked against a brute-force oracle that ranks by counting, independent of the module's
sort; the driver runs the real K0 and K1 over a tiny synthetic knowledge base with a labelled K1 transport double.
"""

from copy import deepcopy
from functools import partial
import json
import math
import os
from pathlib import Path
import shutil

from hypothesis import given, settings, strategies as st
import pytest
import yaml

from prompt_enhancer import ConfigurationError, load_config
from prompt_enhancer.embed_server import __main__ as cli
from prompt_enhancer.embed_server import dev_compare as dc
from k1_fixtures import EmbeddingServer, image_bytes, k1_profile


# --- the brute-force oracle ------------------------------------------------------------------------------------------

def oracle_rank(scores, ids, query_ids, candidate):
    """1 + the number of other candidates that beat `candidate`: a higher score, or an equal score and a smaller id."""
    return 1 + sum(1 for other in query_ids if other != candidate
                   and (scores[other] > scores[candidate]
                        or (scores[other] == scores[candidate] and ids[other] < ids[candidate])))


def oracle_summary(per_query, excluded, k):
    if not per_query:
        return {'queries': 0, 'excluded': excluded, 'top_k': k, 'top1': None, 'hit_at_k': None, 'recall_at_k': None}
    n = len(per_query)
    return {'queries': n, 'excluded': excluded, 'top_k': k,
            'top1': sum(1 for ranks, _ in per_query if 1 in ranks) / n,
            'hit_at_k': sum(1 for ranks, _ in per_query if min(ranks) <= k) / n,
            'recall_at_k': sum(sum(1 for r in ranks if r <= k) / min(k, size) for ranks, size in per_query) / n}


def oracle_leave_one_out(ids, scores, folders, k):
    per_query, excluded = [], 0
    for q in range(len(ids)):
        candidates = [j for j in range(len(ids)) if j != q]
        relevant = [j for j in candidates if folders[q] & folders[j]]
        if not relevant:
            excluded += 1
            continue
        per_query.append(([oracle_rank(scores[q], ids, candidates, j) for j in relevant], len(relevant)))
    return oracle_summary(per_query, excluded, k)


def oracle_name_to_image(scores, relevant, ids, k):
    per_query, excluded = [], 0
    for c, images in enumerate(relevant):
        if not images:
            excluded += 1
            continue
        per_query.append(([oracle_rank(scores[c], ids, range(len(ids)), j) for j in images], len(images)))
    return oracle_summary(per_query, excluded, k)


def close(a, b):
    return a.keys() == b.keys() and all(
        (a[key] is None and b[key] is None) or (a[key] is not None and b[key] is not None
                                                 and math.isclose(a[key], b[key], abs_tol=1e-12)) for key in a)


@st.composite
def ranking_case(draw):
    """Small integer scores force ties; ids are distinct and not in index order, so the tie rule is exercised."""
    n = draw(st.integers(2, 9))
    ids = draw(st.lists(st.text('abcdef', min_size=1, max_size=3), min_size=n, max_size=n, unique=True))
    scores = [[draw(st.integers(0, 3)) for _ in range(n)] for _ in range(n)]
    folders = [set(draw(st.sets(st.sampled_from('wxyz'), min_size=1, max_size=2))) for _ in range(n)]
    return ids, scores, folders, draw(st.integers(1, n + 1))


@settings(max_examples=400, deadline=None)
@given(ranking_case())
def test_property_leave_one_out_equals_the_brute_force_oracle(case):
    ids, scores, folders, k = case
    assert close(dc.leave_one_out(ids, scores, folders, k), oracle_leave_one_out(ids, scores, folders, k))


@settings(max_examples=400, deadline=None)
@given(ranking_case(), st.data())
def test_property_name_to_image_equals_the_brute_force_oracle(case, data):
    ids, _, _, k = case
    names = data.draw(st.integers(1, 4))
    scores = [[data.draw(st.integers(0, 3)) for _ in ids] for _ in range(names)]
    relevant = [data.draw(st.sets(st.integers(0, len(ids) - 1), max_size=len(ids))) for _ in range(names)]
    summary, rows = dc.name_to_image([{'concept_id': f'c{c}'} for c in range(names)], scores, relevant, ids, k)
    assert close(summary, oracle_name_to_image(scores, relevant, ids, k))
    assert [row['concept_id'] for row in rows] == [f'c{c}' for c in range(names) if relevant[c]]
    for row in rows:
        c = int(row['concept_id'][1:])
        assert row['first_relevant_rank'] == min(oracle_rank(scores[c], ids, range(len(ids)), j) for j in relevant[c])


# --- hand-computed fixtures ------------------------------------------------------------------------------------------

def test_leave_one_out_by_hand():
    ids = ['a', 'b', 'c', 'd', 'e']
    folders = [{'F'}, {'F'}, {'G'}, {'G'}, {'H'}]  # e has no same-folder peer: excluded
    scores = [[9, 5, 6, 1, 0],   # a: c (6) before b (5): top1 miss, b at rank 2
              [5, 9, 1, 1, 2],   # b: a first
              [6, 1, 9, 6, 0],   # c: a and d tie at 6, 'a' < 'd': a first (miss), d second
              [1, 1, 6, 9, 0],   # d: c first
              [0, 0, 0, 0, 9]]
    k1 = dc.leave_one_out(ids, scores, folders, 1)
    assert k1 == {'queries': 4, 'excluded': 1, 'top_k': 1, 'top1': 0.5, 'hit_at_k': 0.5, 'recall_at_k': 0.5}
    k2 = dc.leave_one_out(ids, scores, folders, 2)
    assert k2['hit_at_k'] == 1.0 and k2['recall_at_k'] == 1.0  # min(k, 1) = 1 same-folder image each
    assert dc.leave_one_out(['a'], [[1]], [{'F'}], 3)['queries'] == 0  # nobody to rank


def test_recall_is_divided_by_the_smaller_of_k_and_the_relevant_count():
    ids = ['q', 'r1', 'r2', 'r3', 'x']
    summary, rows = dc.name_to_image([{'concept_id': 'c'}], [[9, 8, 1, 0, 7]], [{1, 2, 3}], ids, 2)
    # top 2 = q (9), r1 (8): one of the three relevant images; min(2, 3) = 2 -> recall 0.5 (plain recall: 1/3)
    assert summary['recall_at_k'] == 0.5 and summary['hit_at_k'] == 1.0 and summary['top1'] == 0.0
    assert rows[0]['first_relevant_rank'] == 2 and rows[0]['images'] == 3


def test_measure_on_axis_vectors_and_the_k0_rows():
    """Exact cosines: images on axes, the folder layout and names from K0-shaped rows."""
    image_vectors = {'sha256:a': [1.0, 0.0, 0.0], 'sha256:b': [2.0, 0.1, 0.0], 'sha256:c': [0.0, 1.0, 0.0],
                     'sha256:d': [0.0, 3.0, 0.2]}
    rows = {'folder_images': [{'folder_id': 'F', 'image_id': 'sha256:a'}, {'folder_id': 'F', 'image_id': 'sha256:b'},
                              {'folder_id': 'G', 'image_id': 'sha256:c'}, {'folder_id': 'G', 'image_id': 'sha256:d'},
                              {'folder_id': 'G', 'image_id': 'sha256:unembedded'}],
            'folder_concepts': [{'folder_id': 'F', 'concept_id': 'concept:f'},
                                {'folder_id': 'G', 'concept_id': 'concept:g'}],
            'concepts': [{'concept_id': 'concept:f', 'display_name': 'Ef'},
                         {'concept_id': 'concept:g', 'display_name': 'Gee'},
                         {'concept_id': 'concept:none', 'display_name': 'No vector'}]}
    names = {'concept:f': [0.9, 0.1, 0.0], 'concept:g': [1.0, 0.2, 0.0]}  # g's name points at folder F
    loo, named, per_name = dc.measure(rows, image_vectors, names, 1)
    assert loo == {'queries': 4, 'excluded': 0, 'top_k': 1, 'top1': 1.0, 'hit_at_k': 1.0, 'recall_at_k': 1.0}
    assert named['queries'] == 2 and named['top1'] == 0.5 and named['hit_at_k'] == 0.5
    assert [(row['name'], row['top1'], row['first_relevant_rank']) for row in per_name] == [('Ef', True, 1),
                                                                                            ('Gee', False, 3)]


@pytest.mark.parametrize('vectors', [{'sha256:a': [0.0, 0.0]}, {}, {'sha256:a': [1.0, 0.0], 'sha256:b': [1.0]},
                                     {'sha256:a': [1.5e308, 1.5e308]}])
def test_measure_refuses_vectors_that_cannot_be_ranked(vectors):
    with pytest.raises(dc.DevCompareError):
        dc.measure({'folder_images': [], 'folder_concepts': [], 'concepts': []}, vectors, {}, 1)


def test_unit_rows_normalise_subnormal_rows_accurately():
    """F3 class (P3 fix round 1): a subnormal row was divided by its rounded length (5e-324 for [5e-324] * 2)."""
    rows = dc.unit_rows([[5e-324, 5e-324], [1.0, 0.0], [1e154, 1e154]])
    assert rows.ravel().tolist() == pytest.approx([0.7071067811865476] * 2 + [1.0, 0.0] + [0.7071067811865476] * 2,
                                                  abs=1e-15)
    assert float(rows[0] @ rows[1]) == pytest.approx(0.7071067811865476, abs=1e-15)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.floats(-1, 1, allow_nan=False), min_size=1, max_size=6), st.integers(-1074, 1023))
def test_property_unit_rows_equal_the_reference_unit_vector(vector, exponent):
    from prompt_enhancer.embed_server import reference as ref

    scaled = [math.ldexp(value, exponent) for value in vector]
    if not ref.usable(ref.vector_norm(scaled)):
        with pytest.raises(dc.DevCompareError):
            dc.unit_rows([scaled])
        return
    row = dc.unit_rows([scaled])[0]
    assert row.tolist() == pytest.approx(ref.unit_vector(scaled), abs=1e-15)
    assert abs(math.hypot(*row.tolist()) - 1.0) <= 1e-15


def test_measure_never_allocates_a_pairwise_matrix():
    """F1 (P3 fix round 1), bounded-memory instrument: `tracemalloc` traces NumPy's buffers (probe: a 600 x 600
    float64 product showed 2,880,160 B traced for its 2,880,000 B buffer, NumPy 2.5.5), so a full N x N score
    matrix (N^2 * 8 B) cannot hide from the peak; measured inside `measure` after the inputs exist."""
    import tracemalloc

    n = 600
    image_vectors = {f'sha256:{i:04d}': [math.cos(i), math.sin(i), (i % 7) / 7] for i in range(n)}
    rows = {'folder_images': [{'folder_id': f'F{i % 20}', 'image_id': f'sha256:{i:04d}'} for i in range(n)],
            'folder_concepts': [{'folder_id': f'F{c}', 'concept_id': f'concept:{c}'} for c in range(20)],
            'concepts': [{'concept_id': f'concept:{c}', 'display_name': f'n{c}'} for c in range(20)]}
    names = {f'concept:{c}': [math.sin(c), math.cos(c), 0.5] for c in range(20)}
    dc.measure(rows, dict(list(image_vectors.items())[:3]), names, 1)  # warm-up: module imports stay out of the peak
    tracemalloc.start()
    try:
        loo, named, _ = dc.measure(rows, image_vectors, names, 5)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert loo['queries'] == n and named['queries'] == 20
    assert peak < n * n * 8 / 4, peak  # a pairwise float64 matrix alone is 2,880,000 B


@pytest.mark.parametrize('k', [0, -1, True, 1.0, None])
def test_top_k_must_be_a_positive_integer(k):
    with pytest.raises(dc.DevCompareError, match='--top-k'):
        dc.check_top_k(k)


# --- the profile: `embed dev-compare` --------------------------------------------------------------------------------
# Every test has its own knowledge base, store and outputs below `tmp_path` (inside the session's project root), so
# K1's checkpoints never leak from one test into another.

def kb(base, separator='|'):
    """Three concept folders (two named, one without an info file) of tiny distinct PNGs."""
    root = base / 'kb'
    layout = {'A': (f'Alpha{separator}Alef', [(10, 20, 30), (12, 22, 32), (14, 20, 30)]),
              'B': ('Beta', [(200, 40, 9), (190, 50, 9)]), 'C': (None, [(5, 250, 5), (9, 240, 5)])}
    for folder, (names, colors) in layout.items():
        (root / folder).mkdir(parents=True, exist_ok=True)
        if names is not None:
            (root / folder / 'info.txt').write_text(f'{names}\ndescription', encoding='utf-8')
        for index, color in enumerate(colors):
            (root / folder / f'{folder.lower()}{index}.png').write_bytes(image_bytes(size=(6 + index, 4), color=color))
    return root


def rel(project_root, path):
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def dev_profile(model_test_profile, project_root, base, **embedding):
    profile = k1_profile(model_test_profile, **embedding)
    profile['knowledge'].update(root=rel(project_root, base / 'kb'), concept_separator='|')
    profile['knowledge']['info_file'].update(names=['info.txt'], encoding='utf-8')
    profile['storage'].update(kb_store=rel(project_root, base / 'data' / 'kb'), lock={'timeout_s': 5, 'poll_s': 0.05})
    profile['sync']['mass_deletion_guard']['max_removed_fraction'] = 1.0  # test-only: a fresh store
    profile['runtime']['cpu_workers'] = 1
    return profile


def write(base, profile, name='dc.yaml'):
    path = base / name
    path.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return path


_DELETE = object()


def _set(profile, key, value):
    node = profile
    parts = key.split('.')
    for part in parts[:-1]:
        node = node[part]
    if value is _DELETE:
        node.pop(parts[-1])
    else:
        node[parts[-1]] = value


@pytest.mark.parametrize('key, value, message', [
    ('knowledge.root', 'deploy_required', 'knowledge.root'),
    ('knowledge.info_file.names', 'deploy_required', 'knowledge.info_file.names'),
    ('knowledge.info_file.encoding', 'no-such-codec', 'unknown codec'),
    ('knowledge.image_extensions', ['png'], 'knowledge.image_extensions'),
    ('knowledge.concept_separator', 'design_required', 'knowledge.concept_separator'),
    ('knowledge.concept_separator', _DELETE, 'concept_separator'),
    ('runtime.cpu_workers', 0, 'runtime.cpu_workers'),
    ('sync.mass_deletion_guard.max_removed_fraction', 'design_required', 'max_removed_fraction'),
    ('storage.kb_store', 'OVERLAP', 'overlaps knowledge.root'),
    ('storage.lock.timeout_s', 'deploy_required', 'storage.lock'),
    ('storage.table_format', 'csv', 'storage.table_format'),
    ('sync.resume', 'deploy_required', 'sync.resume'),
    ('embedding.batch_size', 'fit_from_bench', 'embedding.batch_size'),
    ('vision.quality', 'design_required', 'vision.quality'),
    ('vlm.backends.embedder.base_urls', ['deploy_required'], 'base_urls'),
])
def test_every_key_dev_compare_reads_is_refused_missing_or_unresolved(project_root, tmp_path, model_test_profile, key,
                                                                      value, message):
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    _set(profile, key, rel(project_root, tmp_path / 'kb' / 'store') if value == 'OVERLAP' else value)
    with pytest.raises(ConfigurationError, match=message):
        load_config(write(tmp_path, profile), dc.COMMAND, project_root=project_root)


@pytest.mark.parametrize('key, value', [('storage.data_root', 'deploy_required'),
                                        ('vlm.roles.observer.max_tokens', 'deploy_required'),
                                        ('observe.near_duplicate_cosine_distance', 'fit_from_bench'),
                                        ('runtime.gpus', 'deploy_required'),
                                        ('embedding.query_text_limit', 'design_required')])  # a query-side key
def test_keys_outside_the_read_set_are_not_read(project_root, tmp_path, model_test_profile, key, value):
    """The exact read set: K2-K5, the data root, the model roles and the GPU stay unread (no hidden requirement)."""
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    _set(profile, key, value)
    assert load_config(write(tmp_path, profile), dc.COMMAND, project_root=project_root)


# --- the driver end to end -------------------------------------------------------------------------------------------

_real_run = dc.run_dev_compare


def run_cli(project_root, base, profile, monkeypatch, server, out='out', k=2, name='dc.yaml'):
    """The command line with K1's transport replaced by the labelled `EmbeddingServer` double."""
    path = write(base, profile, name)
    monkeypatch.setattr(dc, 'run_dev_compare', partial(_real_run, transport=server.transport()))
    return cli.main(['dev-compare', '--config', str(path), '--project-root', str(project_root),
                     '--out', str(base / out), '--top-k', str(k)])


def report_of(base, out='out'):
    return json.loads((base / out / 'compare.json').read_text(encoding='utf-8'))


def _cosine(a, b):
    return math.fsum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))


def _stored(project_root, base, config):
    """K0's rows and K1's own vectors, read back through the production functions from finished checkpoints."""
    import asyncio

    from prompt_enhancer.k0_sync import sync
    from prompt_enhancer.k1_embed import ConceptText, embed_concept_texts, embed_images
    from prompt_enhancer.storage import open_knowledge_store

    store = open_knowledge_store(config, dc.COMMAND, project_root=project_root)
    scanned = sync(store, base / 'kb', info_names=['info.txt'], encoding='utf-8', extensions=['.png'],
                   separator='|', workers=1, threshold=1.0, plan=True)
    unused = EmbeddingServer(status=500)  # any request would fail: everything must come from the checkpoints
    images = asyncio.run(embed_images(store, config, scanned['handoff']['k1'], knowledge_root=base / 'kb',
                                      transport=unused.transport()))
    items = [ConceptText(row['concept_id'], 'name', row['display_name']) for row in scanned['rows']['concepts']]
    texts = asyncio.run(embed_concept_texts(store, config, items, transport=unused.transport()))
    assert unused.requests == []
    return (scanned['rows'], {row['image_id']: row['vector'] for row in images['image_embeddings'].to_pylist()},
            {row['concept_id']: row['vector'] for row in texts['concept_text_embeddings'].to_pylist()})


def test_the_driver_runs_k0_and_k1_and_its_metrics_equal_the_oracle(project_root, tmp_path, model_test_profile,
                                                                     monkeypatch, capsys):
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    done = run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer())
    captured = capsys.readouterr()
    assert done == 0, captured
    report = report_of(tmp_path)
    assert report['format'] == dc.FORMAT and 'smoke-only' in report['label'] and report['top_k'] == 2
    counts = report['counts']
    assert counts['folders'] == 3 and counts['images'] == 7 and counts['images_embedded'] == 7
    assert counts['names'] == 3 and counts['texts_embedded'] == 3  # Alpha, Alef, Beta: C has no info file
    assert counts['folders_unnamed'] == 1  # F6: C, left out of the name metric and counted
    config = load_config(tmp_path / 'dc.yaml', dc.COMMAND, project_root=project_root)
    rows, vectors, texts = _stored(project_root, tmp_path, config)
    ids = sorted(vectors)
    folders = [{row['folder_id'] for row in rows['folder_images'] if row['image_id'] == image_id} for image_id in ids]
    scores = [[_cosine(vectors[a], vectors[b]) for b in ids] for a in ids]
    assert close(report['leave_one_out'], oracle_leave_one_out(ids, scores, folders, 2))
    assert report['leave_one_out']['queries'] == 7  # C's images still have a same-folder peer
    names = [row for row in rows['concepts'] if row['concept_id'] in texts]
    owner = {row['concept_id']: {r['folder_id'] for r in rows['folder_concepts'] if r['concept_id'] == row['concept_id']}
             for row in names}
    relevant = [{index for index in range(len(ids)) if folders[index] & owner[row['concept_id']]} for row in names]
    text_scores = [[_cosine(texts[row['concept_id']], vectors[b]) for b in ids] for row in names]
    assert close(report['name_to_image'], oracle_name_to_image(text_scores, relevant, ids, 2))
    assert sorted(row['name'] for row in report['per_name']) == ['Alef', 'Alpha', 'Beta']
    line = json.loads(captured.out)
    assert line['step'] == 'dev-compare' and line['leave_one_out'] == report['leave_one_out']
    assert line['out'] == rel(project_root, tmp_path / 'out')


def test_a_rerun_embeds_nothing_and_reports_the_same_metrics(project_root, tmp_path, model_test_profile, monkeypatch,
                                                             capsys):
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    assert run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer(), out='out-1') == 0
    again = EmbeddingServer()
    assert run_cli(project_root, tmp_path, profile, monkeypatch, again, out='out-2') == 0, capsys.readouterr()
    first, second = report_of(tmp_path, 'out-1'), report_of(tmp_path, 'out-2')
    assert again.requests == [] and second['counts']['images_skipped_done'] == 7  # R-017: per content hash
    assert second['counts']['images_embedded'] == 0 and second['seconds']['per_embedded_image'] is None
    assert second['leave_one_out'] == first['leave_one_out'] and second['name_to_image'] == first['name_to_image']


def test_with_resume_off_a_rerun_embeds_again(project_root, tmp_path, model_test_profile, monkeypatch, capsys):
    """`sync.resume: false` (the store's setting): another run's finished items are not skipped."""
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    profile['sync']['resume'] = False
    assert run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer(), out='out-1') == 0
    again = EmbeddingServer()
    assert run_cli(project_root, tmp_path, profile, monkeypatch, again, out='out-2') == 0, capsys.readouterr()
    assert report_of(tmp_path, 'out-2')['counts']['images_embedded'] == 7 and again.requests


@pytest.mark.parametrize('batch_size, counts', [(2, [1, 1, 2, 2, 2, 2]), (3, [1, 3, 3, 3])])
def test_the_profile_batch_size_shapes_the_requests(project_root, tmp_path, model_test_profile, monkeypatch, capsys,
                                                    batch_size, counts):
    kb(tmp_path)
    server = EmbeddingServer()
    profile = dev_profile(model_test_profile, project_root, tmp_path, batch_size=batch_size)
    assert run_cli(project_root, tmp_path, profile, monkeypatch, server) == 0, capsys.readouterr()
    assert sorted(server.input_counts()) == counts  # 7 images, then 3 names, in batches of the profile's size


@pytest.mark.parametrize('top_k', [1, 3])
def test_the_command_line_k_is_the_k_of_the_report(project_root, tmp_path, model_test_profile, monkeypatch, capsys,
                                                   top_k):
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    assert run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer(), k=top_k) == 0, capsys.readouterr()
    report = report_of(tmp_path)
    assert report['top_k'] == report['leave_one_out']['top_k'] == report['name_to_image']['top_k'] == top_k


def test_the_profile_separator_decides_the_names(project_root, tmp_path, model_test_profile, monkeypatch, capsys):
    kb(tmp_path, separator=';')
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    profile['knowledge']['concept_separator'] = ';'
    assert run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer()) == 0, capsys.readouterr()
    assert sorted(row['name'] for row in report_of(tmp_path)['per_name']) == ['Alef', 'Alpha', 'Beta']
    profile['knowledge']['concept_separator'] = '|'  # the same info file read with another separator: one name
    assert run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer(), out='out-pipe',
                   name='dc-pipe.yaml') == 0, capsys.readouterr()
    assert sorted(row['name'] for row in report_of(tmp_path, 'out-pipe')['per_name']) == ['Alpha;Alef', 'Beta']


def test_images_k1_could_not_embed_are_recorded_then_refused(project_root, tmp_path, model_test_profile, monkeypatch,
                                                             capsys):
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    done = run_cli(project_root, tmp_path, profile, monkeypatch, EmbeddingServer(status=500))
    captured = capsys.readouterr()
    assert done == cli.REFUSED and captured.err.startswith('refused: K1 left 7 image(s) and 3 name(s)'), captured
    report = report_of(tmp_path)
    assert report['counts']['images_failed'] == 7 and report['leave_one_out'] is None


class TextsFail(EmbeddingServer):
    """Labelled test double: images are embedded, every text request fails."""

    def handler(self, request):
        import httpx

        if all(isinstance(item, str) for item in json.loads(request.content)['input']):
            return httpx.Response(500, json={'error': 'test double'})
        return super().handler(request)


def test_names_k1_could_not_embed_are_recorded_in_the_report_then_refused(project_root, tmp_path, model_test_profile,
                                                                         monkeypatch, capsys):
    kb(tmp_path)
    profile = dev_profile(model_test_profile, project_root, tmp_path)
    done = run_cli(project_root, tmp_path, profile, monkeypatch, TextsFail())
    captured = capsys.readouterr()
    assert done == cli.REFUSED and 'name(s) unembedded' in captured.err, captured
    report = report_of(tmp_path)
    assert report['counts']['texts_failed'] == 3 and set(report['failed']['texts'].values()) == {'HTTP_ERROR'}
    assert report['name_to_image']['queries'] == 0 and report['leave_one_out']['queries'] == 7


@pytest.mark.parametrize('option, message', [('--top-k', '--top-k'), ('--out', '--out')])
def test_the_command_refuses_before_touching_the_store(project_root, tmp_path, model_test_profile, monkeypatch,
                                                       capsys, option, message):
    kb(tmp_path)
    path = write(tmp_path, dev_profile(model_test_profile, project_root, tmp_path))
    called = []
    monkeypatch.setattr(dc, 'run_dev_compare', lambda *args, **kwargs: called.append(args))
    argv = {'--config': str(path), '--project-root': str(project_root), '--out': str(tmp_path / 'out'), '--top-k': '2'}
    argv[option] = '0' if option == '--top-k' else str(project_root.parent / 'elsewhere')
    done = cli.main(['dev-compare', *[part for pair in argv.items() for part in pair]])
    captured = capsys.readouterr()
    assert done == cli.REFUSED and message in captured.err and called == [], captured
    assert not (tmp_path / 'data').exists()


@pytest.fixture
def own_root(tmp_path):
    """A synthetic project root of this test (the schemas copied, as conftest does), so its protected directories are
    empty test directories, never the session root's."""
    root = tmp_path / 'root'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'contracts' / 'schemas', root / 'contracts' / 'schemas')
    return root.resolve()


def _link(target, link):
    """A directory junction on Windows (no privilege needed), a symlink elsewhere; removed as a link by `_unlink`."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f'synthetic symlink creation unavailable: {error}')


def _unlink(link):
    os.rmdir(link) if os.name == 'nt' else link.unlink()


# (label, directory the out dir aims at, relative out spelling or None for a link alias, expected message)
OUT_CASES = [('the knowledge root', 'kb', 'kb', 'inside knowledge.root'),
             ('a folder of it', 'kb', 'kb/A', 'inside knowledge.root'),
             ('a dot-dot spelling', 'kb', 'kb/../kb/A', 'inside knowledge.root'),
             ('a link to it', 'kb', None, 'inside knowledge.root'),
             ('the store', 'data/kb', 'data/kb/reports', 'inside storage.kb_store'),
             ('a link to the store', 'data/kb', None, 'inside storage.kb_store'),
             *[(f'{name}', name, f'{name}/out', 'in a protected project directory')
               for name in ('models', '.secrets', '.git', '.venv', '.venv-gpu')],
             *[(f'a link to {name}', name, None, 'in a protected project directory')
               for name in ('models', '.secrets', '.git', '.venv', '.venv-gpu')]]
if os.name == 'nt':  # Windows paths are case-insensitive: another case is the same directory
    OUT_CASES.append(('another case', 'kb', 'KB/A', 'inside knowledge.root'))


@pytest.mark.parametrize('label, aimed, spelling, message', OUT_CASES, ids=[case[0] for case in OUT_CASES])
def test_an_out_dir_in_the_source_the_store_or_a_protected_directory_is_refused_before_any_activity(
        own_root, model_test_profile, monkeypatch, capsys, label, aimed, spelling, message):
    """F2 (P3 fix round 1, R-014): the report directory, after resolving links, never lies in the read-only knowledge
    root, the store or a protected project directory; refused before the store is opened or a request is sent."""
    kb(own_root)
    profile = dev_profile(model_test_profile, own_root, own_root)
    for name in ('data/kb', 'models', '.secrets', '.git', '.venv', '.venv-gpu'):
        (own_root / name).mkdir(parents=True, exist_ok=True)
    before = sorted(path.relative_to(own_root) for path in own_root.rglob('*'))
    link = own_root / 'runs' / 'alias'
    if spelling is None:
        _link(own_root / aimed, link)
        out = str(link / 'A')
    else:
        out = str(own_root) + '/' + spelling
    server = EmbeddingServer()
    try:
        path = write(own_root, profile)
        monkeypatch.setattr(dc, 'run_dev_compare', partial(_real_run, transport=server.transport()))
        done = cli.main(['dev-compare', '--config', str(path), '--project-root', str(own_root), '--out', out,
                         '--top-k', '2'])
    finally:
        if spelling is None:
            _unlink(link)
    captured = capsys.readouterr()
    assert done == cli.REFUSED and captured.err == f'refused: --out lies {message} (after resolving links)\n', captured
    assert server.requests == []
    added = {path.relative_to(own_root) for path in own_root.rglob('*')} - set(before)
    assert added <= {Path('dc.yaml'), Path('runs')}, added  # no store file, no report, no folder anywhere


def test_an_out_dir_beside_the_source_is_written(own_root, model_test_profile, monkeypatch, capsys):
    """The override of the refusal above: `runs/` next to the knowledge root and the store is an allowed report
    directory."""
    kb(own_root)
    assert run_cli(own_root, own_root, dev_profile(model_test_profile, own_root, own_root), monkeypatch,
                   EmbeddingServer(), out='runs/compare') == 0, capsys.readouterr()
    assert report_of(own_root, 'runs/compare')['counts']['folders_unnamed'] == 1


def test_a_report_io_error_is_refused_without_its_path(project_root, tmp_path, model_test_profile, monkeypatch,
                                                       capsys):
    """F4 (P3 fix round 1), labelled I/O fault double: the OS refuses the report write, naming the absolute path;
    the command prints the fixed basename and the exception type only."""
    from prompt_enhancer.storage import _anchored

    def failing(root, relative, data, check=None):
        raise PermissionError(13, 'synthetic report I/O error', str(Path(root) / relative))

    kb(tmp_path)
    monkeypatch.setattr(_anchored, 'atomic_write_bytes', failing)
    done = run_cli(project_root, tmp_path, dev_profile(model_test_profile, project_root, tmp_path), monkeypatch,
                   EmbeddingServer())
    captured = capsys.readouterr()
    assert done == cli.REFUSED and captured.err == 'refused: cannot write compare.json (PermissionError)\n', captured
    written = str((tmp_path / 'out' / 'compare.json').resolve())
    assert written not in captured.err and repr(written) not in captured.err


def test_the_command_is_marked_dev_only(capsys):
    with pytest.raises(SystemExit):
        cli.main(['--help'])
    assert 'DEV-ONLY' in capsys.readouterr().out
    assert dc.__doc__.startswith('DEV-ONLY') and 'never a' in dc.__doc__


def test_an_unexpected_error_is_one_failed_line_with_the_type_only(project_root, tmp_path, model_test_profile,
                                                                   monkeypatch, capsys):
    kb(tmp_path)
    path = write(tmp_path, dev_profile(model_test_profile, project_root, tmp_path))

    def broken(*args, **kwargs):
        raise KeyError('secret-content')

    monkeypatch.setattr(dc, 'run_dev_compare', broken)
    done = cli.main(['dev-compare', '--config', str(path), '--project-root', str(project_root), '--out',
                     str(tmp_path / 'out'), '--top-k', '1'])
    captured = capsys.readouterr()
    assert done == cli.FAILED and captured.err == 'failed: KeyError\n', captured


def test_the_profile_helper_never_edits_the_session_profile(model_test_profile, project_root, tmp_path):
    before = deepcopy(model_test_profile)
    dev_profile(model_test_profile, project_root, tmp_path)
    assert model_test_profile == before
