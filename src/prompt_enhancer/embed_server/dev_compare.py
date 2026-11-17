"""DEV-ONLY comparison of one embedder on the dev knowledge base (T-018 P3; R-016, R-017, R-018, R-020).

Smoke-only: the numbers show that the K0 -> K1 -> server path works end to end on the dev machine. They are never a
threshold, a calibration, a bake-off result or a quality claim (R-020, gpu-live-run preflight 6), and nothing here
writes a profile value or a sentinel. The choice of `embedding.chosen` stays with experiment E5.

The flow uses the production entry points; this module adds only the ranking metrics:

- K0 `k0_sync.sync` scans `knowledge.root` into `storage.kb_store` (unpublished, with K0's own resumable caches),
- K1 part 1 `k1_embed.embed_images` embeds every image through the configured embedder backend,
- K1 part 2 `k1_embed.embed_concept_texts` embeds each K0 declared name unchanged, as the production `name` concept
  text (`kb_sync.concept_texts`); K1's checkpoints make a rerun skip what is done (R-017),
- the metrics rank the stored vectors, and `compare.json` is written below the project root through
  `reference._write` (the anchored storage layer).

Metrics, k = `--top-k` (a measurement-procedure value recorded in the report):

- leave-one-out over images: every image with at least one other image sharing a K0 folder ranks all other images;
  `top1` is the share whose nearest other image shares a folder, `hit_at_k` the share with at least one such image in
  its top k, `recall_at_k` the mean of `|top-k ∩ same-folder| / min(k, |same-folder others|)` (PM answer 2);
- name to image: every K0 declared name with at least one embedded image ranks all images; the same three values,
  with "relevant" meaning an image of a folder that declares the name.

Ranking: descending cosine, ties by `image_id` (deterministic). Cosines are dot products of unit vectors, each scaled
by its largest magnitude before its length is taken (`unit_rows`). Scores are computed and ranked one query at a
time, so memory grows with the stored vectors and O(N) per query, never with N x N or names x N (fix round 1, F1).
`compare.json` is refused inside the knowledge root, the store or a protected project directory (F2).
"""

import asyncio
import math
from pathlib import Path
import time

FORMAT = 'embed_dev_compare.v0'  # artifact-bound: layout of compare.json
LABEL = 'smoke-only, dev machine: never a threshold, a calibration or a quality claim (R-020)'
NAME_KIND = 'name'  # protocol-bound: K1 TEXT_KIND of a concept's display name (kb_sync.concept_texts)
COMMAND = 'embed dev-compare'  # protocol-bound: the loader command whose read set this run uses


class DevCompareError(ValueError):
    """The inputs cannot support the comparison, or K1 left items unembedded."""


def check_top_k(top_k):
    if type(top_k) is not int or top_k < 1:
        raise DevCompareError('--top-k must be a positive integer')


def unit_rows(vectors):
    """Row-normalised float64 matrix of `vectors` (a sequence of rows or a 2-D array); a row without a usable length
    is refused. Each row is scaled by its largest magnitude before its length is taken, as `reference.unit_vector`
    does, so a subnormal row is normalised accurately and no length overflows (P3 fix round 1, F3)."""
    import numpy as np

    if len(vectors) and len({len(vector) for vector in vectors}) != 1:
        raise DevCompareError('the stored vectors differ in length')
    matrix = np.array(vectors, dtype=np.float64)
    if matrix.ndim != 2 or not matrix.size or not np.isfinite(matrix).all():
        raise DevCompareError('no vectors, or a stored vector with a zero or non-finite length')
    largest = np.abs(matrix).max(axis=1)
    if not (largest > 0).all():
        raise DevCompareError('no vectors, or a stored vector with a zero or non-finite length')
    matrix /= largest[:, None]
    lengths = np.linalg.norm(matrix, axis=1)  # each scaled length lies in [1, sqrt(d)]
    with np.errstate(over='ignore'):
        if not np.isfinite(largest * lengths).all():  # the true length overflows: not usable (`reference.usable`)
            raise DevCompareError('no vectors, or a stored vector with a zero or non-finite length')
    matrix /= lengths[:, None]
    return matrix


