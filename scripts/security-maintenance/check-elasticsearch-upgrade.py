#!/usr/bin/env python3
"""Exercise an Elasticsearch minor upgrade on a disposable Docker volume.

No production endpoint or credentials are accepted. A mapping-only JSON export
can be supplied; document contents and vectors are synthetic. Docker resources
are unique to this invocation and removed on exit.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def check(mapping_file=None):
    root=Path(__file__).parent
    pins=dict(line.split('=',1) for line in (root/'runtime.env').read_text().splitlines() if line and not line.startswith('#'))
    old='docker.elastic.co/elasticsearch/elasticsearch:8.17.0@sha256:2f602552550869fb29b6fd5848c5118d3ef3a2e1d5d45802e3ab9088cb2de8e2'
    volume='eqarchives-security-upgrade-'+uuid.uuid4().hex[:12]
    container=None
    def docker(*args):return subprocess.check_output(['docker',*args],text=True).strip()
    def request(base,path,body=None,method=None):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
            method=method,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
    try:
        docker('volume','create',volume)
        vector=[1/math.sqrt(768)]*768
        expected={'id':'isolated-upgrade-probe','title':'Compatibility probe','text_full':'A synthetic EverQuest source.',
            'embedding_model':'embeddinggemma-2-768-v1','text':[{'text_chunk':'A synthetic EverQuest source.','vector':vector}]}
        for image in (old,pins['ELASTICSEARCH_IMAGE']):
            container=docker('run','-d','--publish','127.0.0.1::9200','--memory','1536m','--cpus','2',
                '--cap-drop','ALL','--security-opt','no-new-privileges','-e','discovery.type=single-node',
                '-e','xpack.security.enabled=false','-e','ES_JAVA_OPTS=-Xms512m -Xmx512m',
                '--mount','type=volume,source='+volume+',target=/usr/share/elasticsearch/data',image)
            base='http://'+docker('port',container,'9200/tcp')
            deadline=time.monotonic()+120
            while True:
                try:
                    version=request(base,'/')['version']['number']
                    health=request(base,'/_cluster/health?wait_for_status=yellow&timeout=1s')
                    if health.get('timed_out'):raise OSError('Shards still recovering')
                    break
                except (OSError,ValueError):
                    if time.monotonic()>=deadline:raise RuntimeError('Disposable Elasticsearch failed readiness')
                    time.sleep(.5)
            if version=='8.17.0':
                if mapping_file:
                    mapping=json.loads(Path(mapping_file).read_text())
                    mapping=next(iter(mapping.values()))['mappings']
                else:
                    mapping={'properties':{'embedding_model':{'type':'keyword'},'text':{'type':'nested','properties':{
                        'text_chunk':{'type':'text'},'vector':{'type':'dense_vector','dims':768,'index':True,'similarity':'cosine'}}}}}
                request(base,'/upgrade-probe',{'settings':{'number_of_shards':1,'number_of_replicas':0},'mappings':mapping},'PUT')
                request(base,'/upgrade-probe/_doc/isolated-upgrade-probe?refresh=true',expected,'PUT')
            else:
                assert version=='8.19.24','Unexpected target version'
            assert request(base,'/upgrade-probe/_count')['count']==1
            assert request(base,'/upgrade-probe/_doc/isolated-upgrade-probe')['_source']==expected
            result=request(base,'/upgrade-probe/_search',{'size':1,'_source':['id','text_full'],'knn':{
                'field':'text.vector','query_vector':vector,'k':1,'num_candidates':10,
                'filter':{'term':{'embedding_model':'embeddinggemma-2-768-v1'}}}})
            assert result['hits']['hits'][0]['_id']=='isolated-upgrade-probe'
            print(version+': preserved exact text/checkpoint identity and 768-dimensional nested kNN',flush=True)
            docker('stop','--time','30',container);docker('rm',container);container=None
        print('Disposable upgrade passed; no production data or Jobs were changed.')
    finally:
        if container:subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping-file',type=Path)
    check(parser.parse_args().mapping_file)
