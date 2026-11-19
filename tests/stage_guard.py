"""Inventory of the output-shaping code of each offline stage and the digest that pins it (KBS-S3-1, R-017).

A stage's artifact key carries a declared revision instead of a hash of module source. The revision must move when the
code that shapes an artifact moves, so `tests/test_stage_revisions.py` pins a digest of the normalised syntax tree of
exactly the functions and constants listed here. Docstrings, comments, whitespace and every unlisted function leave the
digest alone; any other edit to a listed one changes it.
"""

import ast
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path

# stage -> (module that declares its revision, constant name, [(module, qualified name), ...])
# Each entry shapes a request, the parsing of a reply, the preparation of an input or the stored row of that stage;
# a function that only chooses order of work, concurrency, errors or reporting is left out on purpose.
MODEL_CALL = [
    ('model_client', 'RoleClient._call'),  # composes the request, parses the reply, validates it against the schema
    ('model_client', 'image_max_side'), ('model_client', 'NO_LIMIT'),  # the side an image is resized to before sending
    ('model_client', '_usage'),  # the token counts stored with a call record
    ('model_mapping', 'map_generation_fields'), ('model_mapping', 'openai_request'),
    ('model_mapping', 'map_cloud_reasoning'), ('model_cloud', 'cloud_request'),  # request bodies
    ('model_mapping', 'system_messages'),  # T017-FIX: the system messages and the addendum layout of a request body
    ('model_mapping', 'system_texts'), ('model_mapping', 'effective_system_hash'),  # the system hash in a call record
    ('model_mapping', 'SAMPLING'), ('model_mapping', 'LOCAL_REASONING_EFFORT'),  # their fixed request values
    ('model_json', 'check_json_depth'), ('model_json', 'strict_json'), ('model_json', 'MAX_JSON_DEPTH'),  # reply parsing
    ('model_image', 'prepare_image'), ('model_image', 'prepare_image_with_info'), ('model_image', '_stored_size'),
    ('config', 'image_media_type'),  # image preparation and its declared media type
    ('model_schemas', 'SchemaRegistry.validate'),  # reply validation
    ('model_schemas', 'SchemaRegistry.validate_with'),  # T017-FIX2: reply validation against a caller-derived schema
    ('profile_prompt', 'load_prompt'),  # the system prompt bytes
]
K1_SHARED = [
    ('k1_embed', 'LAYOUT_VERSION'), ('k1_embed', 'ENCODING_FORMAT'), ('k1_embed', 'pack_vector'),
    ('k1_embed', 'unpack_vector'), ('k1_embed', '_reduce'), ('k1_embed', 'parse_embeddings'),  # stored vector, reply
    ('k1_embed', 'Embedder._base'), ('k1_embed', 'Embedder._messages'), ('k1_embed', '_load_record'),
    ('k1_embed', 'Embedder._post'), ('k1_embed', 'Embedder._embed'), ('k1_embed', 'Embedder.embed_images'),
    ('k1_embed', 'Embedder.embed_texts'),  # which vector answers which input
    ('k1_embed', '_chunks'),  # batch composition: a batch changes bf16 vectors (T-018); `embedding.batch_size` is not
    ('k1_embed', '_commit_record'), ('storage.identity', 'canonical_json'),  # in the key, so this code stands for it
]
NAMING_SHARED = [('k2_naming', '_validate_reply')]  # reply validation every model stage uses
RETRY_SHARED = [  # decisions 183 and 188: which refusals are retried, with which seed, and what an attempt records
    ('reply_retry', 'REFUSED'), ('reply_retry', 'SEED_WARNING'), ('reply_retry', 'attempt_seed'),
    ('reply_retry', 'Attempts.__init__'), ('reply_retry', 'reply_digest'), ('reply_retry', 'call_until_valid')]
