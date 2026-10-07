import asyncio
import json

import httpx
import pytest

from common import digest
from server import create_app
from state import connect
from conftest import add_candidate, manifest_for

ORIGIN = "http://192.168.50.100:8090"


def call(app, method, path, payload=None, peer="192.168.50.20", headers=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 54321)), base_url=ORIGIN) as client:
            defaults = {"Origin": ORIGIN, "X-Curation-Request": "1"}
            defaults.update(headers or {})
            return await client.request(method, path, json=payload, headers=defaults)
    return asyncio.run(run())


@pytest.mark.parametrize("path", ["/", "/api/queue", "/api/source", "/assets/review.js", "/healthz"])
@pytest.mark.parametrize("peer", ["127.0.0.1", "172.16.0.1", "203.0.113.10", "::1"])
def test_every_endpoint_rejects_non_lan_peers_and_forwarded_spoofs(tmp_path, path, peer):
    app = create_app(tmp_path / "state", start_worker=False)
    response = call(app, "GET", path, peer=peer, headers={"X-Forwarded-For": "192.168.50.20", "X-Real-IP": "192.168.50.20"})
    assert response.status_code == 403
    assert response.text == "Intranet access only"


@pytest.mark.parametrize("headers", [{"Origin":"https://evil.example"}, {"Host":"evil.example"},
                                    {"Origin":""}, {"X-Curation-Request":""}, {"Content-Type":"text/plain"}])
def test_actions_require_same_origin_host_and_non_simple_json_request(tmp_path, headers):
    app = create_app(tmp_path / "state", start_worker=False)
    assert call(app, "POST", "/api/discover", {"max_candidates":50,"max_usd":2}, headers=headers).status_code == 403


def test_queue_source_scope_approval_and_capture_survive_reload(candidate):
    root, row = candidate
    app = create_app(root, start_worker=False)
    response = call(app, "GET", "/api/queue")
    assert response.headers["Cache-Control"] == "no-store"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.json()["all_count"] == 1
    source = call(app, "GET", "/api/source?candidate=" + row["id"]).json()
    assert "EverQuest guild history." in source["complete_extracted_text"]
    changed = call(app, "POST", "/api/scope", {"id":row["id"],"manifest_sha256":row["manifest_sha256"],"mode":"site"})
    assert changed.status_code == 200
    old_decision = {"id":row["id"],"manifest_sha256":row["manifest_sha256"],"decision":"approve"}
    assert call(app, "POST", "/api/decisions", [old_decision]).status_code == 409
    current = call(app,"GET","/api/queue").json()["candidates"][0]
    decision = {"id":current["id"],"manifest_sha256":current["manifest_sha256"],"decision":"approve"}
    assert call(app,"POST","/api/decisions",[decision]).status_code == 200
    assert call(create_app(root,start_worker=False),"GET","/api/queue").json()["approved"] == 1
    started = call(app,"POST","/api/capture",{"ids":[row["id"]]})
    assert started.status_code == 202
    listing = call(app,"GET","/api/queue?filter=all").json()
    assert listing["candidates"][0]["state"] == "capturing"
    assert listing["operations"][0]["kind"] == "capture"
    assert call(app,"POST","/api/decisions",[decision]).status_code == 409
    assert call(app,"POST","/api/capture",{"ids":[row["id"]]}).status_code == 409


def test_approved_filter_shows_only_sites_ready_to_start_capture(candidate):
    root,row=candidate
    app=create_app(root,start_worker=False)
    assert call(app,'GET','/api/queue?filter=approved').json()['total']==0
    decision={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}
    assert call(app,'POST','/api/decisions',[decision]).status_code==200
    current=call(app,'GET','/api/queue?filter=approved').json()
    assert current['total']==1 and current['candidates'][0]['id']==row['id']
    assert current['operations']==[] and current['batches']==[]
    assert call(app,'POST','/api/capture',{'ids':[row['id']]}).status_code==202
    assert call(app,'GET','/api/queue?filter=approved').json()['total']==0


