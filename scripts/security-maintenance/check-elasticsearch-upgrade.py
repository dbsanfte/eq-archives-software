#!/usr/bin/env python3
"""Exercise an Elasticsearch minor upgrade on a disposable Docker volume.

No production endpoint or credentials are accepted. A mapping-only JSON export
can be supplied; document contents and vectors are synthetic. Docker resources
are unique to this invocation and removed on exit.
"""
import argparse
import base64
import json
import math
from pathlib import Path
import subprocess
import ssl
import socket
import tempfile
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
    certificates=tempfile.TemporaryDirectory(prefix='eqarchives-isolated-tls-')
    certs=Path(certificates.name);certs.chmod(0o755)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-keyout',str(certs/'test.key'),'-out',str(certs/'test.crt'),'-subj','/CN=localhost',
        '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (certs/'test.key').chmod(0o644)  # Disposable synthetic key, never a production credential.
    context=ssl.create_default_context(cafile=str(certs/'test.crt'))
    authorization='Basic '+base64.b64encode(b'elastic:isolated-upgrade-password').decode()
    def docker(*args):return subprocess.check_output(['docker',*args],text=True).strip()
    def request(base,path,body=None,method=None):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
            method=method,headers={'Content-Type':'application/json',**({'Authorization':authorization} if base.startswith('https:') else {})})
        with urllib.request.urlopen(req,timeout=10,context=context if base.startswith('https:') else None) as response:return json.load(response)
    try:
        docker('volume','create',volume)
        vector=[1/math.sqrt(768)]*768
        expected={'id':'isolated-upgrade-probe','title':'Compatibility probe','text_full':'A synthetic EverQuest source.',
            'embedding_model':'embeddinggemma-2-768-v1','text':[{'text_chunk':'A synthetic EverQuest source.','vector':vector}]}
        for image in (old,pins['ELASTICSEARCH_IMAGE']):
            tls=image==pins['ELASTICSEARCH_IMAGE']
            options=['-e','xpack.security.enabled='+str(tls).lower()]
            if tls:
                options+=['-e','ELASTIC_PASSWORD=isolated-upgrade-password',
                    '--mount','type=bind,source='+str(certs)+',target=/usr/share/elasticsearch/config/test-certs,readonly']
                for layer in ('http','transport'):
                    for setting,value in {'enabled':'true','key':'test-certs/test.key','certificate':'test-certs/test.crt',
                                          'certificate_authorities':'test-certs/test.crt'}.items():
                        options+=['-e',f'xpack.security.{layer}.ssl.{setting}={value}']
            container=docker('run' ,'-d','--publish','127.0.0.1::9200','--memory','1536m','--cpus','2',
                '--cap-drop','ALL','--security-opt','no-new-privileges','-e','discovery.type=single-node',
                *options,'-e','ES_JAVA_OPTS=-Xms512m -Xmx512m',
                '--mount','type=volume,source='+volume+',target=/usr/share/elasticsearch/data',image)
            base=('https://' if tls else 'http://')+docker('port',container,'9200/tcp')
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
                # Fail closed for an unknown CA and a mismatched hostname.
                try:urllib.request.urlopen(base,context=ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt'),timeout=5)
                except urllib.error.URLError:pass
                else:raise AssertionError('Untrusted TLS connection was accepted')
                with socket.create_connection(('127.0.0.1',int(base.rsplit(':',1)[1])),timeout=5) as connection:
                    try:context.wrap_socket(connection,server_hostname='wrong-database.example')
                    except ssl.SSLCertVerificationError:pass
                    else:raise AssertionError('Mismatched database hostname was accepted')
                try:urllib.request.urlopen(base,context=context,timeout=5)
                except urllib.error.HTTPError as error:assert error.code==401
                else:raise AssertionError('Anonymous database access was accepted')
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
        certificates.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping-file',type=Path)
    check(parser.parse_args().mapping_file)
