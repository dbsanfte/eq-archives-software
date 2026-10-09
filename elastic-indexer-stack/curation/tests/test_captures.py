import json
from pathlib import Path

import pytest

from common import CrawlError, capture_scope, digest, site_scope, within_scope
from captures import archive_path, capture_sites, check_manifest
from conftest import manifest_for


@pytest.mark.parametrize("url,path", [
    ("http://guild.example/","index.html"), ("http://guild.example/eq/","eq/index.html"),
    ("http://guild.example/eq","eq/index.html"), ("http://guild.example/eq/news.html","eq/news.html"),
    ("http://guild.example/forum.php?board=1&start=2","forum.php?board=1&start=2"),
    ("http://guild.example/images/Old%20Sword.html","images/Old Sword.html"),
    ("http://guild.example/news.php?q=old+weapons","news.php?q=old weapons"),
])
def test_archive_paths_match_existing_linux_downloader_convention(url,path):
    assert archive_path({"url":url,"timestamp":"20000101000000"}) == "websites/guild.example/20000101000000/"+path


@pytest.mark.parametrize("url", ["http://guild.example/../outside.html", "http://guild.example/%2e%2e/x.html", "http://guild.example/%00.html", "http://guild.example/%ff.html"])
def test_unsafe_legacy_filenames_are_rejected(url):
    with pytest.raises(CrawlError):
        archive_path({"url":url,"timestamp":"20000101000000"})


def test_shared_account_and_deep_directory_boundaries_are_conservative():
    assert site_scope("http://host.example/~alice/eq/news.html") == "http://host.example/~alice/"
    scope=capture_scope("http://host.example/~alice/eq/news.html")
    assert scope == "http://host.example/~alice/eq/"
    assert within_scope("http://host.example/~alice/eq/guide.html",scope)
    for url in ("http://host.example/~alice/other/", "http://host.example/~alice/eq2/", "http://host.example/~bob/eq/", "https://host.example/~alice/eq/"):
        assert not within_scope(url,scope)
    assert capture_scope("http://guild.example/eq") == "http://guild.example/eq/"
    exact="http://www.sitepowerup.com/mb/view.asp?BoardID=1"
    assert site_scope(exact) == exact
    assert not within_scope(exact.replace('=1','=2'),exact)


def test_traversal_uses_one_downloader_and_does_not_leave_approved_directory(candidate):
    root,row=candidate
    events=[]
    class FakeDownloader:
        def __init__(self,store,args): events.append(('open',args.delay,args.bytes_per_second))
        def call(self,job):
            events.append((job['op'],job['url']))
            if job['op']=='capture_list':
                return {'captures':[{'url':job['url'],'timestamp':'20000201000000','digest':'cdx','length':'100'}]}
            raw=b'<p>EverQuest guild history.</p><a href="../unrelated.html">Outside</a><a href="http://other.example/">External</a>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): events.append(('close',))
    manifest=capture_sites(root,'a'*32,manifest_for(row)['sites'],FakeDownloader)
    assert sum(event[0]=='open' for event in events) == 1
    assert events[0] == ('open',0,0)
    assert not any('unrelated' in event[1] or 'other.example' in event[1] for event in events if event[0] in ('capture_list','capture'))
    assert any(capture['url'].endswith('/eq/guide.html') for capture in manifest['captures'])
    assert all(capture['archive_path'].startswith('websites/guild.example/') for capture in manifest['captures'])
    check_manifest(root,manifest)


def test_page_only_root_captures_dont_widen_into_host(candidate):
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site['scope_mode']='page'
    site['scope']=site['url']
    class EmptyCatalog:
        def __init__(self,*args): pass
        def call(self,job):
            assert job['op']=='capture_list' and job['url']==site['url']
            return {'captures': [], 'resume_key': None}
        def close(self): pass
    manifest=capture_sites(root,'a'*32,[site],EmptyCatalog)
    assert len(manifest['captures']) == 1


def test_redirected_sample_seeds_all_dated_versions_of_its_actual_page(tmp_path):
    from conftest import add_candidate
    root=tmp_path/'state'
    row=add_candidate(root,url='http://www.solusekro.com/',body=b'<p>EverQuest server news.</p>')
    site=manifest_for(row)['sites'][0]
    site.update(scope='http://www.solusekro.com/',scope_mode='directory')
    target=site['url']+'eq/'
    site['captures'][0]['url']=target
    site['captures'][0]['entry_redirect']={'requested_url':site['url'],'url':target}
    calls=[]
    class Downloader:
        def __init__(self,*args):pass
        def call(self,job):
            calls.append(job)
            if job['op']=='capture_list':
                return {'captures':[{'url':target,'timestamp':'20061231235959','digest':'D','length':'40'}] if job['url']==target else []}
            raw=b'<p>EverQuest server news in 2006.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self):pass
    manifest=capture_sites(root,'a'*32,[site],Downloader)
    assert {capture['timestamp'] for capture in manifest['captures']}=={'20000101000000','20061231235959'}
    assert all(capture['url']==target and capture['archive_path'].endswith('/eq/index.html') for capture in manifest['captures'])
    assert any(job['op']=='capture_list' and job['url']==target for job in calls)
    check_manifest(root,manifest)


