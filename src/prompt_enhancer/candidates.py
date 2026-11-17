"""S1: bounded, structural candidate retrieval over S0's one pinned snapshot (R-009/R-016)."""

from dataclasses import replace
from fractions import Fraction
from functools import lru_cache
import json
import math
from pathlib import Path
import sys
import unicodedata

from .config import ConfigurationError, TEXT_PLACEHOLDER, _EXACT_INTEGERS, _schema, validate_candidates_reads
from .k1_embed import embedder_settings
from .k5_index import ServingIndex, VECTOR_TABLES
from .storage import StorageError, normalise_name


@lru_cache(maxsize=1)
def normalization_expansion():
    """Artifact-bound bound derived from this interpreter's Unicode data, never a fitted threshold.

    Write y=NFKC(raw). NFKD(raw)=NFKD(y), and every raw code point contributes at least
    one decomposition point. Thus len(raw)<=len(NFKD(y))<=D*len(y)<=D*len(casefold(y)).
    Reordering changes no lengths; the scan checks the nonempty mappings used by this proof.
    """
    maximum = 1
    for ordinal in range(sys.maxunicode + 1):
        char = chr(ordinal)
        decomposed = unicodedata.normalize('NFKD', char)
        if not decomposed or not char.casefold():
            raise RuntimeError('Unicode normalization bound requires nonempty mappings')
        maximum = max(maximum, len(decomposed))
    return maximum


def exact_width(keys):
    """Conservative raw width for any span normalizing to one of the already-normalized keys."""
    longest = max(map(len, keys), default=0)
    return longest * normalization_expansion() if longest else 0


def window_count(length, width):
    width = min(length, width)
    return width * (length + 1) - width * (width + 1) // 2


def prompt_windows(text, width):
    """Each nonempty original code-point span exactly once per admitted width, without word rules."""
    for size in range(1, min(len(text), width) + 1):
        for start in range(len(text) - size + 1):
            yield start, start + size, text[start:start + size]


def _reject_constant(value):
    raise ValueError('non-JSON numeric constant')


def _structural_segments(prompt):
    """Original spans plus decoded JSON value text; JSON keys never become extra queries.

    The stdlib raw_decode end offset retains duplicate values and escaped token provenance.
    Validate once before a linear token walk, avoiding recursive re-decoding of each container.
    """
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    try:
        decoder.decode(prompt)
    except (ValueError, RecursionError):
        is_json = False
        parts = []
        start = 0
        for end, char in enumerate(prompt):
            if char == ',' or char in '\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029':
                parts.append((start, end, prompt[start:end]))
                start = end + 1
        parts.append((start, len(prompt), prompt[start:]))
    else:
        is_json = True
        parts = []
        offset = 0
        while offset < len(prompt):
            if prompt[offset] in ' \t\r\n{}[],:':  # protocol-bound JSON syntax, never language text
                offset += 1
                continue
            start = offset
            value, offset = decoder.raw_decode(prompt, offset)
            after = offset
            while after < len(prompt) and prompt[after] in ' \t\r\n':
                after += 1
            if after < len(prompt) and prompt[after] == ':':
                continue
            parts.append((start, offset, value if isinstance(value, str) else prompt[start:offset]))
    return sorted(set(part for part in parts if part[2])), is_json


def structural_segments(prompt):
    """Return structural queries, preserving full-parse provenance internally for S1."""
    return _structural_segments(prompt)[0]


def query_embedder_settings(config):
    """K1 query adapter: replace only the text template, keeping document settings and space unchanged."""
    return replace(embedder_settings(config), text_template=config['embedding']['query_text_template'])


def reciprocal_rank_fusion(rankings, constant):
    """Unweighted one-based RRF. Exact rational accumulation makes input order and ties deterministic."""
    scores = {}
    offset = Fraction(str(constant))
    for ranking in rankings:
        for rank, concept in enumerate(dict.fromkeys(ranking), 1):
            scores[concept] = scores.get(concept, Fraction()) + 1 / (offset + rank)
    return sorted(scores, key=lambda concept: (-scores[concept], concept))


def _span(prompt, unit):
    start, end, _ = unit
    return {'start': start, 'end': end, 'text': prompt[start:end]}


def validate_shortlist(result, prompt, *, project_root):
    """Validate both the wire shape and the invariants JSON Schema cannot express."""
    _EXACT_INTEGERS(_schema(project_root, 'candidate_shortlist.v0')).validate(result)
    seen = set()
    spans = [entry['matched_span'] for entry in result['candidates']]
    spans += [warning['span'] for warning in result['warnings']]
    for span in spans:
        if not 0 <= span['start'] < span['end'] <= len(prompt) or span['text'] != prompt[span['start']:span['end']]:
            raise ValueError('S1: invalid original prompt span')
    for entry in result['candidates']:
        if entry['concept_id'] in seen:
            raise ValueError('S1: duplicate shortlist concept')
        seen.add(entry['concept_id'])
    return result