STAGES = {
    'k0': ('k0_checkpoint', 'K0_SCAN_REVISION', [
        ('k0', 'trim_info_token'), ('k0', 'parse_info'), ('k0', 'rows_from_scan'),
        ('k0_scan', 'decode_info'), ('k0_scan', '_read'), ('k0_scan', 'scan_source'), ('k0_sync', 'sync'),
        ('k0_checkpoint', '_VERSION'), ('k0_checkpoint', '_TABLES'), ('k0_checkpoint', '_json_value'),
        ('k0_checkpoint', '_restore'), ('k0_checkpoint', '_bytes'), ('k0_checkpoint', '_key'),
        ('k0_checkpoint', '_FileCache._identity'), ('k0_checkpoint', '_FileCache._get'),
        ('k0_checkpoint', '_FileCache._put'), ('k0_checkpoint', '_scan_previous'),
        ('k0_checkpoint', '_merge_previous'), ('k0_checkpoint', 'scan_to_checkpoint'),
        ('k0_scan', 'read_source_file'), ('k0_scan', 'iter_source_file'), ('storage.identity', 'concept_id'),
        ('storage.identity', 'image_id')]),
    'k1_image': ('k1_embed', 'K1_IMAGE_REVISION', K1_SHARED + [
        ('k1_embed', 'IMAGE_METADATA_RULE'), ('k1_embed', 'NO_INSTRUCTION'), ('k1_embed', 'DECODE_OK'),
        ('k1_embed', 'UNDECODABLE'), ('k1_embed', 'EXCEEDS'), ('k1_embed', 'Embedder._image_part'),
        ('k1_embed', 'Embedder.image_bodies'), ('k1_embed', '_decode'), ('k1_embed', '_image_record'),
        ('k1_embed', '_image_order'), ('k1_embed', '_batch_members'), ('k1_embed', '_companion_lost'),
        ('k1_embed', '_read_verified'), ('k0_scan', 'read_source_file'), ('k0_scan', 'iter_source_file'),
        ('model_image', 'prepare_vision_image'), ('model_image', 'prepare_vision_image_with_info'),
        ('model_image', 'prepare_image_with_info'),
        ('model_image', '_vision_args'), ('model_image', '_stored_size'), ('model_image', '_decoding_file_bytes'),
        ('config', 'image_media_type')]),
    'k1_text': ('k1_embed', 'K1_TEXT_REVISION', K1_SHARED + [
        ('k1_embed', 'text_hash'), ('k1_embed', 'Embedder.text_bodies'), ('k1_embed', '_text_record'),
        ('k1_embed', '_text_order'),
        ('config', 'TEXT_PLACEHOLDER')]),
    'model_call': ('model_client', 'MODEL_CALL_REVISION', MODEL_CALL),
    'k2_naming': ('k2_naming', 'K2_NAMING_REVISION', NAMING_SHARED + [
        ('k2_naming', 'LAYOUT'), ('k2_naming', 'ROLE_SCHEMAS'), ('k2_naming', 'parse_assessment'),
        ('k2_naming', 'parse_naming'), ('k2_naming', '_override_selectors'), ('k2_naming', 'parse_overrides'),
        ('k2_naming', 'apply_name_overrides'), ('k2_naming', 'uniform_draw'), ('k2_naming', '_load'),
        ('k2_naming', '_save'), ('k2_naming', '_naming_payload'), ('k2_naming', '_naming_key'),
        ('k2_naming', '_naming_result'), ('k2_naming', '_replace_members'), ('k2_naming', '_draw'),
        ('k2_naming', '_redraw'), ('k2_naming', '_read_info'), ('k2_naming', '_read_overrides'),
        ('k2_naming', 'TABLES'), ('k0', 'parse_info'), ('k0', 'trim_info_token'),
        ('storage.identity', 'canonical_json'), ('storage.identity', 'normalise_name'),
        ('storage.identity', 'concept_id')]),
    # draw, redraw, override and applied-name artifacts are keyed by DRAW_REVISION, not by the naming revision
    'k2_draw': ('k2_naming', 'K2_DRAW_REVISION', [
        ('k2_naming', 'uniform_draw'), ('k2_naming', '_draw'), ('k2_naming', '_redraw'), ('k2_naming', '_digest'),
        ('k2_naming', '_key'), ('k2_naming', '_load'), ('k2_naming', '_save'), ('k2_naming', '_recovery_index'),
        ('k2_naming', '_referenced_draw'), ('storage.identity', 'canonical_json')]),
    'k2_observe': ('k2_observe', 'K2_OBSERVE_REVISION', NAMING_SHARED + RETRY_SHARED + [
        ('k2_observe', 'LAYOUT'), ('k2_observe', 'GROUPING'), ('k2_observe', 'STORED_SCHEMA'),
        ('k2_observe', 'REPLY_SCHEMA'), ('k2_observe', 'group_images'), ('k2_observe', 'choose_representatives'),
        ('k2_observe', 'parse_observation'), ('k2_observe', '_observation_payload'),
        ('k2_observe', '_observation_bundle'), ('k2_observe', '_member'), ('k2_observe', '_declared'),
        ('k2_observe', '_rows'), ('k1_embed', '_read_verified'), ('storage.identity', 'canonical_json')]),
    'k3': ('k3_cards', 'K3_CARD_REVISION', NAMING_SHARED + RETRY_SHARED + [
        ('k3_cards', 'LAYOUT'), ('k3_cards', 'VISIBLE'), ('k3_cards', 'REPLY_SCHEMA'), ('k3_cards', 'STORED_SCHEMA'),
        ('k3_cards', 'card_inputs'), ('k3_cards', '_partition'), ('k3_cards', 'parse_card_reply'),
        ('k3_cards', 'stability_label'), ('k3_cards', '_has_variation'), ('k3_cards', 'assemble_card'),
        ('k3_cards', '_grouping_bundle'), ('k3_cards', '_card_revisions'), ('k3_cards', '_curated'),
        ('storage.identity', 'canonical_json')]),
    'k4': ('k4_aliases', 'K4_ALIAS_REVISION', NAMING_SHARED + [
        ('k4_aliases', 'LAYOUT'), ('k4_aliases', 'REPLY_SCHEMA'), ('k4_aliases', 'STORED_SCHEMA'),
        ('k4_aliases', 'parse_alias_reply'), ('k4_aliases', 'mark_collisions'), ('k4_aliases', '_record_revisions'),
        ('k4_aliases', '_alias_payload'), ('k4_aliases', '_generation_bundle'), ('k4_aliases', '_alias_rows'),
        ('storage.identity', 'normalise_name'), ('storage.identity', 'canonical_json')]),
    'k5': ('k5_index', 'K5_INDEX_REVISION', [
        ('k5_index', 'LAYOUT'), ('k5_index', 'LOOKUP_SCHEMA'), ('k5_index', 'DOCUMENT_SCHEMA'),
        ('k5_index', '_encode'), ('k5_index', '_document'), ('k5_index', '_order'), ('k5_index', '_batches'),
        ('k5_index', '_source_hash'), ('k5_index', 'character_ngrams'), ('k5_index', '_lookups'),
        ('k5_index', '_eligible'), ('k5_index', '_matrix'), ('k5_index', '_parts'), ('k5_index', '_tools'),
        ('k5_index', '_metadata'), ('k5_index', '_part_entry'), ('k5_index', 'SCHEMA'), ('k5_index', 'MANIFEST'),
        ('k5_index', 'INPUTS'), ('k5_index', 'VECTOR_TABLES'), ('k5_index', 'SCORE_DTYPE'),
        ('k5_index', 'NORMALISATION_RULE'),
        ('storage.identity', 'normalise_name'), ('storage.identity', 'canonical_json')]),
    'kb_sync': ('kb_sync', 'KB_SYNC_REVISION', [
        ('kb_sync', 'retain_images'), ('kb_sync', 'concept_texts'), ('kb_sync', '_checkpoint'),
        ('kb_sync', 'sync_knowledge'), ('kb_sync', 'alias_failures'), ('kb_sync', 'require_per_concept'),
        ('kb_sync', '_PER_CONCEPT_CODES'),  # which K4 failures publish and what the snapshot records (decision 165)
        ('kb_sync', '_alias_content'), ('kb_sync', '_publishes_the_same_snapshot'),  # identical-publish skip (P3-SWEEP-1)
        ('kb_sync', 'retained_identities'), ('kb_sync', '_k4_failed_summary')]),  # P3-SWEEP-2: early check, failed-id digest
}


