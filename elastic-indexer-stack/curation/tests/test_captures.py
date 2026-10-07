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
            if job['op']=='list':
                return {'captures':[{'url':job['url'],'timestamp':'20000201000000','digest':'cdx','length':'100'}]}
            raw=b'<p>EverQuest guild history.</p><a href="../unrelated.html">Outside</a><a href="http://other.example/">External</a>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self): events.append(('close',))
    manifest=capture_sites(root,'a'*32,manifest_for(row)['sites'],FakeDownloader)
    assert sum(event[0]=='open' for event in events) == 1
    assert events[0] == ('open',3,131072)
    assert not any('unrelated' in event[1] or 'other.example' in event[1] for event in events if event[0] in ('list','capture'))
    assert any(capture['url'].endswith('/eq/guide.html') for capture in manifest['captures'])
    assert all(capture['archive_path'].startswith('websites/guild.example/') for capture in manifest['captures'])
    check_manifest(root,manifest)


def test_page_only_root_captures_dont_widen_into_host(candidate):
    root,row=candidate
    site=manifest_for(row)['sites'][0]
    site['scope_mode']='page'
    site['scope']=site['url']
    def forbidden(*args): raise AssertionError('Page-only approved samples need no traversal')
    manifest=capture_sites(root,'a'*32,[site],forbidden)
    assert len(manifest['captures']) == 1


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
            if job['op']=='list':
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
            if job['op']=='list':
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
            if job['op']=='list':
                return {'captures':[{'url':job['url'],'timestamp':'20000201000000','digest':'cdx','length':'100'}]}
            if job['url'].endswith('/eq/'):
                raise CrawlError('Wayback HTTP 404')
            raw=b'<p>EverQuest class guide.</p>'
            Path(job['destination']).write_bytes(raw)
            return {'url':job['url'],'timestamp':job['timestamp'],'sha256':digest(raw),'bytes':len(raw)}
        def close(self):pass
    manifest=capture_sites(root,'a'*32,manifest_for(row)['sites'],Unavailable)
    assert len(manifest['captures'])==2
    assert any('HTTP 404' in note['note'] for note in manifest['notes'])
    capture_sites(root,'a'*32,manifest_for(row)['sites'],Unavailable)
    assert requests.count(('capture','http://guild.example/eq/'))==1
    check_manifest(root,manifest)


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
            assert reports[-1]['phase']==('checking_wayback' if job['op']=='list' else 'downloading')
            if job['op']=='list':
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
