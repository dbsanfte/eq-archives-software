import copy
import hashlib
import json
from pathlib import Path
import subprocess

import httpx
import pytest

from indexer.discussions import Catalog
from index_discussions import BackfillInventory, Elasticsearch, annotate, backfill, read_source

HOST='forums.example.org'
PREFIX='websites/'+HOST+'/20061213163803/'
FEED=(b'<rss><channel><title>Re: Topic</title><link>http://forums.example.org/eq/board/message?board.id=Board&amp;message.id=20</link>'
      b'<item><title>Topic</title><link>http://forums.example.org/eq/board/message?board.id=Board&amp;message.id=10#M10</link><description>Body</description></item></channel></rss>')
PRINT=(b'<html><a href="/eq/board/message?board.id=Board&amp;message.id=20">Topic</a><td class="msg_text_cell">Body</td>'
       b'<td class="msg_user_cell">User</td><td class="msg_date_cell">12-12-2006</td></html>')


class Client:
    def __init__(self, sources):
        self.sources=sources;self.writes=[]
    def records(self, keys):
        return {key:copy.deepcopy({'_id':key,'found':key in self.sources,'_seq_no':1,'_primary_term':1,
            '_source':self.sources.get(key,{})}) for key in keys}
    def update(self, record, discussion):
        self.writes.append((record['_id'],discussion));self.sources[record['_id']]['discussion']=discussion;return True


@pytest.fixture
def prepared(tmp_path):
    repo=tmp_path/'archive';repo.mkdir()
    paths={'rss/message?board.id=Board&message.id=20':FEED,'eq/board/print?board.id=Board&message.id=20&page=1':PRINT,
           'ordinary.html':b'<html>ordinary</html>'}
    for path,raw in paths.items():
        destination=repo/(PREFIX+path);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(raw)
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Fixture'],check=True)
    sources={PREFIX+path:{'id':PREFIX+path,'url':'https://web.archive.org/web/20061213163803/http://'+HOST+'/'+path,
                         'last_indexed':'original','text_full':'existing reader','text':[{'vector':[1,2,3]}]}
             for path in paths if path!='ordinary.html'}
    return repo,sources


def test_two_pass_backfill_handles_print_before_feed_preserves_data_and_resumes_without_rewriting(prepared,tmp_path):
    repo,sources=prepared;client=Client(sources);before=copy.deepcopy(sources)
    catalog=Catalog(tmp_path/'catalog.sqlite');state=tmp_path/'inventory.sqlite'
    inventory=BackfillInventory(repo,state)
    result=backfill(inventory,catalog,client,[HOST]);assert result['complete'] and not result['pending_gaps']
    assert result['updated']==2 and len({v['discussion']['key'] for v in client.sources.values()})==1
    assert all({k:v for k,v in value.items() if k!='discussion'}==before[key] for key,value in sources.items())
    assert inventory.git('remote').strip()==b''
    inventory.close();catalog.close()
    inventory=BackfillInventory(repo,state);catalog=Catalog(tmp_path/'catalog.sqlite')
    result=backfill(inventory,catalog,client,[HOST]);assert result['read']==result['updated']==0 and len(client.writes)==2
    inventory.close();catalog.close()


def test_operation_bounds_checkpoint_each_source_and_continue_to_completion(prepared,tmp_path):
    repo,sources=prepared;client=Client(sources);inventory=BackfillInventory(repo,tmp_path/'inventory.sqlite');catalog=Catalog(tmp_path/'catalog.sqlite')
    results=[]
    for _ in range(8):
        result=backfill(inventory,catalog,client,[HOST],max_files=1);results.append(result)
        if result['complete']:break
    assert results[-1]['complete'] and any(not value['complete'] for value in results)
    assert len(client.writes)==2 and sum(row['learned'] for row in results)==1
    inventory.close();catalog.close()


def test_metadata_update_requires_exact_record_source_identity_and_hash(tmp_path):
    catalog=Catalog(tmp_path/'catalog.sqlite');key=PREFIX+'rss/message?board.id=Board&message.id=20'
    row={'_id':key,'found':True,'_source':{'id':key,'url':'https://web.archive.org/web/20061213163803/http://'+HOST+'/rss/message?board.id=Board&message.id=20'}}
    assert annotate(row,FEED,catalog)['topic']=='10'
    for change in ({'id':'different'},{'parent_id':'parent'},{'archive_source_sha256':'0'*64}):
        damaged=copy.deepcopy(row);damaged['_source'].update(change)
        with pytest.raises(ValueError):annotate(damaged,FEED,catalog)
    assert annotate({'found':False},FEED,catalog) is None
    catalog.close()