def test_tampered_source_and_ungraded_candidate_cannot_be_approved(candidate):
    root,row = candidate
    app = create_app(root,start_worker=False)
    decision = {"id":row["id"],"manifest_sha256":row["manifest_sha256"],"decision":"approve"}
    (root/row["captures"][0]["path"]).write_bytes(b"changed")
    assert call(app,"POST","/api/decisions",[decision]).status_code == 409
    other = add_candidate(root,url="http://other.example/",grade=None)
    decision = {"id":other["id"],"manifest_sha256":other["manifest_sha256"],"decision":"approve"}
    assert call(app,"POST","/api/decisions",[decision]).status_code == 409


def test_custom_folder_survives_reload_invalidates_approval_and_can_exclude_sample(candidate):
    root,row=candidate
    app=create_app(root,start_worker=False)
    decision={'id':row['id'],'manifest_sha256':row['manifest_sha256'],'decision':'approve'}
    assert call(app,'POST','/api/decisions',[decision]).status_code==200
    changed=call(app,'POST','/api/scope',{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'mode':'custom','path':'/research'})
    assert changed.status_code==200
    current=call(create_app(root,start_worker=False),'GET','/api/queue').json()['candidates'][0]
    assert current['scope']=='http://guild.example/research/' and current['scope_mode']=='custom'
    assert current['state']=='approval_pending' and current['decision'] is None
    assert call(app,'POST','/api/decisions',[decision]).status_code==409
    decision['manifest_sha256']=current['manifest_sha256']
    assert call(app,'POST','/api/decisions',[decision]).status_code==200
    assert call(app,'POST','/api/capture',{'ids':[row['id']]}).status_code==202
    with connect(root) as store:
        operation=json.loads(store.db.execute('SELECT payload FROM operations').fetchone()[0])
        assert operation['sites'][0]['scope']==current['scope']
        assert operation['sites'][0]['captures']==[]


@pytest.mark.parametrize('path',['../other','//other.example/','/eq/../outside/','/eq/%2e%2e/','/eq/%2fother/','/eq/%00/','/eq/%ff/','/eq/?x=1','/eq/#section','/eq/\\outside/'])
def test_custom_scope_rejects_unsafe_or_non_folder_paths(candidate,path):
    root,row=candidate
    app=create_app(root,start_worker=False)
    assert call(app,'POST','/api/scope',{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'mode':'custom','path':path}).status_code==409


def test_custom_scope_cannot_escape_shared_host_account(tmp_path):
    root=tmp_path/'state'
    row=add_candidate(root,url='http://geocities.com/alice/eq/news.html')
    app=create_app(root,start_worker=False)
    for path in ('/','/bob/eq/'):
        assert call(app,'POST','/api/scope',{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'mode':'custom','path':path}).status_code==409
    assert call(app,'POST','/api/scope',{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'mode':'custom','path':'/alice/guides/'}).status_code==200


def test_final_publication_requires_unchanged_batch_and_second_explicit_approval(candidate):
    root,row = candidate
    manifest = manifest_for(row)
    expected = digest(manifest)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,'now','now')",
                         (manifest["batch_id"],json.dumps(manifest),expected))
        store.db.commit()
    app = create_app(root,start_worker=False)
    source = call(app,"GET",f"/api/source?batch={manifest['batch_id']}&slot=0")
    assert "EverQuest" in source.json()["complete_extracted_text"]
    assert call(app,"POST","/api/publish",{"id":manifest["batch_id"],"manifest_sha256":"stale"}).status_code == 409
    assert call(app,"POST","/api/publish",{"id":manifest["batch_id"],"manifest_sha256":expected}).status_code == 202
    assert call(app,"POST","/api/publish",{"id":manifest["batch_id"],"manifest_sha256":expected}).status_code == 409
    with connect(root) as store:
        assert store.db.execute("SELECT state FROM batches").fetchone()[0] == "publication_requested"
        assert store.db.execute("SELECT kind FROM operations").fetchone()[0] == "publish"


