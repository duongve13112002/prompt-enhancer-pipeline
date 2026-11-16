"""Tiny synthetic storage fixtures (test-only): Unicode names, provenance, one image in two folders."""

from datetime import datetime, timezone
import hashlib

from prompt_enhancer.storage import SNAPSHOT_TABLES, SPECS, concept_id, image_id, normalise_name
from prompt_enhancer.storage.store import KnowledgeStore

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DIM = 4
# Folder -> names as written on line 1. 'kimono' appears in two folders and is one concept (decision 10).
FOLDERS = {
    'kimono': ['Kimono'],
    'kimono_donut': ['kimono', 'Donut'],
    'việt/áo_dài': ['Áo dài', '着物'],
}


def _hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def dataset(tag='1', dim=DIM, revision='emb-r1'):
    """Rows of the ten snapshot tables; `tag` marks every table so a reader can detect a mixed view."""
    shared = image_id(b'shared image bytes')
    other = image_id(b'other image bytes ' + tag.encode())
    vector = [0.5, -0.25, 1.0, 0.0][:dim] + [0.125] * max(0, dim - 4)
    rows = {name: [] for name in SNAPSHOT_TABLES}
    concepts = {}
    for folder, names in FOLDERS.items():
        rows['folders'].append({'folder_id': folder, 'info_file_hash': 'sha256:' + _hex(folder),
                                'declared_names': names, 'declared_descriptions': f'mô tả {tag} | 説明',
                                'name_source': 'info_file', 'error': ''})
        for position, name in enumerate(names):
            cid = concept_id(name)
            concepts.setdefault(cid, name)
            rows['folder_concepts'].append({
                'folder_id': folder, 'concept_id': cid, 'position': position, 'name_as_written': name,
                'description': f'desc {tag}', 'description_origin': 'generated' if position else 'info_file',
                'description_revision': 'assessor-r1' if position else None})
        image = shared if folder != 'việt/áo_dài' else other
        rows['folder_images'].append({'folder_id': folder, 'relative_path': f'ảnh {folder}.png',
                                      'image_id': image, 'modified_at': T0})
    for cid, name in concepts.items():
        rows['concepts'].append({'concept_id': cid, 'normalised_name': normalise_name(name), 'display_name': name,
                                 'kind': 'clothing', 'kind_origin': 'generated'})
        rows['cards'].append({
            'concept_id': cid, 'name': name, 'kind': {'value': 'clothing', 'origin': 'generated'},
            'facets': [], 'variant_groups': [], 'curated_statements': [], 'lint_findings': [],
            'revisions': {'model_revision': 'card-r1', 'prompt_id': 'card_v0', 'facet_schema_version': 'f1',
                          'stability_policy_version': 'p1', 'snapshot_id': tag}, 'summary': f'card {tag}'})
        rows['aliases'].append({'concept_id': cid, 'alias_text': f'{name} {tag}', 'origin': 'generated',
                                'status': 'accepted', 'collides_with': [], 'language': 'ja',
                                'revisions': {'model_revision': 'alias-r1', 'prompt_id': 'alias_v0'}})
        rows['concept_text_embeddings'].append({
            'concept_id': cid, 'text_kind': 'name', 'embedder_revision': revision, 'text_hash': _hex(name),
            'dim': dim, 'vector': vector})
    for image in (shared, other):
        rows['images'].append({'image_id': image, 'size_bytes': 18, 'format': 'png', 'width': 2, 'height': 2,
                               'decode_status': 'ok', 'near_duplicate_group': tag})
        rows['image_embeddings'].append({'image_id': image, 'embedder_revision': revision, 'dim': dim,
                                         'vector': vector})
        rows['observations'].append({
            'context_key': _hex('observation context ' + image),
            'image_id': image, 'concept_id': concept_id('kimono'), 'facet': 'apparel.color',
            'model_revision': 'obs-r1', 'prompt_id': 'observer_v0', 'facet_schema_version': 'f1',
            'facet_group': 'apparel', 'value': 'đỏ', 'visibility': 'clear',
            'region': {'x': 0, 'y': 0, 'width': 0.5, 'height': 0.5} if image == shared else None,
            'view': 'front', 'quality': 0.75, 'origin': 'generated'})
    return rows


def data_rows():
    """Rows of the four data-store tables."""
    return {
        'request_traces': [{
            'request_id': 'r-1', 'profile_id': 'demo', 'snapshot_id': None, 'effective_config_hash': 'h1',
            'status': 'degraded', 'retrieval_state': 'failed', 'seeds': {'variant': 7},
            'warnings': ['SNAPSHOT_MISSING'], 'calls': [{'role': 'writer', 'latency_ms': 3.5}], 'latency_ms': 12.5,
            'body': {'prompt': 'vẽ một con mèo 猫'}, 'created_at': T0}],
        'eval_scenarios': [{
            'scenario_id': 'sc-1', 'kind': 'single_concept', 'task': 'generate', 'prompt': 'một chiếc kimono',
            'source_image': None,
            'expected_concept_links': [{'mention': 'kimono', 'status': 'resolved',
                                        'concept_id': concept_id('kimono'), 'role': 'worn_garment'}],
            'expected_constraints': {'constraints': []},
            'generator': {'role': 'scenario_generator', 'model_revision': 'm1', 'prompt_id': 'p1'},
            'tier': 'silver', 'split': 'fit'}],
        'eval_labels': [{'scenario_id': 'sc-1', 'label_id': 'l-1', 'answer': {'concepts': ['kimono']},
                         'tier': 'silver', 'split': 'fit'}],
        'eval_results': [{'run_id': 'run-1', 'scenario_id': 'sc-1', 'metrics': {'recall': 1.0},
                          'status': 'success', 'config_hash': 'h1'}],
    }


def make_store(project_root, root, **overrides):
    """A store with explicit test-only settings (values resolve deploy_required/design_required for tests)."""
    settings = {'timeout_s': 20, 'poll_s': 0.01, 'table_format': 'parquet', 'keep': 1, 'retention_s': 0,
                'resume': True}
    settings.update(overrides)
    return KnowledgeStore(root, project_root=project_root, **settings)


def build_snapshot(writer, tag='1', **kwargs):
    builder = writer.begin_snapshot()
    for name, rows in dataset(tag, **kwargs).items():
        builder.write_table(name, rows, dim=kwargs.get('dim', DIM) if SPECS[name].vector else None)
    builder.add_file('index/names.txt', f'index {tag}'.encode())
    return builder.finish(source_scan_summary={'folders': len(FOLDERS)})


def publish(store, tag='1'):
    with store.writer() as writer:
        sid = build_snapshot(writer, tag)
        writer.publish(sid)
    return sid


def tag_of(pinned):
    """Each table's tag; a consistent snapshot yields one value."""
    tags = {row['near_duplicate_group'] for row in pinned.read_rows('images')}
    tags |= {row['summary'].split(' ')[1] for row in pinned.read_rows('cards')}
    tags |= {pinned.read_file('index/names.txt').decode().split(' ')[1]}
    return tags
