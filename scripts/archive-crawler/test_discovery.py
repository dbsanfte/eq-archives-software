"""Discovery regressions use small disposable archives, never the live checkout."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import tempfile
import shlex
import unittest
from unittest.mock import patch

from common import CrawlError, Page, Store, original_url, site_scope, tier
from crawler import parser
from discovery import Archive, discover
from site_inventory import SiteInventory


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "archive"
        self.repo.mkdir()
        self.git("init", "-q")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.repo), *arguments], stderr=subprocess.DEVNULL).decode().strip()

    def file(self, host, timestamp, path, text):
        destination = self.repo / "websites" / host / timestamp / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)

    def commit(self):
        self.git("add", "websites")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def args(self, maximum=50):
        seeds = self.root / "seeds.json"
        seeds.write_text(json.dumps([{"host": "seed.example", "category": "guild"}]))
        return parser().parse_args(["--work-dir", str(self.store.root), "discover", "--archive-repo", str(self.repo),
                                   "--seeds", str(seeds), "--max-candidates", str(maximum)])

    def test_url_identity_and_capture_unwrapping(self):
        url = "https://EQ.example/Guide%2FOne?b=2&a=1#section"
        self.assertEqual(original_url(url), "https://eq.example/Guide%2FOne?b=2&a=1")
        self.assertEqual(original_url("https://web.archive.org/web/20011231235959id_/" + url), original_url(url))
        self.assertEqual(original_url("../Classes.htm", "http://eq.example/info/index.html"), "http://eq.example/Classes.htm")
        for bad in ("javascript:alert(1)", "http://127.0.0.1/", "http://localhost/", "http://user:password@eq.example/", "http://eq.example/a b"):
            self.assertIsNone(original_url(bad))
        self.assertNotEqual(original_url("http://eq.example/A?a=1&b=2"), original_url("https://eq.example/a?b=2&a=1"))

    def test_sitepowerup_discovery_queues_boards_not_messages_or_post_forms(self):
        from test_sitepowerup import BOARD, MESSAGE
        links = [MESSAGE, MESSAGE.replace('12155','12154'), BOARD, BOARD.replace('102010','104254'), BOARD.replace('Display','Post')]
        self.file('seed.example','20000101000000','index.html',
                  '<title>EverQuest forums</title>' + ''.join('<a href="'+url+'">EQ forum</a>' for url in links))
        self.commit()
        discover(self.args(),self.store)
        rows=self.store.candidates()
        self.assertEqual({row['url'] for row in rows},{BOARD,BOARD.replace('102010','104254')})
        self.assertTrue(all(json.loads(row['coverage'])['scope_mode']=='sitepowerup' for row in rows))

    def test_ezboard_profiles_on_legacy_ports_do_not_become_site_candidates(self):
        self.file('seed.example','20000101000000','index.html',
                  '<title>EverQuest forums</title>'
                  '<a href="http://server2.ezboard.com:8080/ufscnitro.showPublicProfile">Author</a>'
                  '<a href="http://server2.ezboard.com:8080/utanas.showPublicProfile">Author</a>'
                  '<a href="http://pub2.ezboard.com/uother.showPublicProfile">Author</a>')
        self.commit()
        discover(self.args(),self.store)
        self.assertEqual(self.store.candidates(),[])

    def test_freeservers_signup_ad_is_excluded_without_blocking_hosted_sites(self):
        ad='http://www.freeservers.com/cgi-bin/redirect?id=ezboard-r1'
        hosted='http://clerics.freeservers.com/eq/index.html'
        self.file('seed.example','20000101000000','index.html',
                  '<title>EverQuest guild links</title>'+''.join('<a href="'+url+'">EQ links</a>' for url in (ad,hosted)))
        self.commit();discover(self.args(),self.store)
        self.assertEqual({row['url'] for row in self.store.candidates()},{hosted})

    def test_inclusive_tiers_and_shared_host_scope(self):
        for date in ("19990101000000", "20011231235959"):
            self.assertEqual(tier(date), 1)
        for date in ("20020101000000", "20071231235959"):
            self.assertEqual(tier(date), 2)
        for date in ("19981231235959", "20080101000000", "20010230000000", "2001"):
            self.assertIsNone(tier(date))
        self.assertEqual(site_scope("http://www.geocities.com/SouthBeach/Breakers/2938/page.html"), "http://www.geocities.com/SouthBeach/Breakers/2938/")
        self.assertEqual(site_scope("http://www.geocities.com/guild/page.html"), "http://www.geocities.com/guild/")
        self.assertNotEqual(site_scope("http://www.angelfire.com/games/guild1/"), site_scope("http://www.angelfire.com/games/guild2/"))

    def test_page_links_and_text_ignore_scripts(self):
        page = Page("http://seed.example/links.html")
        page.feed('<title>EQ guilds</title><script>ignore me</script><a href="https://eq.example/">Guild<img alt="roster"></a>')
        self.assertEqual(page.links[0]["anchor"].strip(), "Guild roster")
        self.assertNotIn("ignore me", page.text)

    def test_historical_boolean_attributes_do_not_abort_source_parsing(self):
        page = Page("http://seed.example/links.html")
        page.feed('<a href="http://guild.example/"><img alt>EQ guild</a><a href>Empty</a><iframe src>')
        self.assertEqual(page.links[0]["url"], "http://guild.example/")
        self.assertIn("EQ guild", " ".join(page.text))

    def test_cached_discovery_is_bounded_and_does_not_write_archive(self):
        self.file("seed.example", "20000101000000", "links.html", '<p>EverQuest guild links</p>' +
                  ''.join(f'<a href="http://guild{i}.example/">EQ guild {i}</a>' for i in range(8)))
        self.commit()
        head = self.git("rev-parse", "HEAD")
        args = self.args(3)
        discover(args, self.store)
        self.assertEqual(len(self.store.candidates()), 3)
        self.assertEqual(self.store.get("discovery_result")["seed_reads"], 1)
        with patch.object(Archive, "blob", side_effect=AssertionError("cached sources must not be reread")):
            discover(args, self.store)
        self.assertEqual(len(self.store.candidates()), 3)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_coverage_distinguishes_missing_tier_and_legacy_identity(self):
        self.file("known.example", "20000101000000", "Guide.html", "first")
        self.file("known.example", "20030101000000", "later.html", "second")
        self.commit()
        archive = Archive(self.repo, self.store)
        self.assertEqual(archive.coverage("http://known.example/Guide.html")["status"], "present_tier1")
        self.assertEqual(archive.coverage("http://known.example/guide.html")["status"], "absent_page")
        self.assertEqual(archive.coverage("http://known.example/later.html")["status"], "missing_tier1")
        self.assertEqual(archive.coverage("https://known.example/Guide.html")["status"], "uncertain_legacy_url")
        self.assertEqual(archive.coverage("http://unknown.example/")["status"], "absent_host")

    def test_partial_inventory_does_not_claim_page_absent(self):
        for number in range(4):
            self.file("known.example", "20000101000000", f"{number}.html", str(number))
        self.commit()
        archive = Archive(self.repo, self.store, max_entries=2, max_inventory=2)
        self.assertEqual(archive.coverage("http://known.example/3.html")["status"], "inventory_partial")
        self.assertEqual(self.store.db.execute("SELECT SUM(entries) FROM tree_state").fetchone()[0], 2)

    def test_ezboard_discovery_creates_only_top_level_boards_and_retains_deep_provenance(self):
        from ezboard import remember_page
        from common import digest
        from test_ezboard import BOARD, FORUM, html
        body = html(BOARD, FORUM)
        page = Page(BOARD); page.feed(body.decode())
        remember_page(self.store, page, {'url': BOARD, 'timestamp': '20000101000000', 'sha256': digest(body)})
        links = [FORUM + '.showMessage?topicID=1.topic', 'http://pub110.ezboard.com/feqasylumgeneral?page=2',
                 'http://server3.ezboard.com/beqasylum.html', 'http://pub4.ezboard.com/funknownboardgeneral.showMessage?topicID=2.topic']
        self.file('seed.example', '20000101000000', 'links.html', ''.join(f'<a href="{url}">EQ forum</a>' for url in links))
        self.commit()
        discover(self.args(), self.store)
        rows = self.store.candidates()
        self.assertEqual(len(rows), 1)
        self.assertIn('/beqasylum', rows[0]['url'])
        self.assertEqual(json.loads(rows[0]['coverage'])['scope_mode'], 'ezboard')
        self.assertEqual(self.store.get('ezboard_pending_links'), [links[-1]])
        self.assertFalse(any('/f' in row['url'].split('.com')[-1] for row in rows))

    def test_known_site_is_excluded_even_when_root_page_inventory_is_exhausted(self):
        self.file('seed.example','20000101000000','links.html','<a href="http://www.mythiran.com/">EQ research</a><a href="http://new.example/">EQ guild</a>')
        self.file('mythiran.com','19990918084825','research/spells.html','EverQuest research')
        self.commit()
        args=self.args()
        args.max_tree_entries=1
        args.max_inventory_entries=1
        discover(args,self.store)
        self.assertEqual([row['url'] for row in self.store.candidates()],['http://new.example/'])

    def test_shared_host_inventory_checks_accounts_without_merging_other_users(self):
        self.file('geocities.com','20000101000000','alice/eq/index.html','EQ guild')
        self.file('server3.ezboard.com','20000101000000','bthesafehouse/index.html','EQ rogue forum')
        self.commit()
        archive=Archive(self.repo,self.store,max_inventory=0)
        inventory=SiteInventory(archive)
        with patch.object(Archive,'files',side_effect=AssertionError('No recursive page inventory')):
            known=inventory.check('https://www.geocities.com:443/alice/eq/news.html')
            self.assertEqual(known['status'],'already_archived')
            self.assertIn('/alice',known['archive_path'])
            self.assertEqual(inventory.check('http://geocities.com/bob/eq/')['status'],'new_site')
            self.assertEqual(inventory.check('http://server3.ezboard.com/bthesafehouse.html')['status'],'already_archived')
            self.assertEqual(inventory.check('http://server3.ezboard.com/botherguild.html')['status'],'new_site')
        self.assertFalse(self.store.db.execute('SELECT * FROM files').fetchone())

    def test_www_and_default_port_archive_folders_block_duplicate_sites(self):
        folders = []
        for index, suffix in enumerate(('', ':80', ':443', '_80', '_443')):
            for prefix in ('', 'www.'):
                host = f'{prefix}guild{index}-{bool(prefix)}.example{suffix}'.lower()
                self.file(host, '20000101000000', 'deep/guide.html', 'EQ guild')
                folders.append((host, f'guild{index}-{bool(prefix)}.example'.lower()))
        self.commit()
        inventory = SiteInventory(Archive(self.repo, self.store, max_inventory=0))
        with patch.object(Archive, 'files', side_effect=AssertionError('No recursive page inventory')), \
                patch.object(SiteInventory, 'tree', side_effect=AssertionError('Host metadata suffices')):
            for folder, host in folders:
                for scheme, port in (('http', ''), ('http', ':80'), ('https', ''), ('https', ':443')):
                    for prefix in ('', 'www.'):
                        with self.subTest(folder=folder, scheme=scheme, port=port, prefix=prefix):
                            result = inventory.check(f'{scheme}://{prefix}{host}{port}/other/page.html')
                            self.assertEqual(result['status'], 'already_archived')
                            self.assertEqual(result['archive_path'], 'websites/' + folder)

    def test_literal_port_account_checks_keep_accounts_and_nondefault_ports_distinct(self):
        self.file('www.geocities.com:80', '20000101000000', 'alice/eq/index.html', 'EQ guild')
        self.file('custom.example:8080', '20000101000000', 'index.html', 'EQ guild')
        self.file('legacy.example_8080', '20000101000000', 'index.html', 'EQ guild')
        self.commit()
        inventory = SiteInventory(Archive(self.repo, self.store, max_inventory=0))
        self.assertEqual(inventory.check('https://geocities.com/alice/eq/')['status'], 'already_archived')
        self.assertEqual(inventory.check('http://www.geocities.com/bob/')['status'], 'new_site')
        for host in ('custom.example', 'legacy.example'):
            self.assertEqual(inventory.check(f'http://www.{host}/')['status'], 'new_site')
            self.assertEqual(inventory.check(f'http://www.{host}:8080/')['status'], 'already_archived')
            self.assertEqual(inventory.check(f'http://www.{host}:8081/')['status'], 'new_site')
        self.assertEqual(inventory.check('http://geocities.com:8080/alice/')['status'], 'new_site')

    def test_port_alias_fix_rechecks_previously_cached_new_sites(self):
        from common import site_identity
        self.file('www.guild.example:80', '20000101000000', 'index.html', 'EQ guild')
        self.commit()
        archive = Archive(self.repo, self.store)
        url = 'https://guild.example/'
        old_cache = 'site_inventory:' + repr((1, archive.sha, site_identity(url)))
        self.store.set(old_cache, {'status': 'new_site', 'complete': True})
        result = SiteInventory(archive).check(url)
        self.assertEqual(result['status'], 'already_archived')
        self.assertEqual(result['archive_path'], 'websites/www.guild.example:80')

    def test_missing_shared_metadata_or_budget_is_unverified_and_never_fetches(self):
        self.file('geocities.com','20000101000000','alice/index.html','EQ guild')
        self.commit()
        archive=Archive(self.repo,self.store)
        bounded=SiteInventory(archive,max_bytes=1)
        result=bounded.check('http://geocities.com/alice/')
        self.assertEqual(result['reason'],'tree_too_large')
        self.assertFalse(result['retryable'])
        self.assertEqual(bounded.bytes,0)
        result=SiteInventory(archive,max_trees=0).check('http://geocities.com/alice/',force=True)
        self.assertEqual(result['reason'],'tree_limit')
        self.assertTrue(result['retryable'])
        timestamp=self.git('rev-parse','HEAD:websites/geocities.com/20000101000000')
        (self.repo/'.git/objects'/timestamp[:2]/timestamp[2:]).unlink()
        missing=SiteInventory(archive).check('http://geocities.com/alice/',force=True)
        self.assertEqual(missing['status'],'inventory_partial')
        self.assertEqual(missing['reason'],'metadata_unavailable')
        self.assertFalse(missing['retryable'])

    def test_large_shared_host_tree_uses_the_existing_total_byte_budget(self):
        # Model a large host with Git objects, without creating thousands of files.
        self.file('pub6.ezboard.com','19990101000000','bthemagicianstower.html','EQ forum')
        self.commit()
        capture_tree=self.git('rev-parse','HEAD:websites/pub6.ezboard.com/19990101000000')
        def tree(entries):
            return subprocess.check_output(['git','-C',str(self.repo),'mktree'],input=entries.encode()).decode().strip()
        dates=[(datetime(1999,1,1)+timedelta(seconds=index)).strftime('%Y%m%d%H%M%S') for index in range(52000)]
        host=tree(''.join(f'040000 tree {capture_tree}\t{stamp}\n' for stamp in dates))
        self.assertGreater(int(self.git('cat-file','-s',host)),2*1024*1024)
        websites=tree(f'040000 tree {host}\tpub6.ezboard.com\n')
        root=tree(f'040000 tree {websites}\twebsites\n')
        commit=self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit-tree',root,'-m','large metadata fixture')
        self.git('update-ref','HEAD',commit)
        archive=Archive(self.repo,self.store)
        inventory=SiteInventory(archive,max_trees=3)
        with patch.object(Archive,'files',side_effect=AssertionError('No page inventory or checkout walk')):
            result=inventory.check('http://pub6.ezboard.com/bthemagicianstower.html',timestamps=[dates[-1]])
        self.assertEqual(result['status'],'already_archived')
        self.assertTrue(result['complete'])
        self.assertIn(dates[-1],result['archive_path'])
        self.assertLessEqual(inventory.probes,3)
        self.assertLessEqual(inventory.bytes,32*1024*1024)
        self.assertFalse(self.store.db.execute('SELECT * FROM files').fetchone())

    def test_staged_site_aliases_do_not_use_multiple_candidate_slots(self):
        self.file('seed.example','20000101000000','links.html','<a href="http://new.example/">EQ guild</a><a href="https://www.new.example:443/eq/">EQ guild</a>')
        self.commit()
        discover(self.args(),self.store)
        self.assertEqual(len(self.store.candidates()),1)

    def test_shared_metadata_recheck_resumes_under_original_per_check_bounds(self):
        for year in range(1999,2005):
            self.file('geocities.com',f'{year}0101000000',f'other-{year}/index.html','EQ')
        self.commit()
        archive=Archive(self.repo,self.store)
        result=SiteInventory(archive,max_trees=3).check('http://geocities.com/new-account/')
        self.assertEqual(result['status'],'inventory_partial')
        self.assertEqual(result['progress']['checked'],2)
        self.assertEqual(result['progress']['total'],6)
        previous=result['progress']['checked']
        for _ in range(6):
            inventory=SiteInventory(archive,max_trees=3)
            result=inventory.check('http://geocities.com/new-account/',force=True)
            self.assertLessEqual(inventory.probes,3)
            self.assertGreater(result['progress']['checked'],previous)
            previous=result['progress']['checked']
            if result['status']=='new_site':break
        self.assertEqual(result['status'],'new_site')
        self.assertEqual(result['progress']['checked'],6)

    def test_state_cannot_live_in_software_or_archive(self):
        with self.assertRaises(CrawlError):
            Store(Path(__file__).parent / "test-state")
        self.file("known.example", "20000101000000", "index.html", "EQ")
        self.commit()
        nested = Store(self.repo / "state")
        self.addCleanup(nested.close)
        with self.assertRaises(CrawlError):
            Archive(self.repo, nested)

    def test_missing_partial_clone_blob_never_invokes_remote_or_writes_objects(self):
        self.file("known.example", "20000101000000", "index.html", "EQ guild history")
        self.commit()
        blob = self.git("rev-parse", "HEAD:websites/known.example/20000101000000/index.html")
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        self.git("config", "remote.origin.url", "ssh://fixture.invalid/archive")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialclonefilter", "blob:none")
        marker = self.root / "fetch-attempt"
        ssh = self.root / "fake-ssh.py"
        ssh.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('attempted')\nraise SystemExit(1)\n")
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "python3 " + shlex.quote(str(ssh)), "GIT_NO_LAZY_FETCH": "0"}):
            archive = Archive(self.repo, self.store)
            with self.assertRaises(CrawlError):
                archive.blob(blob, 1024)
        self.assertFalse(marker.exists(), "Discovery must never try a promisor fetch")

    def test_missing_seed_can_use_next_available_capture_within_probe_cap(self):
        self.file("seed.example", "19990101000000", "links.html", "missing first")
        self.file("seed.example", "20000101000000", "links.html", '<a href="http://guild.example/">EQ guild</a>')
        self.commit()
        blob = self.git("rev-parse", "HEAD:websites/seed.example/19990101000000/links.html")
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        args = self.args()
        args.max_per_seed = 1
        args.max_seed_probes = 2
        discover(args, self.store)
        self.assertEqual(len(self.store.candidates()), 1)
        self.assertEqual(self.store.get("discovery_result")["seed_probes"], 2)


if __name__ == "__main__":
    unittest.main()