def test_publication_can_include_only_reviewed_files_and_binds_selection_to_a_new_hash(candidate):
    root,row=candidate
    manifest=manifest_for(row)
    second={**manifest['captures'][0],'timestamp':'20010101000000','tier':1}
    from captures import archive_path
    second['archive_path']=archive_path(second)
    manifest['captures'].append(second)
    expected=digest(manifest)
    with connect(root) as store:
        store.db.execute("INSERT INTO batches VALUES (?,'awaiting_review',?,?,NULL,NULL,NULL,'now','now')",(manifest['batch_id'],json.dumps(manifest),expected))
        store.db.commit()
    app=create_app(root,start_worker=False)
    for slots in ([],[99],[True],[0,0]):
        assert call(app,'POST','/api/publish',{'id':manifest['batch_id'],'manifest_sha256':expected,'slots':slots}).status_code==409
    assert call(app,'POST','/api/publish',{'id':manifest['batch_id'],'manifest_sha256':expected,'slots':[1]}).status_code==202
    with connect(root) as store:
        row=store.db.execute('SELECT manifest,manifest_sha256 FROM batches').fetchone()
        approved=json.loads(row[0])
        assert len(approved['captures'])==1 and approved['captures'][0]['timestamp']=='20010101000000'
        assert approved['reviewed_manifest_sha256']==expected
        assert row[1]==digest(approved) and row[1]!=expected


@pytest.mark.parametrize("payload", [{"max_candidates":51,"max_usd":2}, {"max_candidates":50,"max_usd":3},
                                    {"max_candidates":True,"max_usd":2}, {"max_candidates":0,"max_usd":2},
                                    {"max_candidates":50,"max_usd":0}, {}, []])
def test_discovery_spend_and_count_must_be_explicit_and_bounded(tmp_path, payload):
    app = create_app(tmp_path/"state",start_worker=False)
    assert call(app,"POST","/api/discover",payload).status_code == 409


def test_paid_discovery_only_enqueues_one_explicit_operation_and_resume_retains_bounds(tmp_path):
    root=tmp_path/"state"
    app=create_app(root,start_worker=False)
    response=call(app,"POST","/api/discover",{"max_candidates":50,"max_usd":2})
    assert response.status_code == 202
    identifier=response.json()["operation"]
    assert call(app,"POST","/api/discover",{"max_candidates":50,"max_usd":2}).status_code == 409
    with connect(root) as store:
        store.db.execute("UPDATE operations SET state='interrupted'")
        store.db.commit()
    assert call(app,"POST","/api/resume",{"id":identifier}).status_code == 202
    with connect(root) as store:
        op=store.db.execute("SELECT payload,state FROM operations").fetchone()
        assert json.loads(op[0]) == {"max_candidates":50,"max_usd":2}
        assert op[1] == "queued"


def test_queue_filters_pagination_bad_actions_and_assets(candidate):
    root,row=candidate
    add_candidate(root,url="http://low.example/",grade=1)
    app=create_app(root,start_worker=False)
    assert call(app,"GET","/api/queue").json()["total"] == 1
    assert call(app,"GET","/api/queue?filter=pending").json()["total"] == 2
    assert call(app,"GET","/api/queue?filter=all&offset=50").json()["candidates"] == []
    for path in ("/api/queue?offset=-1", "/api/queue?offset=bad", "/api/queue?filter=bogus", "/api/source?slot=-1", "/api/source?slot=x", "/api/source?candidate=unknown", "/api/source?candidate="+row["id"]+"&slot=8"):
        assert call(app,"GET",path).status_code == 409
    for path in ("/assets/review.js", "/assets/review.css", "/", "/healthz"):
        assert call(app,"GET",path).status_code == 200
    assert call(app,"GET","/assets/secret").status_code == 404
    for path in ("/api/capture", "/api/scope", "/api/publish", "/api/resume", "/api/decisions"):
        assert call(app,"POST",path,{}).status_code == 409