KEY = 'composes or names the artifact key itself, so a change moves every key without a revision'
DRIVER = 'driver: schedules work, concurrency and reporting; the stored content comes from the pinned helpers'
LIFECYCLE = 'transport, lifecycle, admission or credentials; no artifact content'
ERRORS = 'error type, error code or report text only'
DECLARED = 'the declared revision itself'
CHECK = 'validates configuration or stored input and refuses; writes no artifact content'
FILES = 'file system access and link refusal; the bytes it yields are hashed and pinned elsewhere'
REUSE = 'chooses reuse or recompute of a stored record; the record is verified by a pinned loader'
WRITER = 'request-time profile and writer prompt parsing; no knowledge-base stage calls it'
SERVING = 'query-time search over a built index; it writes no index file'
LOADER = 'loads schema, config or profile text whose bytes enter the key hash'
# Every top-level function, method and constant of these modules is pinned in some stage above or listed here with a
# reason; `tests/test_stage_revisions.py` fails on a name that is neither, so a new function cannot go unclassified.
LEFT_OUT = [
    ('k0', DRIVER, ['resolve_workers']),
    ('k0', 'plans removals and diffs between runs; reads stored rows and writes none of their content',
     ['guard_removals', 'diff_folders', 'diff_rows']),
    ('k0_scan', FILES, ['_CHUNK_BYTES', '_REPARSE', '_is_link', '_native_open', '_lstat', '_opened', '_names']),
    ('k0_scan', ERRORS, ['ScanError', 'LinkRefused', 'CacheError', 'SourceTooLarge', 'MissingFileID']),
    ('k0_scan', CHECK, ['validate_scan']),
    ('k0_checkpoint', DECLARED, ['K0_SCAN_REVISION']),
    ('k0_checkpoint', KEY, ['_REVISION']),
    ('k0_checkpoint', REUSE, ['_FileCache.__init__', '_FileCache.get', '_FileCache.put', '_matching', '_valid_id',
                              '_previous', 'load_scan_checkpoint']),
    ('k0_sync', DRIVER, ['_previous_for_plan', 'sync_config']),
    ('k1_embed', DECLARED, ['K1_IMAGE_REVISION', 'K1_TEXT_REVISION']),
    ('k1_embed', KEY, ['IMAGE_STAGE', 'TEXT_STAGE', '_digest', 'image_revisions', 'text_revisions', 'image_key',
                       'text_key', 'embedder_settings', 'EmbedderSettings', 'ConceptText']),
    ('k1_embed', 'cancellation of one decode worker (safety-bound)', ['_DECODE_STOP', '_check_decode_stop']),
    ('k1_embed', ERRORS, ['SOURCE_CHANGED', 'REUSED_REFERENCED', 'EmbeddingError.__init__']),
    ('k1_embed', LIFECYCLE, ['Embedder.__init__', 'Embedder.close', 'Embedder.capacity_window']),
    # embed_images keeps only scheduling: which cached members join a request is `_batch_members`/`_companion_lost`
    ('k1_embed', DRIVER, ['_flow_limit', '_cpu_workers', 'embed_images', 'embed_concept_texts']),
    ('k1_embed', REUSE, ['_skip_or_reuse']),
    ('k2_naming', DECLARED, ['K2_NAMING_REVISION', 'K2_DRAW_REVISION']),
    ('k2_naming', KEY, ['DRAW_REVISION', '_role_revisions']),
    ('k2_naming', ERRORS, ['failure_code', 'record_probe_failure']),
    ('k2_naming', DRIVER, ['name_folders']),
    ('k2_observe', DECLARED, ['K2_OBSERVE_REVISION']),
    ('k2_observe', KEY, ['STAGE', 'observation_revisions', 'observation_key']),
    ('k2_observe', DRIVER, ['observe_images']),
    ('k2_observe', 'diagnostic evidence row of a refused attempt; never read back as a result', ['_failure_row']),
    ('k3_cards', DECLARED, ['K3_CARD_REVISION']),
    ('k3_cards', DRIVER, ['build_cards']),
    ('k3_cards', 'diagnostic evidence row of a refused attempt; never read back as a result', ['_failure_row']),
    ('k4_aliases', DECLARED, ['K4_ALIAS_REVISION']),
    ('k4_aliases', CHECK, ['_inputs']),
    ('k4_aliases', DRIVER, ['build_aliases']),
    ('k5_index', DECLARED, ['K5_INDEX_REVISION']),
    ('k5_index', 'reads and verifies a built index back; a mismatch refuses, nothing is written',
     ['_digest', '_decode', '_parse_document', '_checked_tables', '_allocation_bound', '_building_tables',
      '_building_reader', '_validate']),
    ('k5_index', DRIVER, ['build_index']),
    ('k5_index', SERVING, ['ServingIndex.__init__', 'ServingIndex.exact', 'ServingIndex.lexical',
                           'ServingIndex.search']),
    ('kb_sync', DECLARED, ['KB_SYNC_REVISION']),
    ('kb_sync', KEY, ['LAYOUT', 'ROLES', 'sync_identity']),
    ('kb_sync', ERRORS, ['require_success', '_CLOSED_CODES', '_closed_code']),
    ('model_client', DECLARED, ['MODEL_CALL_REVISION']),
    ('model_client', ERRORS, ['MODEL_CODES', 'safe_model_code', 'safe_probe_events', 'ModelCallError.__init__',
                              'ProbeCasesMissing', '_safe_attempts']),
    ('model_client', KEY, ['_digest', 'semantic_role_settings', 'RoleClient._proof_key']),
    ('model_client', 'capability probes admit a backend or role; a probe result gates a call and is no artifact content',
     ['CAPABILITIES', 'IMAGE_SOURCE_ROLES', 'validate_require', 'RoleClient._multiple_images_pair',
      'RoleClient._take_probe_requests', 'RoleClient._probe_transport', 'RoleClient._production_probe',
      'RoleClient._bundle_images', 'RoleClient._extend_multiple_images', 'RoleClient.ensure_role_capabilities',
      'RoleClient._retry_plain_image']),  # decision 188: the plain image probe's seed retry
    ('model_client', LIFECYCLE, ['ModelClientRun.__init__', 'ModelClientRun.for_role', 'ModelClientRun.close',
                                 'BoundModelClient.__init__', 'BoundModelClient.call', 'BoundModelClient.for_role',
                                 'BoundModelClient.close', 'run_concurrency', 'compose_model_client',
                                 'RoleClient.__init__', 'RoleClient._headers', 'RoleClient.start_backend',
                                 'RoleClient._start_backend', 'RoleClient.call']),
    ('model_cloud', LIFECYCLE, ['cloud_headers']),
    ('model_image', 'safety limits and cancellation of a decode', ['_BACKSTOP_LOCK', 'configure_decode_backstop']),
    ('model_image', ERRORS, ['ImageRejected.__init__', 'UndecodableImage', 'DecodeStopped']),
    ('model_image', CHECK, ['check_vision_settings', '_check_settings']),
    ('model_schemas', LOADER, ['SnapshotCache.__init__', 'SnapshotCache.digest', 'SnapshotCache.fetch',
                               'SnapshotCache.clear', '_read_directory', 'SchemaRegistry.__init__',
                               'SchemaRegistry.from_snapshot', 'SchemaRegistry._build', 'SchemaRegistry.get',
                               'REGISTRY_CACHE_CAPACITY', '_REGISTRIES', 'cached_registry']),
    ('profile_prompt', WRITER, ['SECTION_NAMES', '_HEADERS', '_BOM', '_PROMPT_ID_CHARS', 'ProfileError.__init__',
                                'sha256_tag', 'PromptStructure.body', '_lines', 'parse_structure', 'parse_meta',
                                'placeholders', 'system_message', 'user_template']),
    ('storage.identity', KEY, ['PHYSICAL_VERSION', 'REVISION_FIELDS', 'artifact_key', 'check_artifact_key']),
    ('storage.identity', 'store paths and names; a path change moves where an artifact lives, not what it holds',
     ['_ARTIFACT_PATH', 'artifact_path', 'check_artifact_path', 'check_relative_name', 'identity_key']),
    ('storage.identity', CHECK, ['_HEX64', '_KIND', 'is_concept_id', 'is_image_id', 'check_folder_id', 'check_kind']),
]
CONFIG_PINNED = {'image_media_type', 'TEXT_PLACEHOLDER'}  # the two config names a stage inventory draws on