def test_custom_folder_uses_reviewed_links_without_publishing_outside_source(tmp_path):
    from conftest import add_candidate
    root=tmp_path/'state'
    row=add_candidate(root,body=b'<p>EverQuest guild history.</p><a href="/research/guide.html">Research</a><a href="/news.html">Outside</a>')
    site=manifest_for(row)['sites'][0]
    site.update(scope='http://guild.example/research/',scope_mode='custom',captures=[],reviewed_captures=row['captures'])
    requests=[]
    class Downloader:
        def __init__(self,*args): pass
        def call(self,job):
            requests.append(job['url'])
            if job['op']=='capture_list':
                return {'captures': [] if job['url'].endswith('/') else [{'url':job['url'],'timestamp':'20000101000000','digest':'cdx','length':'100'}]}
            raw=b'<p>EverQuest research guide.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    manifest=capture_sites(root,'a'*32,[site],Downloader)
    assert [capture['url'] for capture in manifest['captures']]==['http://guild.example/research/guide.html']
    assert all('/research/' in url for url in requests)
    check_manifest(root,manifest)


@pytest.mark.parametrize('url', ['http://geocities.com/', 'http://angelfire.com/', 'http://www.sitepowerup.com/mb/'])
def test_unidentified_shared_hosts_cannot_expand_into_unrelated_accounts(url):
    scope=capture_scope(url,'site')
    assert within_scope(url,scope)
    assert not within_scope(url+'unrelated/index.html',scope)
    assert not within_scope(url+'?BoardID=other',scope)


def test_spidered_filename_collisions_are_excluded_without_losing_reviewed_source(candidate):
    root,row=candidate
    class Collision:
        def __init__(self,*args):pass
        def call(self,job):
            if job['op']=='capture_list':
                return {'captures':[{'url':job['url'],'timestamp':'20000101000000','digest':'cdx','length':'100'}]}
            raw=b'<p>EverQuest guild archive</p><a href="index.html">Index alias</a>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self):pass
    manifest=capture_sites(root,'a'*32,manifest_for(row)['sites'],Collision)
    assert row['captures'][0]['sha256'] == manifest['captures'][0]['sha256']
    assert len({c['archive_path'] for c in manifest['captures']})==len(manifest['captures'])
    assert any('collides' in note['note'] for note in manifest['notes'])
    check_manifest(root,manifest)


def test_exhausted_http_budget_leaves_valid_subset_ready_for_review(candidate):
    root,row=candidate
    class Exhausted:
        def __init__(self,*args):pass
        def call(self,job):raise CrawlError('HTTP request budget reached')
        def close(self):pass
    manifest=capture_sites(root,'a'*32,manifest_for(row)['sites'],Exhausted)
    assert len(manifest['captures'])==1
    assert any('budget' in note['note'] for note in manifest['notes'])


def test_missing_replay_is_not_retried_forever_and_retains_other_scoped_captures(candidate):
    root,row=candidate
    requests=[]
    class Unavailable:
        def __init__(self,*args):pass
        def call(self,job):
            requests.append((job['op'],job['url']))
            if job['op']=='capture_list':
                return {'captures':[{'url':job['url'],'timestamp':'20000201000000','digest':'cdx','length':'100'}]}
            if job['url'].endswith('/eq/'):
                raise CrawlError('Wayback HTTP 404')
            raw=b'<p>EverQuest class guide.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self):pass
    manifest=capture_sites(root,'a'*32,manifest_for(row)['sites'],Unavailable)
    assert len(manifest['captures'])==3  # the sampled page also gets its later version
    assert any('HTTP 404' in note['note'] for note in manifest['notes'])
    capture_sites(root,'a'*32,manifest_for(row)['sites'],Unavailable)
    assert requests.count(('capture','http://guild.example/eq/'))==1
    check_manifest(root,manifest)