class _Ranker:
    """Ranks one row of scores at a time: descending score, ties by id. Memory per query is O(N), never a pairwise
    matrix (P3 fix round 1, F1); the order equals sorting by `(-score, id)`."""

    def __init__(self, ids):
        import numpy as np

        self._np = np
        self._id_rank = np.empty(len(ids), dtype=np.int64)
        self._id_rank[sorted(range(len(ids)), key=ids.__getitem__)] = np.arange(len(ids))

    def __call__(self, scores, relevant, top_k, exclude=None):
        """`relevant` a boolean mask over the candidates; returns top1, hit, recall and the first relevant rank."""
        np = self._np
        order = np.lexsort((self._id_rank, -np.asarray(scores, dtype=np.float64)))
        if exclude is not None:
            order = order[order != exclude]
        hits = relevant[order]
        found = int(hits[:top_k].sum())
        return {'top1': bool(hits[0]), 'hit': found > 0, 'recall': found / min(top_k, int(relevant.sum())),
                'first_relevant_rank': int(np.argmax(hits)) + 1}


class _Totals:
    """Running counts of the per-query results; the recall terms are kept (one float per query) for an exact
    `math.fsum`, never the per-query result rows."""

    def __init__(self, top_k):
        self.top_k, self.count, self.excluded, self.top1, self.hits, self.recalls = top_k, 0, 0, 0, 0, []

    def add(self, result):
        self.count += 1
        self.top1 += result['top1']
        self.hits += result['hit']
        self.recalls.append(result['recall'])

    def summary(self):
        if not self.count:
            return {'queries': 0, 'excluded': self.excluded, 'top_k': self.top_k, 'top1': None, 'hit_at_k': None,
                    'recall_at_k': None}
        return {'queries': self.count, 'excluded': self.excluded, 'top_k': self.top_k, 'top1': self.top1 / self.count,
                'hit_at_k': self.hits / self.count, 'recall_at_k': math.fsum(self.recalls) / self.count}


def _members(groups):
    """`groups[i]` the set of groups of item i -> {group: array of item indices}."""
    import numpy as np

    members = {}
    for index, own in enumerate(groups):
        for group in own:
            members.setdefault(group, []).append(index)
    return {group: np.asarray(indices, dtype=np.int64) for group, indices in members.items()}


def _mask(size, members, groups):
    import numpy as np

    mask = np.zeros(size, dtype=bool)
    for group in groups:
        if group in members:
            mask[members[group]] = True
    return mask


def leave_one_out(ids, scores, folders, top_k):
    """`scores[i]` the row of similarities of image i to every image (a list, or a lazy row provider), `folders[i]`
    the set of folders of image i. Rows are read one at a time."""
    check_top_k(top_k)
    rank, totals, members = _Ranker(ids), _Totals(top_k), _members(folders)
    for query in range(len(ids)):
        relevant = _mask(len(ids), members, folders[query])
        relevant[query] = False
        if not relevant.any():
            totals.excluded += 1
            continue
        totals.add(rank(scores[query], relevant, top_k, exclude=query))
    return totals.summary()


def name_to_image(names, scores, relevant, ids, top_k):
    """`scores[c]` the row of similarities of name c to every image, `relevant[c]` the image indices of name c's
    folders (both may be lazy). Returns the summary and one row per name (rank of its first relevant image, hit and
    recall)."""
    import numpy as np

    check_top_k(top_k)
    rank, totals, rows = _Ranker(ids), _Totals(top_k), []
    for index, name in enumerate(names):
        indices = relevant[index]
        if not len(indices):
            totals.excluded += 1
            continue
        mask = np.zeros(len(ids), dtype=bool)
        mask[np.fromiter(indices, dtype=np.int64, count=len(indices))] = True
        result = rank(scores[index], mask, top_k)
        totals.add(result)
        rows.append(dict(name, images=len(indices), **result))
    return totals.summary(), rows


