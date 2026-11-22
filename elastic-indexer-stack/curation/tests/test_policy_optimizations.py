from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

import httpx
import pytest

from indexer import passages
from indexer.source_encoding import decode_source
from indexer.source_files import SourceError
import policy_preparation as preparation
from policy_maintenance import (MaintenanceServices,ServiceError,State,YieldWork,apply_prepared,
                                identity,repair,run)
from policy_pool import PreparationPool,WorkerLost,worker
from test_policy_maintenance import fixture,Services,V


def many(tmp_path):
    raw='<title>Guild news</title><main>'+''.join(
        '<p>'+f'Guild record {i}: '+'The guild explored Norrath together. '*80+'</p>' for i in range(5))+'</main>'
    hit,sources=fixture(tmp_path,raw)
    assert len(hit['_source']['text'])>3
    return hit,sources


def test_one_changed_input_reuses_other_exact_vectors_and_returns_every_offset(tmp_path):
    hit,sources=many(tmp_path);source=hit['_source'];old=deepcopy(source['text'])
    for row in source['text']:row['vector']=[2.0]+[0.0]*767
    source['text'][0]['title']='Old'
    services=Services([hit])
    try:assert repair(services,sources,hit['_id'],lambda:None)=='updated'
    finally:sources.close()
    assert len(services.inputs)==1
    saved=services.updates[0]['text']
    assert len(saved)==len(old) and saved[0]['vector']==V
    assert all(row['vector'][0]==2 for row in saved[1:])
    assert [(row['source_start'],row['source_end']) for row in saved]==[(row['source_start'],row['source_end']) for row in old]


@pytest.mark.parametrize('damage',['offset','overlap','view_hash','html_hash','unknown_version','metadata'])
def test_damaged_old_plan_disables_reuse_without_suppressing_a_valid_repair(tmp_path,damage):
    hit,sources=many(tmp_path);source=hit['_source'];source['text'][0]['title']='Old'
    if damage=='offset':source['text'][1]['source_start']+=1
    elif damage=='overlap':source['text'][1]['text_chunk']='X'+source['text'][1]['text_chunk'][1:]
    elif damage=='view_hash':source['text_passage_provenance']['sources'][0]['view_sha256']='0'*64
    elif damage=='html_hash':source['text_passage_provenance']['sources'][0]['html_sha256']='0'*64
    elif damage=='metadata':source['text'][1].pop('title')
    else:source['text_passage_provenance']['version']='unknown'
    expected=preparation.prepare(hit,sources).requests
    services=Services([hit])
    try:repair(services,sources,hit['_id'],lambda:None)
    finally:sources.close()
    assert services.inputs==list(expected)
    assert all(row['vector']==V for row in services.updates[0]['text'])


@pytest.mark.parametrize('bad',[None,[],[0.0]*768,[True]+[0.0]*767,[float('nan')]+[0.0]*767])
def test_invalid_saved_vectors_are_reembedded_before_the_atomic_write(tmp_path,bad):
    hit,sources=many(tmp_path);source=hit['_source'];source['text'][0]['title']='Old'
    source['text'][1]['vector']=bad
    services=Services([hit])
    try:repair(services,sources,hit['_id'],lambda:None)
    finally:sources.close()
    assert len(services.inputs)==2 and len(services.updates)==1
    assert all(row['vector']==V for row in services.updates[0]['text'])


