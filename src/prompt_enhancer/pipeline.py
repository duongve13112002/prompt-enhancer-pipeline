"""Production S0-S6 library coordinator; interfaces/delivery transport belong to T-016."""
import asyncio
import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from inspect import isawaitable
import json
from pathlib import Path
import time

from .assembly import build_assembly
from .candidates import build_candidates, query_embedder_settings
from .config import ConfigurationError, effective_request_config, validate_pipeline_reads
from .envelope import build_envelope, build_trace_row
from .intake import RETENTION_MESSAGE, AttachmentRetentionUnavailable, intake, refuse_attachments
from .model_client import ModelCallError, compose_model_client
from .model_json import strict_json
from .planner import plan
from .profile_prompt import load_prompt, sha256_tag
from .response_lifecycle import LifecycleFull, ResponseLifecycle
from .storage import StorageError
from .trace_store import TraceRepository, trace_expiry
from .validation import _decoded_output, _obligations, validate, validate_verifier_reply
from .verifier_policy import provenance_matches, require_calibrated_verifier
from .writer import prepare_write, write


@dataclass
class PipelineResponse:
    envelope: dict
    attachment_bytes: dict
    ticket: object

    async def after_delivery(self):
        await self.ticket.after_delivery()


async def monitor_output(s0, planned, assembled, checked, owner, *, project_root):
    """One text-only calibrated check after delivery; no repair or delivered-envelope mutation."""
    config, profile = s0.payload['effective_config'], s0.profile
    bound = require_calibrated_verifier(config, profile, project_root=project_root)
    if not checked['calls'] or not provenance_matches(strict_json(checked['calls'][-1]['record_json']), bound['writer_models']):
        raise ValueError('monitored writer was outside calibrated routes')
    client = await owner.for_role('verifier')
    intent = planned['intent_ir']
    pack = None if assembled is None else assembled['pack']
    entries, _, _ = _obligations(intent, profile, pack)
    if not entries:
        entries = [{'id': '@s6:monitor', 'type': 'intent', 'text': 'Preserve the original user intent.'}]
    output = _decoded_output(checked['output'], profile)
    data = {'obligations': entries, 'candidate': output, 'prompt': s0.payload['prompt'],
            'facts': [] if pack is None else pack['facts'], 'delta': intent['delta'], 'preserve': intent['preserve'],
            'dependent_effects': intent['dependent_effects']}
    reply, record = await client.call(load_prompt(project_root, config['vlm']['roles']['verifier']['prompt_id']),
                                      json.dumps(data, ensure_ascii=False, allow_nan=False))
    if record['status'] != 'OK' or not provenance_matches(record, bound['verifier_models']):
        raise ValueError('monitor did not complete on a calibrated route')
    results = validate_verifier_reply(reply, [entry['id'] for entry in entries], output, project_root=project_root)
    return {'status': 'checked', 'results': results, 'calls': [record]}


def require_attachment_copies(s0, assembled):
    """Right after S3: the selected attach images must read back hash-checked, before S4 can name them."""
    pack = None if assembled is None else assembled['pack']
    for item in [] if pack is None else pack['images']:
        if item['channel'] == 'attach':
            try:
                s0.snapshot.read_image(item['image_id'])
            except (StorageError, OSError):
                raise AttachmentRetentionUnavailable(RETENTION_MESSAGE) from None


def retain_attachments(s0, assembled, expires_at):
    """Read only hashed files in the pinned manifest, then hold before the original lease is released."""
    pack = None if assembled is None else assembled['pack']
    images = [] if pack is None else [item for item in pack['images'] if item['channel'] == 'attach']
    if not images:
        return {}
    if s0.payload['task'] != 'edit' or not s0.profile.manifest['renderer']['accepts_reference_images']:
        raise StorageError('profile cannot receive knowledge attachments')
    if pack['snapshot_id'] != s0.snapshot.snapshot_id or pack['request_id'] != s0.payload['request_id']:
        raise StorageError('attachment pack differs from pinned request')
    data = {item['image_id']: s0.snapshot.read_image(item['image_id']) for item in images}
    s0.knowledge.store.hold(s0.snapshot.snapshot_id, s0.payload['request_id'], expires_at)
    return data