def test_full_date_capture_resumes_saved_catalog_without_replaying_previous_versions(candidate):
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site.update(scope_mode='page',scope=site['url'])
    calls=[]
    failing=True
    class Interrupted:
        def __init__(self,*args): pass
        def call(self,job):
            calls.append(job)
            if job['op']=='capture_list':
                if job.get('resume_key') and failing:
                    raise CrawlError('Wayback connection failed after bounded retries')
                stamp='20061231235959' if job.get('resume_key') else '20011231235959'
                return {'captures':[{'url':job['url'],'timestamp':stamp,'digest':'D','length':'30'}],
                        'resume_key':None if job.get('resume_key') else 'next'}
            raw=b'<p>EverQuest version.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    with pytest.raises(CrawlError,match='connection failed'):
        capture_sites(root,'a'*32,[site],Interrupted)
    # A pre-progress deployment checkpoint has an offset but no cumulative
    # checked field. Its completed page must survive resume and pagination.
    draft_path=root/'batches'/('a'*32)/'draft.json'
    draft=json.loads(draft_path.read_text())
    for catalog in draft['catalogs'].values(): catalog.pop('checked',None)
    draft_path.write_text(json.dumps(draft))
    failing=False
    reports=[]
    manifest=capture_sites(root,'a'*32,[site],Interrupted,progress=reports.append)
    assert {c['timestamp'] for c in manifest['captures']}=={'20000101000000','20011231235959','20061231235959'}
    assert len([job for job in calls if job['op']=='capture' and job['timestamp']=='20011231235959'])==1
    assert len([job for job in calls if job['op']=='capture_list' and not job.get('resume_key')])==1
    assert reports[0]['versions_found']==1 and reports[0]['versions_pending']==0
    assert reports[-1]['versions_found']==2 and reports[-1]['versions_pending']==0
    assert any(r['versions_found']==2 and r['versions_pending']==1 for r in reports)


def test_full_date_capture_limits_are_visible_and_later_records_remain_checkpointed(candidate,monkeypatch):
    import captures
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site.update(scope_mode='page',scope=site['url'])
    monkeypatch.setitem(captures.LIMITS,'files',2)
    calls=[]
    class Limited:
        def __init__(self,*args): pass
        def call(self,job):
            calls.append(job)
            if job['op']=='capture_list':
                return {'captures':[{'url':job['url'],'timestamp':stamp,'digest':'D','length':'30'}
                                    for stamp in ('20010101000000','20060101000000')], 'resume_key':None}
            raw=b'<p>EverQuest version.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    manifest=capture_sites(root,'a'*32,[site],Limited)
    assert manifest['capture_coverage'][site['id']]['state']=='bounded'
    assert 'incomplete' in manifest['capture_coverage'][site['id']]['reason']
    draft=json.loads((root/'batches'/('a'*32)/'draft.json').read_text())
    catalog=next(iter(draft['catalogs'].values()))
    assert catalog['records'][catalog['offset']]['timestamp']=='20060101000000'
    monkeypatch.setitem(captures.LIMITS,'files',3)
    continued=capture_sites(root,'a'*32,[site],Limited)
    assert continued['capture_coverage'][site['id']]['state']=='complete'
    assert len([job for job in calls if job['op']=='capture_list'])==1


def test_new_capture_excludes_2007_sample_but_legacy_review_remains_valid(candidate):
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site.update(scope_mode='page',scope=site['url'])
    site['captures'][0]['timestamp']='20070101000000'
    legacy=manifest_for(row)
    legacy['captures'][0]['timestamp']='20070101000000'
    legacy['captures'][0]['archive_path']=archive_path(legacy['captures'][0])
    check_manifest(root,legacy)
    legacy['capture_window']={'from':'19990101000000','to':'20061231235959','versions':'all_available'}
    with pytest.raises(CrawlError,match='date window'):check_manifest(root,legacy)
    class Earlier:
        def __init__(self,*args): pass
        def call(self,job):
            if job['op']=='capture_list':
                return {'captures':[{'url':job['url'],'timestamp':'20061231235959','digest':'D','length':'30'}],'resume_key':None}
            raw=b'<p>EverQuest 2006.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    manifest=capture_sites(root,'a'*32,[site],Earlier)
    assert [c['timestamp'] for c in manifest['captures']]==['20061231235959']


@pytest.mark.parametrize('failure', ['Wayback HTTP 404','Response exceeds byte limit','CDX repeated its continuation key'])
def test_failed_catalog_does_not_claim_full_date_coverage(candidate,failure):
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site.update(scope_mode='page',scope=site['url'])
    class BrokenCatalog:
        def __init__(self,*args): pass
        def call(self,job):
            assert job['op']=='capture_list'
            if failure.startswith('CDX repeated'):
                return {'captures': [], 'resume_key': 'repeat'}
            raise CrawlError(failure)
        def close(self): pass
    with pytest.raises(CrawlError,match=failure):
        capture_sites(root,'a'*32,[site],BrokenCatalog)


