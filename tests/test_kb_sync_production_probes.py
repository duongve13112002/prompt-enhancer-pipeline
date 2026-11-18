"""Production CLI acceptance against an owned loopback HTTP double; no inference."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
from pathlib import Path
import shutil
from threading import Event, Thread

import httpx
from PIL import Image
import yaml

from k2_fixtures import folder, source_tree
from kb_sync_fixtures import SyncServer
from prompt_enhancer import __main__ as cli
from prompt_enhancer import model_schemas
from prompt_enhancer.model_client import ModelClientRun


class ProductionServer(SyncServer):
    """Labelled semantic/embedding double; image challenges are answered from pixels."""

    def __init__(self):
        super().__init__()
        self.probe_requests = []

    def answer(self, route, body):
        request = httpx.Request('POST', 'http://127.0.0.1' + route, json=body)
        if route == '/v1/embeddings':
            return self.embedder.handler(request).json()
        messages = body['messages']
        content = messages[-1]['content']
        text = content[0]['text'] if isinstance(content, list) else content
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and any(key in payload for key in (
            'folder_name', 'concept_names', 'facets', 'card_summary'
        )):
            return self.handler(request).json()
        self.probe_requests.append(body)
        counts = []
        if isinstance(content, list):
            for part in content:
                if part['type'] != 'image_url':
                    continue
                encoded = part['image_url']['url'].split(',', 1)[1]
                with Image.open(BytesIO(base64.b64decode(encoded))) as image:
                    # Count disjoint dark runs across the synthetic circles' center line.
                    row = [sum(image.convert('RGB').getpixel((x, image.height // 2))) < 384
                           for x in range(image.width)]
                    counts.append(sum(dark and (x == 0 or not row[x - 1])
                                      for x, dark in enumerate(row)))
        if counts:
            value = counts[0] * 10 + counts[1] if len(counts) == 2 else counts[0]
            answer = {'value': value}
        else:
            # The labelled double echoes the literal object in the received instruction.
            answer, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}


@contextmanager
def loopback_server(double):
    """One bounded HTTP server thread, stopped and joined even after a failed assertion."""
    stopping = Event()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            response = json.dumps(double.answer(self.path, body)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 0.05

    def serve():
        while not stopping.is_set():
            server.handle_request()

    thread = Thread(target=serve, name='labelled-probe-http-double')
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        stopping.set()
        thread.join(timeout=3)
        server.server_close()
        assert not thread.is_alive(), 'owned HTTP double did not stop'


def test_kb_sync_cli_probes_without_injection(k2_env, monkeypatch, capsys):
    evidence = []
    close = ModelClientRun.close

    async def record_close(run):
        evidence.extend(run.client.probe_events)
        await close(run)

    monkeypatch.setattr(ModelClientRun, 'close', record_close)
    runs, start = [], ModelClientRun.__init__

    def record_run(run, *args, **kwargs):  # labelled recording wrapper around the real constructor
        start(run, *args, **kwargs)
        runs.append(run)

    monkeypatch.setattr(ModelClientRun, '__init__', record_run)
    builds = model_schemas._REGISTRIES.builds
    env = k2_env
    folder(env, 'declared', 'Declared\nSynthetic description')
    folder(env, 'unnamed', count=2)
    before = source_tree(env.source)
    config = env.config
    config['embedding']['query_text_template'] = '{text}'
    config['runtime']['cpu_workers'] = 1
    config['vlm']['backends']['main']['probe'] = {'max_requests': 20, 'timeout_s': 2}  # Test-only allowance for five contexts.
    # Bind only the CLI's project-root discovery to the tiny temporary project.
    monkeypatch.setattr(cli, '__file__', str(env.root / 'src/prompt_enhancer/__main__.py'))
    bundle = Path(__file__).resolve().parents[1] / 'contracts/probes/model_capabilities.v0.json'
    if bundle.exists():
        target = env.root / 'contracts/probes'
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle, target / bundle.name)
    double = ProductionServer()
    with loopback_server(double) as endpoint:
        for name in ('main', 'embedder'):
            config['vlm']['backends'][name]['base_urls'] = [endpoint]
        profile = env.root / 'production-probes.yaml'
        profile.write_text(yaml.safe_dump(config), encoding='utf-8')
        result = cli.main(['kb', 'sync', '--config', str(profile)])
        captured = capsys.readouterr()
        print('Run-owned probe evidence: ' + json.dumps(evidence, sort_keys=True))
        assert result == 0, captured.err + '\n' + json.dumps(evidence, sort_keys=True)
        capsys.readouterr()  # Keep diagnostic output separate from the next CLI JSON.
        published = json.loads(captured.out)
        assert published['status'] == 'published'
        assert {'info_assessor', 'folder_namer', 'observer', 'card_builder', 'alias_generator'} <= set(double.roles)
        assert len(double.probe_requests) == 20  # Assessor 2 + namer 8 + observer 6 + cards 2 + aliases 2.
        # One run shares transport, allowance and ledger; only identical request contexts share proofs.
        assert len(runs) == 1 and model_schemas._REGISTRIES.builds - builds <= 1
        count = (len(double.probe_requests), len(double.requests), len(double.embedder.requests))
        assert cli.main(['kb', 'sync', '--config', str(profile)]) == 0
        repeated = json.loads(capsys.readouterr().out)
        assert repeated['status'] == 'unchanged'
        assert repeated['snapshot_id'] == published['snapshot_id']
        assert count == (len(double.probe_requests), len(double.requests), len(double.embedder.requests))
    assert source_tree(env.source) == before