async def _cleanup(diagnostics, *closers):
    """Join teardown even when the request was cancelled or an independent closer fails."""
    async def close_all():
        for closer in closers:
            if closer is not None:
                try:
                    result = closer()
                    if isawaitable(result):
                        await result
                except (Exception, asyncio.CancelledError):
                    diagnostics.append('RESOURCE_CLOSE_FAILED')

    task = asyncio.create_task(close_all())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class Pipeline:
    def __init__(self, config, *, project_root):
        self.config, self.root = deepcopy(config), Path(project_root).resolve()
        self.runtime = validate_pipeline_reads(config, project_root=self.root)
        # Open KB storage for retention only when an old trace actually has a hold to release.
        release_hold = None
        if config['rag']['enabled']:
            def release_hold(snapshot_id, request_id):
                from .storage import open_knowledge_store
                store = open_knowledge_store(self.config, 'enhance once', project_root=self.root)
                store.release_hold(snapshot_id, request_id)
        self.repository = TraceRepository(config, project_root=self.root,
                                           retention_s=self.runtime['trace']['retention_s'], release_hold=release_hold)
        self._loop, self._maintenance_task = None, None
        self._closing = None
        self._requests = set()
        self._changed = asyncio.Event()
        self.lifecycle = ResponseLifecycle(self.runtime, self._append_trace)

    def _append_trace(self, row):
        result = self.repository.append(row)
        self._loop.call_soon_threadsafe(self._changed.set)
        return result

    def _start(self):
        if self._closing is not None:
            raise LifecycleFull('pipeline is closing')
        if self._maintenance_task is None:
            self._loop = asyncio.get_running_loop()
            self._maintenance_task = asyncio.create_task(self._maintain())

    async def _maintain(self):
        while True:
            self._changed.clear()
            try:
                expiry = await self.lifecycle.io(self.repository.next_expiry)
                timeout = None if expiry is None else max(0, (expiry - datetime.now(timezone.utc)).total_seconds())
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=timeout)
                except TimeoutError:
                    await self.lifecycle.io(self.repository.expire)
            except (StorageError, OSError, OverflowError):
                self.lifecycle.errors.append('TRACE_RETENTION_FAILED')
                await self._changed.wait()

    async def __aenter__(self):
        self._start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        async def drain():
            if self._requests:
                await asyncio.gather(*self._requests)
            if self._maintenance_task is not None:
                self._maintenance_task.cancel()
                await asyncio.gather(self._maintenance_task, return_exceptions=True)
            await self.lifecycle.close()
            try:
                self.repository.expire()
            except (StorageError, OSError, OverflowError):
                self.lifecycle.errors.append('TRACE_RETENTION_FAILED')
        if self._closing is None:
            self._closing = asyncio.create_task(drain())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            await asyncio.shield(self._closing)
            raise

    async def enhance(self, request):
        return await self._enhance(request)

    async def _enhance(self, request, profile=None):
        self._start()
        finished = asyncio.get_running_loop().create_future()
        self._requests.add(finished)
        try:
            return await self._run_request(request, profile)
        finally:
            self._requests.discard(finished)
            finished.set_result(None)

    async def _run_request(self, request, profile):
        started = time.monotonic()
        try:
            effective = effective_request_config(self.config, request.get('overrides', {}) if isinstance(request, dict) else {},
                                                   project_root=self.root)
        except ConfigurationError:
            effective = self.config  # S0 returns the precise request refusal without starting any model work.
        runtime = validate_pipeline_reads(effective, project_root=self.root)
        if runtime != self.runtime or effective['storage'] != self.config['storage'] or effective['rag']['enabled'] != self.config['rag']['enabled']:
            raise ConfigurationError('request deployment overrides require a separately owned Pipeline lifecycle')
        token = self.lifecycle.reserve()
        s0 = None
        owner = embedder = None
        handed_off = fatal = False
        shortlist = planned = assembled = written = checked = None
        diagnostics, attachments = [], {}
        release_hold = brief = created_at = None
        try:
            try:
                async with asyncio.timeout(runtime['request_deadline_s'] - (time.monotonic() - started)):
                    s0 = intake(request, self.config, project_root=self.root, profile_view=profile)
                    if s0.payload['outcome'] != 'refused':
                        if effective['rag']['enabled']:
                            from .k1_embed import Embedder
                            embedder = Embedder(query_embedder_settings(effective))
                            shortlist = await build_candidates(s0, embedder=embedder)
                        owner = await compose_model_client(effective, 'planner', project_root=self.root)
                        planned = await plan(s0, shortlist, owner, project_root=self.root, embedder=embedder)
                        if planned['state'] != 'needs_clarification':
                            writer = None
                            if planned['intent_ir'] is None:
                                written = {'state': 'failed', 'output': None, 'calls': [], 'warnings': [],
                                           'error': {'code': 'INTENT_UNAVAILABLE', 'message': 'S4: no usable plan'},
                                           'brief_hash': sha256_tag(b''),
                                           'system_prompt_hash': sha256_tag(s0.profile.system_message), 'addendum_hash': None}
                            else:
                                if effective['rag']['enabled']:
                                    assembled = build_assembly(s0, planned, project_root=self.root)
                                    require_attachment_copies(s0, assembled)
                                prepared = prepare_write(s0, planned, assembled, project_root=self.root)
                                brief = prepared.text
                                binding_error = None
                                try:
                                    writer = await owner.for_role('writer')
                                except ModelCallError as error:
                                    binding_error = error
                                written = await write(s0, planned, assembled, writer, project_root=self.root,
                                                      prepared=prepared, binding_error=binding_error)
                            checked = await validate(s0, planned, assembled, written, writer, project_root=self.root)
                            if checked['output'] is not None:
                                created_at = datetime.now(timezone.utc)
                                try:
                                    expires = trace_expiry(created_at, runtime['trace']['retention_s'])
                                    attachments = retain_attachments(s0, assembled, expires)
                                    if attachments:
                                        release_hold = partial(s0.knowledge.store.release_hold,
                                                               s0.payload['retrieval']['snapshot_id'],
                                                               s0.payload['request_id'])
                                except (StorageError, OSError, OverflowError):
                                    diagnostics.append('ATTACHMENT_UNAVAILABLE')
            except AttachmentRetentionUnavailable as error:
                # Same refusal intake gives; S4 never ran, so nothing promised an attachment.
                refuse_attachments(s0, error)
                shortlist = planned = assembled = None
            except (TimeoutError, ModelCallError, StorageError) as error:
                if s0 is None:
                    raise
                diagnostics.append('REQUEST_DEADLINE' if isinstance(error, TimeoutError) else type(error).__name__)
                fatal = True
            finally:
                await _cleanup(diagnostics, embedder.close if embedder is not None else None,
                               s0.close if s0 is not None else None)

            monitor = None
            if not fatal and checked and checked['output'] is not None and checked['monitoring_selected'] and owner is not None:
                monitor = partial(monitor_output, s0, planned, assembled, checked, owner, project_root=self.root)
            elif owner is not None:
                closer, owner = owner.close, None
                await _cleanup(diagnostics, closer)
            envelope = build_envelope(s0, planned, assembled, checked, shortlist=shortlist, fatal=fatal,
                                      attachments_ready=bool(attachments), project_root=self.root)
            if not fatal and time.monotonic() - started >= runtime['request_deadline_s']:
                diagnostics.append('REQUEST_DEADLINE')
                fatal = True
                envelope = build_envelope(s0, planned, assembled, checked, shortlist=shortlist,
                                          fatal=True, project_root=self.root)
                if monitor is not None:
                    monitor = None
                    closer, owner = owner.close, None
                    await _cleanup(diagnostics, closer)
            if fatal and release_hold is not None:
                closer, release_hold = release_hold, None
                await _cleanup(diagnostics, closer)
                attachments = {}
            row = build_trace_row(s0, envelope, shortlist=shortlist, planned=planned, assembled=assembled,
                                  written=written, checked=checked, latency_ms=(time.monotonic() - started) * 1000,
                                  diagnostics=diagnostics, created_at=created_at, model_config=effective)
            row['body']['attachments_retained'] = bool(attachments)
            if brief is not None:
                row['body']['brief'] = brief
            ticket = self.lifecycle.submit(token, row, monitor=monitor, cleanup=owner.close if monitor is not None else None)
            handed_off = True
            return PipelineResponse(envelope, attachments, ticket)
        finally:
            if not handed_off:
                self.lifecycle.cancel(token)
                await _cleanup(self.lifecycle.errors, owner.close if owner is not None else None, release_hold)


class GoldenAdapter:
    """Production golden runner for a confirmed ProfileView before its activation exists."""
    identity = 's0-s6.v1'  # artifact-bound adapter revision

    def __init__(self, pipeline):
        self.pipeline = pipeline

    async def run(self, profile, request):
        request = dict(request, profile_id=profile.profile_id)
        if isinstance(request.get('source_image'), bytes):
            request['source_image'] = base64.b64encode(request['source_image']).decode('ascii')
        response = await self.pipeline._enhance(request, profile)
        await response.after_delivery()
        return response.envelope
