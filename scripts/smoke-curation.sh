#!/usr/bin/env bash
set -euo pipefail
image=${1:?Usage: smoke-curation.sh CURATION_IMAGE}
: "${BROWSER_TEST_PYTHON:?Set BROWSER_TEST_PYTHON}"
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
curation_suffix="$$"
curation_network="curation-test-$curation_suffix"
curation_container="curation-test-$curation_suffix"
curation_runtime=$(mktemp -d)
cleanup() {
  docker rm -f "$curation_container" >/dev/null 2>&1 || true
  docker network rm "$curation_network" >/dev/null 2>&1 || true
  rm -rf "$curation_runtime"
}
trap cleanup EXIT
# Match the production seeder's restricted capabilities, including restarts
# with service-owned state. Unit tests alone cannot check Linux capabilities.
docker run --rm -i --network none --user 0:10001 --entrypoint python \
  --read-only --tmpfs /data:rw,mode=700 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --security-opt no-new-privileges \
  "$image" - <<'PY'
import os
from pathlib import Path
from seed import seed
assert os.getuid()==0 and os.getgid()==10001
caps=int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:')),16)
assert not caps & (1<<3), 'The seed regression must run without FOWNER'
for name,mode in [('fresh',None),('interrupted',0o700),('restart',0o2770)]:
    root=Path('/data')/name;root.mkdir(mode=0o700)
    (root/'seed-complete').touch();(root/'preserved').write_bytes(b'existing private state')
    os.chown(root,10001,10001)
    indexing=root/'indexing-state'
    if mode is not None:
        indexing.mkdir();indexing.chmod(mode);os.chown(indexing,10001,10001)
    for _ in range(2):
        seed('/absent-pilot',root,10001)
        info=indexing.stat()
        assert info.st_uid==info.st_gid==10001 and info.st_mode&0o7777==0o2770
        assert (root/'preserved').read_bytes()==b'existing private state'
        assert not (root/'crawl.sqlite3').exists()
        assert root.stat().st_uid==10001 and root.stat().st_mode&0o777==0o700
print('Production seed capabilities, first start, interrupted ownership and repeated restart verified')
PY
# Exercise real pinned-tokenizer startup within the import Job's actual budget.
docker run --rm --network none --read-only --memory=2g --cpus=1 -i --entrypoint python "$image" - <<'PYTHON'
import json,resource
from jobs import import_job
import index_captures
from indexer import passages
container=import_job({'id':'a'*32,'manifest_sha256':'b'*64},'dbsanfte/frontend@sha256:'+'1'*64)['spec']['template']['spec']['containers'][0]
assert container['resources']['limits']['memory']=='2Gi'
plan=passages.prepare('A guild expedition defeated Nagafen and explored Lower Guk. '*20,
    'websites/guild.example/20000101000000/index.html','Guild history',
    html_source='<p>A guild expedition defeated Nagafen and explored Lower Guk.</p>',mime_type='text/html')