def test_nonindexed_pages_are_not_created_and_conflicts_remain_visible(prepared,tmp_path):
    repo,sources=prepared;client=Client(sources);client.update=lambda *args:False
    inventory=BackfillInventory(repo,tmp_path/'inventory.sqlite');catalog=Catalog(tmp_path/'catalog.sqlite')
    result=backfill(inventory,catalog,client,[HOST]);assert result['conflicts']==2 and result['pending_gaps']==2
    assert not client.writes and all('discussion' not in x for x in sources.values())
    inventory.close();catalog.close()
    inventory=BackfillInventory(repo,tmp_path/'empty.sqlite');empty=Client({});catalog=Catalog(tmp_path/'other.sqlite')
    result=backfill(inventory,catalog,empty,[HOST]);assert result['not_indexed']==2 and result['updated']==0
    inventory.close();catalog.close()


def test_inventory_rejects_scope_changes_and_changed_source_blobs(prepared,tmp_path):
    repo,sources=prepared;inventory=BackfillInventory(repo,tmp_path/'inventory.sqlite')
    inventory.scope([HOST])
    for scope in ([],['bad/host'],['other.example.org']):
        with pytest.raises(ValueError):inventory.scope(scope)
    key=next(iter(sources));raw=(repo/key).read_bytes();blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    assert read_source(inventory,key,blob)==raw
    (repo/key).write_bytes(b'changed')
    with pytest.raises(ValueError):read_source(inventory,key,blob)
    inventory.close()


def test_http_adapter_requests_only_metadata_and_changes_only_discussion_with_concurrency_tokens(tmp_path):
    password=tmp_path/'password';password.write_text('dummy')
    adapter=Elasticsearch('http://es.invalid','eq-archive',password);adapter.client.close();requests=[]
    def transport(request):
        body=json.loads(request.content);requests.append((request,body))
        if '_mget' in request.url.path:
            return httpx.Response(200,json={'docs':[{'_id':'id','found':True}]})
        return httpx.Response(200,json={'result':'updated'})
    adapter.client=httpx.Client(transport=httpx.MockTransport(transport))
    assert adapter.records(['id'])['id']['found']
    assert adapter.update({'_id':'id','_seq_no':3,'_primary_term':2},{'key':'proof'})
    assert requests[-1][1]=={'doc':{'discussion':{'key':'proof'}},'if_seq_no':3,'if_primary_term':2}
    assert 'text_full' not in str(requests[0][0].url) and 'vector' not in str(requests[0][0].url)
    adapter.close()


def test_explicit_gap_retry_is_bounded_and_clears_only_acknowledged_updates(prepared,tmp_path):
    from index_discussions import retry_gaps
    repo,sources=prepared;client=Client(sources);inventory=BackfillInventory(repo,tmp_path/'inventory.sqlite');catalog=Catalog(tmp_path/'catalog.sqlite')
    original=client.update;client.update=lambda *args:False
    assert backfill(inventory,catalog,client,[HOST])['pending_gaps']==2
    client.update=original
    result=retry_gaps(inventory,catalog,client,[HOST],limit=1)
    assert result=={'attempted':1,'resolved':1,'pending':1}
    assert retry_gaps(inventory,catalog,client,[HOST])['pending']==0
    assert retry_gaps(inventory,catalog,client,[HOST])['attempted']==0
    inventory.close();catalog.close()


def test_evidence_preparation_is_remote_free_and_cannot_read_or_write_elasticsearch(prepared,tmp_path):
    repo,sources=prepared;inventory=BackfillInventory(repo,tmp_path/'inventory.sqlite');catalog=Catalog(tmp_path/'catalog.sqlite')
    result=backfill(inventory,catalog,None,[HOST],learn_only=True)
    assert result['prepared'] and not result['complete'] and result['updated']==0
    assert inventory.cursor(HOST,'learn')[1] and not inventory.cursor(HOST,'annotate')[1]
    client=Client(sources)
    assert backfill(inventory,catalog,client,[HOST])['updated']==2
    inventory.close();catalog.close()