def _catalogue(snapshot):
    concepts = {row['concept_id']: dict(row, description=None) for row in snapshot.read_rows('concepts')}
    exact = {}
    for concept, row in concepts.items():
        exact.setdefault(normalise_name(row['display_name']), {})[concept] = 'name'
    for row in snapshot.read_rows('aliases'):
        if row['status'] == 'accepted':
            exact.setdefault(normalise_name(row['alias_text']), {}).setdefault(row['concept_id'], 'alias')
    memberships = {}
    for row in sorted(snapshot.read_rows('folder_concepts'), key=lambda item: (item['folder_id'], item['concept_id'])):
        memberships.setdefault(row['folder_id'], set()).add(row['concept_id'])
        target = concepts[row['concept_id']]
        if target['description'] is None and row['description'] is not None:
            target['description'] = ' '.join(row['description'].split())
    image_folders = {}
    for row in snapshot.read_rows('folder_images'):
        image_folders.setdefault(row['image_id'], set()).add(row['folder_id'])
    # Preserve the two membership edges; do not materialize every image/concept pair per folder.
    return concepts, exact, (image_folders, memberships)


def _remember(hits, concept, score, source, unit):
    # Source/evidence order is protocol-bound; scores affect ranks within their own source only.
    key = (-score, 0 if source == 'name' else 1, source, unit[0], unit[1])
    if concept not in hits or key < hits[concept][0]:
        hits[concept] = (key, source, unit)


def _rank(hits, depth):
    return sorted(hits, key=lambda concept: (hits[concept][0][0], concept))[:depth]


def _check_space(reader, config, embedder):
    settings = query_embedder_settings(config)
    space = reader.metadata['embedding']
    supplied = getattr(embedder, 'settings', None)
    if (settings.revision != space['revision'] or settings.dimension != space['dimension']
            or (supplied is not None and (supplied.revision != space['revision']
                                         or supplied.dimension != space['dimension']))):
        raise StorageError('S1: query embedding space differs from pinned snapshot')
    if supplied != settings:
        raise ConfigurationError('S1: supply an encoder using query_embedder_settings(effective_config)')
    return settings


async def _dense(reader, embedder, settings, queries, hits, depth, image_concepts):
    image_folders, memberships = image_concepts
    for start in range(0, len(queries), settings.batch_size):
        batch = queries[start:start + settings.batch_size]
        vectors = await embedder.embed_texts([unit[2] for unit in batch])
        if not isinstance(vectors, (list, tuple)) or len(vectors) != len(batch):
            raise StorageError('S1: query vector count differs')
        for unit, vector in zip(batch, vectors):
            for table in VECTOR_TABLES:
                for hit in reader.search(table, vector, k=depth, embedder_revision=settings.revision,
                                         dimension=settings.dimension):
                    if not math.isfinite(hit['score']):
                        raise StorageError('S1: nonfinite dense score')
                    concepts = ([hit['key']['concept_id']] if table == 'concept_text_embeddings'
                                else sorted({concept for folder in image_folders.get(hit['key']['image_id'], ())
                                             for concept in memberships.get(folder, ())}))
                    for concept in concepts:
                        _remember(hits, concept, hit['score'], 'dense', unit)