class _Rows:
    """Lazy `queries @ images.T`: row `i` is computed when it is read, so no Q x N matrix is held (F1)."""

    def __init__(self, queries, images):
        self._queries, self._images = queries, images

    def __getitem__(self, index):
        return self._images @ self._queries[index]


class _Relevant:
    """Lazy relevant image indices per name: the members of the folders that declare it (F1)."""

    def __init__(self, folder_sets, members):
        self._folder_sets, self._members = folder_sets, members

    def __getitem__(self, index):
        import numpy as np

        parts = [self._members[folder] for folder in self._folder_sets[index] if folder in self._members]
        return np.unique(np.concatenate(parts)) if parts else ()


def measure(rows, image_vectors, name_vectors, top_k):
    """The two metric groups from K0's rows and K1's vectors (`{image_id: vector}`, `{concept_id: vector}`). Peak
    memory: the unit vectors (O((N + C) d)) plus O(N) per query."""
    ids = sorted(image_vectors)
    if not ids:
        raise DevCompareError('no embedded image to compare')
    folders = {image_id: set() for image_id in ids}
    for row in rows['folder_images']:
        if row['image_id'] in folders:
            folders[row['image_id']].add(row['folder_id'])
    images = unit_rows([image_vectors[image_id] for image_id in ids])
    image_folders = [folders[image_id] for image_id in ids]
    loo = leave_one_out(ids, _Rows(images, images), image_folders, top_k)
    folders_of = {}
    for row in rows['folder_concepts']:
        folders_of.setdefault(row['concept_id'], set()).add(row['folder_id'])
    concepts = [row for row in rows['concepts'] if row['concept_id'] in name_vectors]
    names = [{'concept_id': row['concept_id'], 'name': row['display_name']} for row in concepts]
    relevant = _Relevant([folders_of.get(row['concept_id'], set()) for row in concepts], _members(image_folders))
    scores = _Rows(unit_rows([name_vectors[row['concept_id']] for row in concepts]), images) if concepts else []
    named, per_name = name_to_image(names, scores, relevant, ids, top_k)
    return loo, named, per_name


def _per_item(seconds, count):
    return seconds / count if count else None


def _vectors(table, key, kind=None):
    """`{row[key]: vector}` of a K1 table, the vectors read as one float32 array (4 B a value) rather than as Python
    lists of floats (F1); `kind` keeps the rows of one `text_kind`."""
    column = table.column('vector').combine_chunks()
    values = column.flatten().to_numpy(zero_copy_only=False).reshape(len(column), column.type.list_size)
    kinds = table.column('text_kind').to_pylist() if kind is not None else None
    return {name: values[index] for index, name in enumerate(table.column(key).to_pylist())
            if kinds is None or kinds[index] == kind}


def check_out_dir(config, project_root, out_dir):
    """The report directory: outside the protected project directories, the read-only knowledge root and the store,
    after resolving links (R-014; P3 fix round 1, F2). Checked before the store is opened or a request is sent."""
    from .reference import ReferenceCheckError, check_output_dir

    project = Path(project_root).resolve()
    try:
        return check_output_dir(project, out_dir, (('knowledge.root', project / config['knowledge']['root']),
                                                   ('storage.kb_store', project / config['storage']['kb_store'])))
    except ReferenceCheckError as error:
        raise DevCompareError(str(error)) from None