def left_out(module_names_of):
    """module -> {name: reason}; `config` is left out as a whole except the names a stage inventory pins."""
    table = {}
    for module, reason, names in LEFT_OUT:
        for name in names:
            table.setdefault(module, {})[name] = reason
    table['config'] = {name: 'configuration schema, loading and validation; configured values enter the keys '
                             'themselves' for name in module_names_of('config') if name not in CONFIG_PINNED}
    return table


def unclassified(source_of=None):
    """(missing, stale, doubled): names that are neither pinned nor left out, left-out names that no longer exist,
    and names both pinned and left out, per covered module."""
    source_of = source_of or module_source
    pinned = {(module, name) for stage in STAGES.values() for module, name in stage[2]}
    table = left_out(lambda module: module_names(source_of(module)))
    missing, stale, doubled = [], [], []
    for module in sorted({module for module, _ in pinned} | set(table)):
        names = set(module_names(source_of(module)))
        out = set(table.get(module, {}))
        missing += [(module, name) for name in sorted(names - out) if (module, name) not in pinned]
        stale += [(module, name) for name in sorted(out - names)]
        doubled += [(module, name) for name in sorted(out) if (module, name) in pinned]
    return missing, stale, doubled


def module_names(source):
    """Every top-level function, constant and method of a module, plus classes without methods, as qualified names."""
    names = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = [f'{node.name}.{item.name}' for item in node.body
                       if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            names.extend(methods or [node.name])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.extend(target.id for target in targets if isinstance(target, ast.Name))
    return names


def module_source(module):
    return Path(find_spec('prompt_enhancer.' + module).origin).read_text(encoding='utf-8')


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return tree


def _find(body, parts):
    """The definition or top-level assignment that `parts` names inside `body`; None when it is absent."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == parts[0]:
            return node if len(parts) == 1 else _find(node.body, parts[1:])
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if len(parts) == 1 and any(isinstance(target, ast.Name) and target.id == parts[0] for target in targets):
            return node
    return None


def entry_dump(source, name):
    """The normalised syntax tree of one inventoried name: no docstring, no line, column or comment."""
    node = _find(ast.parse(source).body, name.split('.'))
    if node is None:
        raise LookupError(name)
    return ast.dump(_strip_docstrings(ast.Module(body=[node], type_ignores=[])), include_attributes=False)


def stage_digest(stage, sources=None):
    """Digest of the whole inventory of `stage`; `sources` may replace a module's text (scratch copies in tests)."""
    sources = sources or {}
    digest = sha256()
    for module, name in STAGES[stage][2]:
        text = sources[module] if module in sources else module_source(module)
        digest.update(f'{module}:{name}\n{entry_dump(text, name)}\n'.encode('utf-8'))
    return digest.hexdigest()