def test_changed_record_between_preparation_and_vector_read_cannot_write(tmp_path):
    hit,sources=many(tmp_path);hit['_source']['text'][0]['title']='Old'
    prepared=preparation.prepare(hit,sources);services=Services([hit]);services.hits[hit['_id']]['_seq_no']+=1
    with pytest.raises(SourceError,match='concurrent_update'):apply_prepared(services,hit,prepared,lambda:None)
    assert services.inputs==services.updates==[]
    sources.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_source_bound_compatible_reader_is_not_reextracted(tmp_path,monkeypatch,legacy):
    hit,sources=many(tmp_path);source=hit['_source']
    raw=sources.read(source);decoded=decode_source(raw,'text/html')
    source['text_extraction_version']='html-2026-11-05-script-evidence'
    source['text_passage_provenance']['decoding']={**decoded.provenance(),'version':'source-decoding-v3'}
    if legacy:
        monkeypatch.setattr(preparation, 'renderer_identity', lambda: preparation.LEGACY_RENDERER)
    else:
        source['text_passage_provenance']['reader']=preparation.reader_proof(source['text_full'], decoded.text, decoded.provenance())
    monkeypatch.setattr(preparation.html_extraction,'website_markdown',lambda _:pytest.fail('Unchanged reader was reextracted'))
    monkeypatch.setattr(preparation,'BeautifulSoup',lambda *a,**k:pytest.fail('Unneeded title DOM parse'))
    result=preparation.prepare(hit,sources)
    assert result.reader==source['text_full'] and 'text_full' not in result.patch
    assert result.patch['text_extraction_version']==preparation.html_extraction.WEBSITE_EXTRACTION_VERSION
    assert result.patch['text_passage_provenance']['reader']['source_sha256']==hashlib.sha256(raw).hexdigest()
    sources.close()


@pytest.mark.parametrize('change',['renderer','decoder','html','checkpoint'])
def test_reader_compatibility_never_waives_changed_source_or_renderer_proof(tmp_path,monkeypatch,change):
    hit,sources=fixture(tmp_path);source=hit['_source'];raw=sources.read(source);decoded=decode_source(raw,'text/html')
    source['text_extraction_version']='html-2026-11-05-script-evidence'
    source['text_passage_provenance']['decoding']=decoded.provenance()
    if change=='renderer':monkeypatch.setattr(preparation,'renderer_identity',lambda:'different')
    elif change=='decoder':source['text_passage_provenance']['decoding']['reason']='different'
    elif change=='html':source['text_passage_provenance']['sources'][0]['html_sha256']='0'*64
    else:source['text_extraction_version']='unknown-old-renderer'
    prepared=preparation.prepare(hit,sources)
    assert 'text_full' in prepared.patch and 'Nagafen' in prepared.reader
    sources.close()


def test_new_reader_proof_survives_checkpoint_names_but_not_new_renderer_or_bytes(tmp_path,monkeypatch):
    hit,sources=fixture(tmp_path);source=hit['_source'];raw=sources.read(source).decode();decoding=decode_source(raw.encode(),'text/html').provenance()
    source['text_extraction_version']='separate-decoder-upgrade'
    source['text_passage_provenance']['reader']=preparation.reader_proof(source['text_full'],raw,decoding)
    assert preparation.reader_reusable(source,source['text_full'],raw,decoding)
    monkeypatch.setattr(preparation,'renderer_identity',lambda:'changed-renderer')
    assert not preparation.reader_reusable(source,source['text_full'],raw,decoding)
    sources.close()


def test_preparation_result_must_match_its_reader_and_exact_requests(tmp_path):
    from dataclasses import replace
    hit,sources=fixture(tmp_path,changed=True);prepared=preparation.prepare(hit,sources)
    for bad in (replace(prepared,identifier='wrong'),replace(prepared,reader='wrong'),replace(prepared,requests=('wrong',)),
                replace(prepared,reusable=(('wrong',0),))):
        services=Services([hit])
        with pytest.raises(SourceError):apply_prepared(services,hit,bad,lambda:None)
        assert not services.inputs and not services.updates
    sources.close()


def test_changed_request_compiler_cannot_reuse_historical_vectors(tmp_path,monkeypatch):
    hit,sources=many(tmp_path);hit['_source']['text'][0]['title']='Old'
    assert preparation.request_identity()==preparation.LEGACY_REQUEST
    monkeypatch.setattr(preparation,'request_identity',lambda:'changed-prefix-or-tokenizer')
    prepared=preparation.prepare(hit,sources)
    assert not prepared.reusable
    services=Services([hit]);apply_prepared(services,hit,prepared,lambda:None)
    assert services.inputs==list(prepared.requests)
    sources.close()


def test_renderer_fingerprint_includes_the_reader_header_builder(monkeypatch):
    expected=preparation.renderer_identity()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(preparation,'website_reader',lambda raw,identifier:raw)
            preparation.renderer_identity.cache_clear()
            assert preparation.renderer_identity()!=expected
    finally:preparation.renderer_identity.cache_clear()
    assert preparation.renderer_identity()==expected