def run_dev_compare(config, project_root, out_dir, top_k, *, transport=None):
    """K0, K1 images, K1 names, then the metrics; writes `compare.json` under `out_dir` (inside the project root)
    and returns the report. `transport` is a labelled-test seam of K1's client only."""
    from types import SimpleNamespace

    from ..k0_sync import sync
    from ..k1_embed import ConceptText, embed_concept_texts, embed_images, embedder_settings
    from ..storage import open_knowledge_store
    from .reference import _write

    check_top_k(top_k)
    out_dir = check_out_dir(config, project_root, out_dir)
    project = Path(project_root).resolve()
    knowledge = config['knowledge']
    source = project / knowledge['root']
    k1 = embedder_settings(config)
    store = open_knowledge_store(config, COMMAND, project_root=project)
    started = time.perf_counter()
    scanned = sync(store, source, info_names=knowledge['info_file']['names'],
                   encoding=knowledge['info_file']['encoding'], extensions=knowledge['image_extensions'],
                   separator=knowledge['concept_separator'], workers=config['runtime']['cpu_workers'],
                   threshold=config['sync']['mass_deletion_guard']['max_removed_fraction'])
    scan_s = time.perf_counter() - started
    if any(scanned['report'][key] for key in ('unreadable', 'protected_folders', 'protected_info')):
        raise DevCompareError('k0: SOURCE_READ_FAILED')  # the same rule kb_sync applies
    rows = scanned['rows']
    started = time.perf_counter()
    images = asyncio.run(embed_images(store, config, scanned['handoff']['k1'], knowledge_root=source,
                                      transport=transport))
    image_s = time.perf_counter() - started
    items = [ConceptText(row['concept_id'], NAME_KIND, row['display_name']) for row in rows['concepts']
             if row['display_name']]
    started = time.perf_counter()
    texts = asyncio.run(embed_concept_texts(store, config, items, transport=transport))
    text_s = time.perf_counter() - started
    image_vectors = _vectors(images['image_embeddings'], 'image_id')
    name_vectors = _vectors(texts['concept_text_embeddings'], 'concept_id', NAME_KIND)
    named_folders = {row['folder_id'] for row in rows['folder_concepts']}
    image_report, text_report = images['report'], texts['report']
    # Nothing to rank when K1 embedded no image: the report still records the counts and K1's codes, then refuses.
    loo, named, per_name = measure(rows, image_vectors, name_vectors, top_k) if image_vectors else (None, None, [])
    report = {
        'format': FORMAT, 'label': LABEL, 'top_k': top_k,
        'embedder': {'backend': k1.backend_name, 'model_id': k1.backend['model']['id'],
                     'model_revision': k1.backend['model']['revision'], 'embedder_revision': k1.revision,
                     'dimension': k1.dimension},
        # `folders_unnamed`: K0 folders without a declared name, left out of the name metric (F6)
        'counts': {'folders': len(rows['folders']), 'images': len(rows['images']), 'names': len(items),
                   'folders_unnamed': sum(1 for row in rows['folders'] if row['folder_id'] not in named_folders),
                   'images_embedded': image_report['embedded'], 'images_skipped_done': image_report['skipped_done'],
                   'images_decode_failed': len(image_report['decode_failed']),
                   'images_failed': len(image_report['failed']), 'texts_embedded': text_report['embedded'],
                   'texts_skipped_done': text_report['skipped_done'], 'texts_failed': len(text_report['failed'])},
        'seconds': {'k0_scan': scan_s, 'k1_images': image_s, 'k1_names': text_s,
                    'per_embedded_image': _per_item(image_s, image_report['embedded']),
                    'per_embedded_name': _per_item(text_s, text_report['embedded'])},
        'leave_one_out': loo, 'name_to_image': named, 'per_name': per_name,
        'failed': {'images': image_report['failed'], 'texts': text_report['failed']},
    }
    _write(SimpleNamespace(project_root=project), Path(out_dir) / 'compare.json', report)
    if image_report['failed'] or text_report['failed']:
        raise DevCompareError(f"K1 left {len(image_report['failed'])} image(s) and {len(text_report['failed'])} "
                              'name(s) unembedded; compare.json records their codes')
    if loo is None:
        raise DevCompareError('no embedded image to compare')
    return report
