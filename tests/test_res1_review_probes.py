"""Independent RES-1 review probes; labelled doubles only, no sockets or models."""
import asyncio
import hashlib
import json
import threading
import time
from copy import deepcopy
import httpx
import pytest
from k1_fixtures import EmbeddingServer, image_bytes, k0_handoff, k1_profile, make_source
from storage_fixtures import make_store
from prompt_enhancer import k1_embed

class BatchSensitiveServer(EmbeddingServer):
    """Labelled valid embedding double whose vectors depend on batch size (bf16 sensitivity)."""
    def handler(self, request):
        body=json.loads(request.content); self.requests.append((str(request.url),body))
        items=self.items(body)
        data=[{'object':'embedding','index':i,'embedding':[1.,len(items)/10.,.2,.3]} for i,item in enumerate(items)]
        return httpx.Response(200,json={'object':'list','data':data})

def test_resume_preserves_batch_composition(project_root,tmp_path,model_test_profile):
    valid=[image_bytes(color=(i*30,2,3)) for i in range(4)]
    ordered=sorted(hashlib.sha256(data).hexdigest() for data in valid)
    for i in range(10000):
        bad=f'invalid {i}'.encode(); digest=hashlib.sha256(bad).hexdigest()
        if ordered[0]<digest<ordered[1]: break
    else: raise AssertionError('no synthetic invalid image at desired position')
    files={f'{i}.png':data for i,data in enumerate(valid)}; files['bad.png']=bad
    source=make_source(tmp_path/'source',files)
    config=k1_profile(model_test_profile,batch_size=2); config['runtime']['cpu_workers']=1
    store=make_store(project_root,tmp_path/'stopped'); handoff=k0_handoff(store,source)
    async def stop():
        started=asyncio.Event()
        async def blocked(request):
            started.set(); await asyncio.Event().wait()
        task=asyncio.create_task(k1_embed.embed_images(store,config,handoff,knowledge_root=source.resolve(),transport=httpx.MockTransport(blocked)))
        await asyncio.wait_for(started.wait(),5)
        await asyncio.sleep(.05) # allow both admitted chunks to finish preparing; no model answer is returned
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(stop())
    resumed_server=BatchSensitiveServer()
    resumed=asyncio.run(k1_embed.embed_images(store,config,handoff,knowledge_root=source.resolve(),transport=resumed_server.transport()))
    fresh_store=make_store(project_root,tmp_path/'fresh'); fresh_server=BatchSensitiveServer()
    clean=asyncio.run(k1_embed.embed_images(fresh_store,config,handoff,knowledge_root=source.resolve(),transport=fresh_server.transport()))
    print('RESUME batch sizes',resumed_server.input_counts(),'CLEAN',fresh_server.input_counts())
    assert resumed['image_embeddings'].equals(clean['image_embeddings']), 'resume changed vectors under the same checkpoint identity'

def test_stop_does_not_block_event_loop_in_decode_shutdown(project_root,tmp_path,model_test_profile,monkeypatch):
    source=make_source(tmp_path/'source',{'a.png':image_bytes()})
    store=make_store(project_root,tmp_path/'kb'); handoff=k0_handoff(store,source)
    config=k1_profile(model_test_profile); config['runtime']['cpu_workers']=1
    entered=threading.Event(); release=threading.Event(); original=k1_embed._decode
    def blocked(data,settings):
        entered.set(); release.wait(timeout=1.0); return original(data,settings)
    monkeypatch.setattr(k1_embed,'_decode',blocked)
    async def exercise():
        task=asyncio.create_task(k1_embed.embed_images(store,config,handoff,knowledge_root=source.resolve(),transport=EmbeddingServer().transport()))
        while not entered.is_set(): await asyncio.sleep(.001)
        ticks=[]
        async def release_on_loop():
            await asyncio.sleep(.02); ticks.append(time.monotonic()); release.set()
        ticker=asyncio.create_task(release_on_loop()); begin=time.monotonic(); task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError): await task
        finally:
            release.set(); await ticker
        delay=time.monotonic()-begin
        print('STOP duration',round(delay,3),'event loop release delay',round(ticks[0]-begin,3))
        assert ticks[0]-begin<.5, 'pool.shutdown blocked the event loop until running decoder timed out'
    asyncio.run(exercise())

_review_profile = k1_profile
def k1_profile(profile, **overrides):
    config = _review_profile(profile, **overrides)
    if overrides.get('batch_size') == 2:
        config['vlm']['backends']['embedder']['max_concurrency'] = 3
    return config