def test_request_fingerprint_includes_title_normalization(monkeypatch):
    expected=preparation.request_identity()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(passages,'_plain',lambda value:value)
            preparation.request_identity.cache_clear()
            assert preparation.request_identity()!=expected
    finally:preparation.request_identity.cache_clear()
    assert preparation.request_identity()==expected


def test_renderer_fingerprint_includes_archive_header_configuration(monkeypatch):
    expected=preparation.renderer_identity()
    try:
        with monkeypatch.context() as patch:
            patch.setenv('WEBSITES_PATH','different-archive-root/')
            preparation.renderer_identity.cache_clear()
            assert preparation.renderer_identity()!=expected
    finally:preparation.renderer_identity.cache_clear()
    assert preparation.renderer_identity()==expected


def test_optional_vector_read_is_same_revision_bounded_and_fail_closed(tmp_path):
    hit,_=many(tmp_path);service=MaintenanceServices.__new__(MaintenanceServices)
    service.es_url='https://archive';service.index=hit['_index']
    def response(request):
        assert request.url.params['_source_includes']=='id,embedding_model,text.vector'
        return httpx.Response(200,json=hit)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        service.client=client;assert service.stored_vectors(hit)==[V]*len(hit['_source']['text'])
    changed=deepcopy(hit);changed['_seq_no']+=1
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=changed))) as client:
        service.client=client
        with pytest.raises(SourceError,match='concurrent_update'):service.stored_vectors(hit)
    service.request=lambda *a,**k:(_ for _ in ()).throw(SourceError('record_too_large'))
    assert service.stored_vectors(hit)==[]


def test_spawned_pool_keeps_results_identical_and_advances_only_acknowledged_records(tmp_path):
    first,sources=many(tmp_path);second=deepcopy(first);second['_id']=second['_source']['id']=first['_id'].replace('index.html','later.html')
    target=sources.roots[0]/second['_id'];target.write_bytes((sources.roots[0]/first['_id']).read_bytes())
    # This record's plan is intentionally stale; its new URL is independently prepared.
    services=Services([first,second]);state=State(tmp_path/'state')
    pool=PreparationPool(sources.roots,sources.publication,2)
    try:
        result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-1',maximum=2,should_yield=lambda:False,pool=pool)
        assert result['scanned']==2 and result['failed']==0 and len(services.updates)==2
        assert result['execution']['preparation_workers']==2
        assert state.snapshot()['cursor']==second['_id']
        assert len(pool.slots)==2 and all(slot.process.is_alive() for slot in pool.slots)
    finally:pool.close();state.close();sources.close()
    assert pool.slots==[]


def test_pool_cancels_speculative_work_on_yield_and_bounds_one_slow_record(tmp_path):
    hit,sources=fixture(tmp_path);pool=PreparationPool(sources.roots,sources.publication,1,timeout=.001)
    try:
        pending=pool.submit(hit)
        with pytest.raises(SourceError,match='preparation_timeout'):pending.result(lambda:None)
        assert pool.slots==[]
        pool.timeout=120;pending=pool.submit(hit)
        def stop():raise YieldWork()
        with pytest.raises(YieldWork):pending.result(stop)
        assert pool.slots==[]
    finally:pool.close();sources.close()


def test_dead_child_has_exact_identity_and_can_be_replaced(tmp_path):
    hit,sources=fixture(tmp_path);pool=PreparationPool(sources.roots,sources.publication,1)
    try:
        pending=pool.submit(hit);pending.slot.process.kill();pending.slot.process.join(timeout=2)
        with pytest.raises(WorkerLost):pending.result(lambda:None)
        assert pool.slots==[]
        assert pool.submit(hit).result(lambda:None).identifier==hit['_id']
    finally:pool.close();sources.close()


def test_recycled_processes_leave_no_live_children_or_stale_pipe_messages(tmp_path):
    hit,sources=fixture(tmp_path);pool=PreparationPool(sources.roots,sources.publication,1,tasks=1)
    try:
        first=pool.submit(hit);result=first.result(lambda:None);pid=first.slot.process.pid
        second=pool.submit(hit);assert second.result(lambda:None)==result and second.slot.process.pid!=pid
    finally:pool.close();sources.close()