assert plan.passages
print(json.dumps({'import_tokenizer_startup':'passed','peak_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)}))
PYTHON
# Exercise real spawn/pipes and the pinned tokenizer under the maintenance
# Job's UID, read-only filesystem, CPU/memory limits and dropped capabilities.
docker run --rm --network none --read-only --user 10001:10001 --memory=3g --cpus=2 \
  --tmpfs /tmp:rw,uid=10001,gid=10001,mode=700 --cap-drop ALL --security-opt no-new-privileges \
  -i --entrypoint python "$image" - <<'PYTHON'
from pathlib import Path
import subprocess
probe=Path('/tmp/policy-runtime.py')
probe.write_text('''
import hashlib,multiprocessing,os
from pathlib import Path
from indexer import passages,gemma
from indexer.html_extraction import WEBSITE_EXTRACTION_VERSION
from policy_maintenance import State,identity,run
from policy_pool import PreparationPool
from policy_sources import Sources
from policy_scheduler import job_for

class Local:
    def __init__(self,hits):self.hits=hits;self.writes=0
    def get(self,identifier):return self.hits[identifier]
    def select(self,cursor):return [identifier for identifier in sorted(self.hits) if identifier>cursor]
    def update(self,hit,patch):
        assert set(patch)<=set(['text_chunking_version','text_passage_provenance'])
        self.writes+=1
    def vectors(self,*args):raise AssertionError('Unchanged bound requests must reuse vectors')

def main():
    assert os.getuid()==10001
    name='eqarchives-policy-'+identity()+'-1'
    job=job_for(name,'dbsanfte/frontend@sha256:'+'1'*64)
    container=job['spec']['template']['spec']['containers'][0]
    assert container['resources']['limits']=={'cpu':'2','memory':'3Gi'}
    root=Path('/tmp/archive');hits={}
    for leaf in ('a.html','b.html'):
        identifier='websites/guild.example/20000101000000/'+leaf
        raw='<html><title>Guild history</title><p>Our guild defeated Nagafen.</p></html>'
        reader='Complete archival reader.'
        plan=passages.prepare(reader,identifier,'Guild history',html_source=raw,mime_type='text/html')
        source=dict(id=identifier,file_type='text',mime_type='text/html',title='Guild history',text_full=reader,
            embedding_model=gemma.MODEL,text_extraction_version=WEBSITE_EXTRACTION_VERSION,
            text_chunking_version='eq-passages-480-v10',archive_source_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            text_passage_provenance={'version':'eq-passages-480-v10','sources':[{**plan.provenance(),'version':'eq-passages-480-v10'}]},
            text=[dict(text_chunk=p.text,title=p.title,context=p.context,kind=p.kind,source_start=p.start,source_end=p.end) for p in plan.passages])
        path=root/identifier;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(raw)
        hits[identifier]={'_id':identifier,'_source':source}
    service=Local(hits);state=State('/tmp/state');sources=Sources([root]);pool=PreparationPool([root],None,2)
    try:
        result=run('/tmp',state,service,sources,name,maximum=2,should_yield=lambda:False,pool=pool)
        assert result['scanned']==2 and result['failed']==0 and service.writes==2
        assert len(pool.slots)==2 and state.snapshot()['cursor']==max(hits)
    finally:pool.close();sources.close();state.close()
    assert not multiprocessing.active_children()
    print('Non-root policy spawn, source proof, cursor writes and process cleanup verified')

if __name__=='__main__':main()
''')
subprocess.run(['python',str(probe)],check=True,timeout=60)
PYTHON
mkdir "$curation_runtime/credentials" "$curation_runtime/kubernetes"
printf '%s\n' dummy-paid-key > "$curation_runtime/credentials/luna_api_key"
printf '%s\n' dummy-service-token > "$curation_runtime/kubernetes/token"
printf '%s\n' eqarchives-es > "$curation_runtime/kubernetes/namespace"
printf '%s\n' dummy-ca > "$curation_runtime/kubernetes/ca.crt"
chmod 755 "$curation_runtime/credentials" "$curation_runtime/kubernetes"
chmod 444 "$curation_runtime/credentials/luna_api_key" "$curation_runtime/kubernetes/"*
docker network create --subnet 192.168.240.0/24 "$curation_network" >/dev/null
docker run -d --name "$curation_container" --network "$curation_network" --ip 192.168.240.2 \
  --read-only --tmpfs /tmp:rw,uid=10001,gid=10001,mode=700 --tmpfs /data:rw,uid=10001,gid=10001,mode=700 \
  --mount "type=bind,source=$curation_runtime/credentials,target=/run/secrets,readonly" \
  --mount "type=bind,source=$curation_runtime/kubernetes,target=/run/kubernetes,readonly" \
  --cap-drop ALL --security-opt no-new-privileges \
  -e LAN_BIND_IP=192.168.240.2 -e KUBERNETES_CREDENTIALS_DIR=/run/kubernetes \
  "$image" python /app/curation/browser_fixture.py >/dev/null
bash "$repo_dir/scripts/wait-http.sh" http://192.168.240.2:8090/healthz
# Exercise the production UID's real SSH client without network or credentials.
docker exec -i "$curation_container" python - <<'PY'
import os
import pwd
import subprocess
assert os.getuid()==10001
assert pwd.getpwuid(os.getuid()).pw_name=='curation'
result=subprocess.run(['ssh','-G','git@github.com'],capture_output=True)
assert result.returncode==0,'The non-root publication SSH client cannot start'
print('Non-root publication account and offline SSH configuration verified')
PY
"$BROWSER_TEST_PYTHON" "$repo_dir/scripts/test-curation-browser.py" http://192.168.240.2:8090
# Real TCP peer checks against the built service, including spoofed headers.
docker exec -i "$curation_container" python - <<'PY'
import http.client
import os
from pathlib import Path
assert Path('/run/secrets/luna_api_key').read_text().strip()=='dummy-paid-key'
assert Path(os.environ['KUBERNETES_CREDENTIALS_DIR'],'token').read_text().strip()=='dummy-service-token'
for path in ('/','/api/queue','/assets/review.js','/healthz'):
    connection=http.client.HTTPConnection('192.168.240.2',8090,source_address=('127.0.0.1',0),timeout=5)
    connection.request('GET',path,headers={'X-Forwarded-For':'192.168.50.20','X-Real-IP':'192.168.50.20'})
    response=connection.getresponse()
    assert response.status==403,(path,response.status)
    assert b'Intranet access only' in response.read()
    connection.close()
print('Non-LAN TCP peers and forwarded-header spoofs rejected on UI/API/assets/health')
PY
