import json

import pytest

from common import CrawlError
from import_status import EnrichmentError, read_error, status_path, write_status


def test_error_report_is_bound_to_exact_job_and_manifest_and_never_trusts_free_text(tmp_path):
    batch={'id':'a'*32,'manifest_sha256':'b'*64}
    job='eqarchives-captures-'+batch['id']+'-r3'
    directory=tmp_path/'enrichment'/batch['id'];directory.mkdir(parents=True)
    write_status(directory,job,batch['manifest_sha256'],'failed',EnrichmentError('correction_limit','source_evidence'))
    assert 'two correction attempts' in read_error(tmp_path,batch,job)
    assert 'verbatim source excerpt' in read_error(tmp_path,batch,job)
    assert read_error(tmp_path,{**batch,'manifest_sha256':'c'*64},job) is None
    assert read_error(tmp_path,batch,job+'-r4') is None
    path=status_path(directory,job)
    value=json.loads(path.read_text());value['message']='Secret-like upstream text'
    path.write_text(json.dumps(value))
    assert 'Secret-like' not in read_error(tmp_path,batch,job)
    for key,replacement in [('job_name','other-job'),('state','running'),('code','upstream-secret'),('cause',[])]:
        path.write_text(json.dumps({**value,key:replacement}))
        assert read_error(tmp_path,batch,job) is None
    for content in ('not-json','[]','x'*4097):
        path.write_text(content)
        assert read_error(tmp_path,batch,job) is None
    with pytest.raises(ValueError):status_path(directory,'../../escape')


@pytest.mark.parametrize('failure',[EnrichmentError('correction_limit','source_evidence'),CrawlError('private upstream text')])
def test_indexer_main_saves_safe_failure_before_exiting(candidate,tmp_path,monkeypatch,capsys,failure):
    from conftest import manifest_for
    import index_captures
    root,row=candidate
    manifest=manifest_for(row)
    batch={'manifest':manifest,'manifest_sha256':'b'*64,'publication':{'commit':'c'*40}}
    job='eqarchives-captures-'+manifest['batch_id']+'-r3'
    enrichment_root=tmp_path/'enrichment'
    monkeypatch.setenv('IMPORT_JOB_NAME',job)
    monkeypatch.setattr('sys.argv',['index_captures','--root',str(root),'--batch','approved.json',
                                    '--manifest-sha256',batch['manifest_sha256'],'--enrichment-root',str(enrichment_root)])
    monkeypatch.setattr(index_captures,'read_batch',lambda *args:batch)
    monkeypatch.setattr(index_captures,'Services',lambda **kwargs:kwargs['enricher'])
    def fail(*args):raise failure
    monkeypatch.setattr(index_captures,'index_batch',fail)
    assert index_captures.main()==1
    value=json.loads(status_path(enrichment_root/manifest['batch_id'],job).read_text())
    assert value['state']=='failed' and value['job_name']==job and value['manifest_sha256']==batch['manifest_sha256']
    output=capsys.readouterr().out
    assert 'private upstream text' not in output
    if isinstance(failure,EnrichmentError):
        assert value['code']=='correction_limit' and value['cause']=='source_evidence'
        assert 'verbatim source excerpt' in output
    else:assert value['code']=='indexing'


def test_indexer_main_records_completion_and_clears_a_previous_pod_failure(candidate,tmp_path,monkeypatch,capsys):
    from conftest import manifest_for
    import index_captures
    root,row=candidate;manifest=manifest_for(row)
    job='eqarchives-captures-'+manifest['batch_id']
    directory=tmp_path/'enrichment'/manifest['batch_id'];directory.mkdir(parents=True)
    write_status(directory,job,'b'*64,'failed',EnrichmentError('request'))
    monkeypatch.setenv('IMPORT_JOB_NAME',job)
    monkeypatch.setattr('sys.argv',['index_captures','--root',str(root),'--batch','approved.json',
                                  '--manifest-sha256','b'*64,'--enrichment-root',str(directory.parent)])
    monkeypatch.setattr(index_captures,'read_batch',lambda *args:{'manifest':manifest})
    monkeypatch.setattr(index_captures,'Services',lambda **kwargs:kwargs['enricher'])
    monkeypatch.setattr(index_captures,'index_batch',lambda *args:{'created':1,'existing':0,'captures':1})
    assert index_captures.main()==0
    value=json.loads(status_path(directory,job).read_text())
    assert value['state']=='completed' and 'code' not in value
    assert json.loads(capsys.readouterr().out)['created']==1
