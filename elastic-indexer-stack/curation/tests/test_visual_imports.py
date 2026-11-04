import io
import json
from unittest.mock import patch

from PIL import Image
import httpx
import pytest

from common import CrawlError, digest
from conftest import manifest_for
from index_captures import MODEL, Services, index_batch
from indexer.image_embeddings import prepare_image, VERSION


def image_batch(candidate, *, corrupt=False, tiny=False):
    root,row=candidate
    stream=io.BytesIO()
    Image.new('RGB',(1,1) if tiny else (64,48),'blue').save(stream,format='PNG')
    raw=b'bad raster' if corrupt else stream.getvalue()
    capture=row['captures'][0]
    (root/capture['path']).write_bytes(raw)
    capture.update(content_type='image/png',bytes=len(raw),sha256=digest(raw))
    manifest=manifest_for(row)
    return root,{'manifest':manifest,'manifest_sha256':digest(manifest),'publication':{'commit':'b'*40}}


class ImageServices:
    def __init__(self):self.documents={};self.calls=0
    def exists(self,key):return key in self.documents
    def enrich(self,*args):raise AssertionError('Direct raster embeddings never call Luna')
    def image_embedding(self,image):self.calls+=1;return [1.]+[0.]*767
    def create(self,key,document):self.documents[key]=document;return True


def test_approved_raster_is_create_only_source_bound_and_never_pays_for_captioning(candidate):
    root,batch=image_batch(candidate);services=ImageServices()
    assert index_batch(root,batch,services)=={'created':1,'existing':0,'captures':1}
    document=next(iter(services.documents.values()))
    assert document['file_type']=='image'
    assert document['embedding_model']==MODEL
    assert document['image_embedding_version']==VERSION
    assert document['image_embedding_status']=='embedded'
    assert document['image_embedding_sha256']==batch['manifest']['captures'][0]['sha256']
    assert len(document['image_vector'])==768
    assert document['archive_commit']=='b'*40
    assert 'llm_summary' not in document
    assert index_batch(root,batch,services)['existing']==1
    assert services.calls==1


@pytest.mark.parametrize('options,reason',[({'corrupt':True},'invalid_or_oversized_raster'),({'tiny':True},'decorative_pixel')])
def test_unusable_raster_remains_visible_as_metadata_without_blocking_the_import(candidate,options,reason):
    root,batch=image_batch(candidate,**options);services=ImageServices()
    assert index_batch(root,batch,services)['created']==1
    document=next(iter(services.documents.values()))
    assert document['image_embedding_status']==reason
    assert 'image_vector' not in document and services.calls==0


def service(tmp_path,handler):
    for name in ('es_username','es_password','openai_api_key'):(tmp_path/name).write_text('fixture-'+name)
    value=Services(tmp_path);value.embedder.close()
    value.embedder=httpx.Client(transport=httpx.MockTransport(handler))
    return value


def test_image_transport_retries_transient_failures_and_reuses_identical_sources(tmp_path):
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(503) if len(seen)==1 else httpx.Response(200,json={
            'model':MODEL,'data':[{'index':0,'embedding':[2.]+[0.]*767}]})
    instance=service(tmp_path,handler);stream=io.BytesIO();Image.new('RGB',(32,32),'blue').save(stream,format='PNG')
    image=prepare_image(stream.getvalue())
    try:
        with patch('index_captures.time.sleep'):
            assert instance.image_embedding(image)==[1.]+[0.]*767
            instance.image_embedding(image)
        assert len(seen)==2
        assert seen[0].url.host=='gemma-image-embeddings.eqarchives-es.svc.cluster.local'
        payload=json.loads(seen[0].content)
        assert payload['input']==[image.content()]
        assert payload['model']==MODEL
    finally:instance.close()


@pytest.mark.parametrize('status,body',[(401,{}),(200,{'model':'wrong','data':[]}),(503,{})])
def test_image_transport_does_not_publish_invalid_vectors_or_uncertain_success(tmp_path,status,body):
    instance=service(tmp_path,lambda _:httpx.Response(status,json=body))
    stream=io.BytesIO();Image.new('RGB',(32,32),'blue').save(stream,format='PNG')
    try:
        with patch('index_captures.time.sleep'),pytest.raises(CrawlError):
            instance.image_embedding(prepare_image(stream.getvalue()))
        assert instance.visual_cache=={}
    finally:instance.close()