def test_saved_capture_receipt_resumes_without_repeating_network_or_losing_late_links(candidate,monkeypatch):
    import captures
    root,row=candidate
    sites=manifest_for(row)['sites']
    calls=[]
    original_document=captures.document
    interrupted=False
    def temporary_failure(root,source):
        nonlocal interrupted
        if source['timestamp']=='20060101000000' and not interrupted:
            interrupted=True
            raise CrawlError('Temporary source read failure')
        return original_document(root,source)
    monkeypatch.setattr(captures,'document',temporary_failure)
    class LaterLinks:
        def __init__(self,*args): pass
        def call(self,job):
            calls.append(job)
            if job['op']=='capture_list':
                if job['url'].endswith('/') or job['url'].endswith('/guide.html'):
                    return {'captures': [], 'resume_key': None}
                return {'captures':[{'url':job['url'],'timestamp':'20060101000000','digest':'D','length':'30'}],'resume_key':None}
            raw=b'<p>EverQuest later history</p><a href="later.html">Later page</a>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    with pytest.raises(CrawlError,match='Temporary source'):
        capture_sites(root,'a'*32,sites,LaterLinks)
    result=capture_sites(root,'a'*32,sites,LaterLinks)
    assert len([job for job in calls if job['op']=='capture' and job['url']==sites[0]['url']])==1
    assert any(c['url'].endswith('/later.html') and c['timestamp']=='20060101000000' for c in result['captures'])


def test_manifest_rejects_changed_files_duplicate_destinations_and_wrong_layout(candidate):
    root,row=candidate
    manifest=manifest_for(row)
    check_manifest(root,manifest)
    manifest['captures'].append(manifest['captures'][0])
    with pytest.raises(CrawlError,match='duplicate'):
        check_manifest(root,manifest)
    manifest['captures']=manifest['captures'][:1]
    manifest['captures'][0]['archive_path']='websites/other/filename.html'
    with pytest.raises(CrawlError,match='destination'):
        check_manifest(root,manifest)


def test_capture_reports_real_counts_before_requests_and_after_resume(candidate):
    root,row=candidate
    reports=[]
    class Downloader:
        def __init__(self,*args): pass
        def call(self,job):
            assert reports[-1]['current_url']==job['url']
            assert reports[-1]['phase']==('checking_wayback' if job['op']=='capture_list' else 'downloading')
            if job['op']=='capture_list':
                return {'captures':[{'url':job['url'],'timestamp':'20000201000000','digest':'cdx','length':'100'}]}
            raw=b'<p>EverQuest guide.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    sites=manifest_for(row)['sites']
    manifest=capture_sites(root,'a'*32,sites,Downloader,progress=reports.append)
    assert reports[0]['phase']=='preparing' and reports[0]['files']==1
    assert reports[-1]['phase']=='ready_for_review'
    assert reports[-1]['files']==len(manifest['captures'])
    assert reports[-1]['bytes']==sum(c['bytes'] for c in manifest['captures'])
    assert reports[-1]['urls_checked']==len(manifest['visited'])
    assert reports[-1]['sites_done']==reports[-1]['sites_total']==1
    previous=len(reports)
    resumed=capture_sites(root,'a'*32,sites,Downloader,progress=reports.append)
    assert reports[previous]['files']==len(manifest['captures'])
    assert resumed['captures']==manifest['captures']


def test_approved_page_captures_full_1999_2006_window_with_pagination_and_sample_reuse(candidate):
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site.update(scope_mode='page',scope=site['url'])
    calls=[]
    class Versions:
        def __init__(self,*args): pass
        def call(self,job):
            calls.append(job)
            if job['op'] in ('list','capture_list'):
                stamps=['19990101000000','20000101000000'] if not job.get('resume_key') else ['20061231235959']
                return {'captures':[{'url':job['url'],'timestamp':stamp,'digest':'SAME','length':'100'} for stamp in stamps],
                        'resume_key':'page-two' if not job.get('resume_key') else None}
            raw=b'<p>EverQuest guild history.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): pass
    reports=[]
    manifest=capture_sites(root,'a'*32,[site],Versions,progress=reports.append)
    assert {c['timestamp'] for c in manifest['captures']}=={'19990101000000','20000101000000','20061231235959'}
    assert all(job['from']=='19990101000000' and job['to']=='20061231235959' for job in calls)
    assert len([job for job in calls if job['op']=='capture_list'])==2
    assert not any(job['op']=='capture' and job['timestamp']=='20000101000000' for job in calls)
    assert manifest['capture_window']=={'from':'19990101000000','to':'20061231235959','versions':'all_available'}
    assert manifest['capture_coverage'][site['id']]['state']=='complete'
    assert reports[-1]['versions_found']==3 and reports[-1]['versions_pending']==0
    assert any(r['versions_found']==2 and r['versions_pending']==2 for r in reports)
    assert any(r['versions_found']==3 and r['versions_pending']==1 for r in reports)
    previous=len(calls)
    assert capture_sites(root,'a'*32,[site],Versions)['captures']==manifest['captures']
    assert len(calls)==previous
    check_manifest(root,manifest)
