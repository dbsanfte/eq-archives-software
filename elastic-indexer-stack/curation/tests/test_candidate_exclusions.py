import pytest

from conftest import add_candidate
from review import apply_decisions, record
from server import create_app
from state import connect, enqueue
from test_server import call

AD='http://www.freeservers.com/cgi-bin/redirect?id=ezboard-r1'


@pytest.mark.parametrize('url,excluded', [
    ('http://ad.example.com/banner.gif', True),
    ('https://ADS.example.com:443/eq/', True),
    ('http://ad.example.com:8080/', True),
    ('https://web.archive.org/web/20000101000000/http://ads.example.com/', True),
    ('http://adventure.example.com/', False),
    ('http://adds.example.com/', False),
    ('http://www.example.com/ads/banner.gif?next=http://ad.example.com/', False),
    ('http://www.ads.example.com/', False),
])
def test_ad_host_filter_matches_only_the_requested_hostname_prefix(url, excluded):
    from common import candidate_exclusion
    assert bool(candidate_exclusion(url)) == excluded


@pytest.mark.parametrize('url', ['http://ad.example.com/', 'https://ads.example.com:443/'])
def test_ad_sites_cannot_start_manual_paid_checks(tmp_path, url):
    root = tmp_path/'state'
    row = add_candidate(root, url=url, grade=None)
    app = create_app(root, start_worker=False)
    for path, payload in (('/api/submit-site', {'url': url, 'max_usd': 2}),
                          ('/api/check-candidate', {'id': row['id'], 'manifest_sha256': row['manifest_sha256'], 'max_usd': 2})):
        result = call(app, 'POST', path, payload)
        assert result.status_code == 409 and 'Advertising subdomain' in result.json()['error']
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


def test_exclusion_is_limited_to_the_verified_signup_campaign():
    from common import candidate_exclusion
    from manual import site_url
    for url in ('http://clerics.freeservers.com/', AD.replace('ezboard-r1','other'),
                AD+'&target=another-site', AD.replace('.com','.com.example'), 'http://www.freeservers.com/'):
        assert candidate_exclusion(url) is None
        assert site_url(url)==url


def test_signup_ad_submission_and_evidence_checks_never_queue_paid_work(tmp_path):
    root=tmp_path/'state';row=add_candidate(root,url=AD,grade=None)
    app=create_app(root,start_worker=False)
    for path,payload in (
        ('/api/submit-site',{'url':AD,'max_usd':2}),
        ('/api/check-candidate',{'id':row['id'],'manifest_sha256':row['manifest_sha256'],'max_usd':2})):
        result=call(app,'POST',path,payload)
        assert result.status_code==409 and 'hosting signup advertisement' in result.json()['error']
    with connect(root) as store:
        assert not store.db.execute('SELECT 1 FROM operations').fetchone()
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()


def test_existing_ad_moves_to_history_with_explanation_and_keeps_source_and_decisions(tmp_path):
    root=tmp_path/'state'
    pending=add_candidate(root,url=AD,grade=None)
    approved=add_candidate(root,url=AD.replace('http:','https:'))
    active=add_candidate(root,url=AD.replace('www.',''),grade=None)
    hosted=add_candidate(root,url='http://clerics.freeservers.com/')
    with connect(root) as store:
        apply_decisions(store,[{'id':approved['id'],'manifest_sha256':approved['manifest_sha256'],'decision':'approve'}])
        enqueue(store,'candidate_check',{'id':active['id'],'manifest_sha256':active['manifest_sha256'],'max_usd':2})
    app=create_app(root,start_worker=False)
    for _ in range(2):call(app,'GET','/api/queue?filter=all')
    with connect(root) as store:
        rows={row['id']:record(store,row) for row in store.candidates()}
        assert rows[pending['id']]['state']=='rejected'
        assert 'hosting signup advertisement' in rows[pending['id']]['coverage']['candidate_exclusion']
        assert rows[pending['id']]['decision'] is None  # An automatic exclusion is not a human decision.
        assert rows[approved['id']]['state']=='approved_waiting_batch'
        assert rows[active['id']]['state']=='sampled'
        assert rows[hosted['id']]['state']=='approval_pending'
        for original in (pending,approved,active,hosted):
            assert rows[original['id']]['manifest_sha256']==original['manifest_sha256']
            assert rows[original['id']]['captures']==original['captures']
        assert store.db.execute("SELECT COUNT(*) FROM events WHERE action='candidate_excluded'").fetchone()[0]==1
        assert not store.db.execute('SELECT 1 FROM attempts').fetchone()
    result=call(app,'POST','/api/restore',{'id':pending['id'],'manifest_sha256':pending['manifest_sha256']})
    assert result.status_code==409 and 'hosting signup advertisement' in result.json()['error']