def test_source_failure_in_child_retains_reason_and_hash_and_later_work_is_available(tmp_path):
    hit,sources=fixture(tmp_path);path=sources.roots[0]/hit['_id'];path.write_text('changed bytes')
    pool=PreparationPool(sources.roots,sources.publication,1)
    try:
        with pytest.raises(SourceError,match='source_integrity') as error:pool.submit(hit).result(lambda:None)
        assert error.value.source_sha256==hashlib.sha256(b'changed bytes').hexdigest()
        assert len(pool.slots)==1 and not pool.slots[0].busy
    finally:pool.close();sources.close()


def test_hard_preparation_deaths_retry_the_exact_id_then_quarantine_and_continue(tmp_path):
    hit,sources=fixture(tmp_path,changed=True,identifier='websites/guild.example/20000101000000/a.html')
    later,_=fixture(tmp_path,changed=True,identifier='websites/guild.example/20000101000000/b.html')
    services=Services([hit,later]);state=State(tmp_path/'state')
    class Task:
        def __init__(self,hit):self.hit=hit
        def result(self,check):
            if self.hit['_id']==hit['_id']:raise WorkerLost()
            return preparation.prepare(self.hit,sources)
        def cancel(self):pass
    class Pool:
        workers=2
        def submit(self,hit):return Task(hit)
        def cancel_pending(self):pass
    try:
        for attempt in range(1,4):
            result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-'+str(attempt),should_yield=lambda:False,pool=Pool())
            assert result['phase']=='retry_wait' and state.inflight()==hit['_id']
            assert result['failed']==0 and not services.updates
        result=run(tmp_path,state,services,sources,'eqarchives-policy-'+identity()+'-4',should_yield=lambda:False,pool=Pool())
        assert result['unresolved']==1 and result['updated']==1 and state.inflight() is None
        assert state.db.connection().execute('SELECT id,reason FROM policy_failures').fetchone()[:]==(hit['_id'],'repeated_worker_interruption')
    finally:state.close();sources.close()


def test_inline_worker_has_no_network_or_state_clients_and_returns_only_safe_errors(tmp_path,monkeypatch):
    hit,sources=fixture(tmp_path)
    class Pipe:
        def __init__(self):self.incoming=iter([hit,None]);self.sent=[];self.closed=False
        def recv(self):return next(self.incoming)
        def send(self,value):self.sent.append(value)
        def close(self):self.closed=True
    pipe=Pipe()
    monkeypatch.setattr(httpx,'Client',lambda *a,**k:pytest.fail('CPU worker opened a network client'))
    worker(pipe,sources.roots,sources.publication,2)
    assert pipe.closed and pipe.sent[0][0]=='ready'
    assert pipe.sent[0][1].identifier==hit['_id']
    (sources.roots[0]/hit['_id']).write_text('changed source')
    other=Pipe();worker(other,sources.roots,sources.publication,2)
    assert other.sent[0][0:2]==('source','source_integrity') and 'changed source' not in str(other.sent)
    sources.close()


@pytest.mark.parametrize('kwargs',[{'workers':0},{'workers':True},{'workers':5},{'tasks':0},{'tasks':257},{'timeout':0},{'timeout':float('nan')}])
def test_preparation_pool_bounds_are_explicit(kwargs):
    with pytest.raises(ValueError):PreparationPool([],None,**kwargs)


def test_forum_negative_guard_retains_all_supported_dom_roles():
    from bs4 import BeautifulSoup
    from indexer.passage_forums import vbulletin_possible
    assert not vbulletin_possible(BeautifulSoup('<td class="msg_text_cell">A Lithium message</td>','html.parser'))
    for raw in ('<div class="posttext"></div>','<tr class="archivebody"></tr>',
                '<div id="post_message_17"></div>','<div id="p17e"></div>',
                '<!--\n message \n-->','<div id="post&#95;message_17"></div>'):
        assert vbulletin_possible(BeautifulSoup(raw,'html.parser'))