async def build_candidates(intake_result, *, embedder=None):
    """Return candidate_shortlist.v0; never acquire/release S0's pin or create a model client.

    Programming/configuration/space errors raise before calls. Operational retrieval failure returns
    failed S1 so the future coordinator can continue without knowledge. Cancellation propagates.
    """
    payload = intake_result.payload
    config = payload.get('effective_config')
    if not isinstance(config, dict) or not isinstance(config.get('rag'), dict):
        raise ValueError('S1 requires successful intake configuration')
    if config['rag'].get('enabled') is False:
        raise ValueError('S1 is absent from the retrieval-disabled graph')
    if config['rag'].get('enabled') is not True or payload.get('outcome') not in ('ready', 'degraded'):
        raise ValueError('S1 requires successful retrieval-enabled intake')
    prompt = payload.get('prompt')
    if not isinstance(prompt, str):
        raise ValueError('S1 requires the original prompt string')
    snapshot = intake_result.snapshot
    root = snapshot.store.catalog.root if snapshot is not None else Path(__file__).resolve().parents[2]
    options = validate_candidates_reads(config, project_root=root)
    result = {'snapshot_id': None if snapshot is None else snapshot.snapshot_id,
              'state': 'ready', 'candidates': [], 'warnings': [], 'error': None}

    def failed(code):
        result.update(state='failed', candidates=[], error={'code': code, 'message': {
            'SNAPSHOT_MISSING': 'S1: no pinned snapshot',
            'WINDOW_BUDGET_EXCEEDED': 'S1: candidate work budget exceeded',
            'RETRIEVAL_FAILED': 'S1: operational retrieval failed',
        }[code]})
        return validate_shortlist(result, prompt, project_root=root)

    if snapshot is None:
        if payload.get('outcome') != 'degraded':
            raise ValueError('S1: intake snapshot is closed or absent')
        return failed('SNAPSHOT_MISSING')
    if payload.get('retrieval') != {'state': 'pinned', 'snapshot_id': snapshot.snapshot_id}:
        raise ValueError('S1: intake snapshot identity differs')
    if not prompt or not options['shortlist_size'] or not any(s['enabled'] for s in options['sources'].values()):
        return validate_shortlist(result, prompt, project_root=root)
    try:
        prompt.encode('utf-8')
        reader = ServingIndex(snapshot, config)
        concepts, exact, image_concepts = _catalogue(snapshot)
    except Exception:
        return failed('RETRIEVAL_FAILED')
    if not concepts:
        return validate_shortlist(result, prompt, project_root=root)
    sources = options['sources']
    settings = _check_space(reader, config, embedder) if sources['dense']['enabled'] else None
    try:
        segments, is_json = _structural_segments(prompt) if options['segment_prompt'] else ([], False)
        queries = sorted(set([(0, len(prompt), prompt), *segments]))
        width = exact_width(exact) if sources['alias']['enabled'] else 0
        # Decoded JSON strings can contain exact mentions hidden by escape syntax. Probe their
        # bounded windows too, attributing them to the complete original scalar token.
        decoded = [unit for unit in segments if is_json and prompt[unit[0]] == '"'
                   and unit[2] != prompt[unit[0] + 1:unit[1] - 1]]
        work = window_count(len(prompt), width)
        work += sum(window_count(len(unit[2]), width) for unit in decoded)
        work += len(queries) * (int(sources['lexical']['enabled']) + int(sources['dense']['enabled']))
        if work > options['max_prompt_windows']:
            return failed('WINDOW_BUDGET_EXCEEDED')
        hits = {name: {} for name in sources}

        def exact_probe(unit):
            token = normalise_name(unit[2])
            for concept in reader.exact(unit[2]):
                _remember(hits['alias'], concept, 0, exact[token][concept], unit)

        if sources['alias']['enabled']:
            for unit in prompt_windows(prompt, width):
                exact_probe(unit)
            for outer in decoded:
                for _, _, text in prompt_windows(outer[2], width):
                    exact_probe((outer[0], outer[1], text))
        if sources['lexical']['enabled']:
            for unit in queries:
                for hit in reader.lexical(unit[2]):
                    _remember(hits['lexical'], hit['concept_id'], hit['matched_grams'], 'lexical', unit)
        if sources['dense']['enabled']:
            admitted = []
            for unit in queries:
                rendered = settings.text_template.replace(TEXT_PLACEHOLDER, unit[2])
                if len(rendered) > config['embedding']['query_text_limit']:
                    result['warnings'].append({'code': 'DENSE_QUERY_TOO_LONG', 'span': _span(prompt, unit)})
                else:
                    admitted.append(unit)
            await _dense(reader, embedder, settings, admitted, hits['dense'], sources['dense']['depth'], image_concepts)
        rankings = {name: _rank(hits[name], source['depth']) for name, source in sources.items() if source['enabled']}
        chosen = reciprocal_rank_fusion(rankings.values(), options['rrf_constant'])[:options['shortlist_size']]
        for concept in chosen:
            evidence = [(ranking.index(concept), hits[name][concept][1], hits[name][concept][2])
                        for name, ranking in rankings.items() if concept in ranking]
            _, source, unit = min(evidence, key=lambda row: (row[0],
                ('name', 'alias', 'lexical', 'dense').index(row[1]), row[2][0], row[2][1]))
            row = concepts[concept]
            result['candidates'].append({'concept_id': concept, 'name': row['display_name'], 'kind': row['kind'],
                                         'description': row['description'], 'matched_source': source,
                                         'matched_span': _span(prompt, unit)})
        return validate_shortlist(result, prompt, project_root=root)
    except Exception:
        return failed('RETRIEVAL_FAILED')
