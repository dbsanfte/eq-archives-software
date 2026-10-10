"""Progressive source discovery against disposable archives, with no network."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from archive_frontier import advance, scan
from common import Store
from crawler import parser
from discovery import Archive, discover
from site_inventory import SiteInventory


class FrontierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'archive'
        self.repo.mkdir()
        self.git('init', '-q')
        self.store = Store(self.root / 'state')
        self.addCleanup(self.store.close)
        seeds = self.root / 'seeds.json'
        seeds.write_text(json.dumps([{'host': 'seed.example', 'category': 'guild'}]))
        self.args = parser().parse_args(['--work-dir', str(self.store.root), 'discover',
            '--archive-repo', str(self.repo), '--seeds', str(seeds)])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def page(self, host, path, target, timestamp='20000101000000'):
        destination = self.repo / 'websites' / host / timestamp / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f'<a href="http://{target}/">EverQuest guild</a>')

    def commit(self):
        self.git('add', 'websites')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')

    def scan(self, **kwargs):
        return scan(Archive(self.repo, self.store), self.args, **kwargs)

    def test_advances_beyond_cached_pages_and_capped_inventory(self):
        for index in range(5):
            self.page('seed.example', f'links{index}.html', f'guild{index}.example')
        self.commit()
        self.args.max_per_seed = self.args.max_inventory_entries = self.args.max_tree_entries = 1
        discover(self.args, self.store)  # One cached source; the old inventory is full.
        with patch.object(Archive, 'files', side_effect=AssertionError('No recursive inventory')):
            for _ in range(15):
                result = self.scan()
                if not result['remaining']:
                    break
        discover(self.args, self.store, cached_only=True)
        self.assertEqual(len(self.store.candidates()), 5)
        self.assertEqual(result['hosts_complete'], 1)
        self.assertEqual(result['pages_scanned'], 4)  # Cached page was not reread.
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)

    def test_rotates_to_nonseed_hosts_and_prefers_early_link_pages(self):
        self.page('seed.example', 'links.html', 'first.example')
        self.page('other.example', 'nested/links.html', 'second.example')
        self.page('other.example', 'index.html', 'later.example', '20030101000000')
        self.page('ads.example', 'links.html', 'ad.example')
        self.commit()
        self.args.max_per_seed = 1
        seen = []
        result = self.scan(notify=lambda state: seen.append(state['current_host']), max_hosts=1)
        self.assertEqual(seen[0], 'seed.example')
        self.assertTrue(result['remaining'])
        self.scan(max_hosts=1)
        links = {row[0] for row in self.store.db.execute('SELECT url FROM links')}
        self.assertEqual(links, {'http://first.example/', 'http://second.example/'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM discovery_frontier').fetchone()[0], 2)

    def test_unavailable_objects_advance_and_dont_starve_other_hosts(self):
        for index in range(6):
            self.page('seed.example', f'links{index}.html', f'missing{index}.example')
        self.page('other.example', 'links.html', 'available.example')
        self.commit()
        for index in range(6):
            oid = self.git('rev-parse', f'HEAD:websites/seed.example/20000101000000/links{index}.html')
            (self.repo / '.git/objects' / oid[:2] / oid[2:]).unlink()
        self.args.max_seed_probes = 2
        with patch.object(Archive, 'blob', autospec=True, side_effect=Archive.blob) as read:
            for _ in range(10):
                result = self.scan()
                if not result['remaining']:
                    break
        self.assertEqual(read.call_count, 7)
        self.assertEqual(len({call.args[1] for call in read.call_args_list}), 7)
        self.assertEqual(result['sources_unavailable'], 6)
        self.assertEqual(result['pages_scanned'], 1)
        self.assertFalse(result['remaining'])
        with patch.object(Archive, 'blob', side_effect=AssertionError('Unavailable objects must wait')):
            self.scan()
        self.assertEqual(self.store.db.execute('SELECT url FROM links').fetchone()[0], 'http://available.example/')

    def test_missing_sources_can_recover_after_cooldown_without_rereading_successes(self):
        self.page('seed.example', 'links.html', 'missing.example')
        self.page('seed.example', 'index.html', 'present.example')
        self.commit()
        oid = self.git('rev-parse', 'HEAD:websites/seed.example/20000101000000/links.html')
        path = self.repo / '.git/objects' / oid[:2] / oid[2:]
        packed = path.read_bytes(); path.unlink()
        self.scan()
        retry = self.store.db.execute('SELECT retry_at FROM discovery_frontier').fetchone()[0]
        path.write_bytes(packed)
        self.assertEqual(self.scan()['reads'], 0)
        with patch('archive_frontier.time.time', return_value=retry + 1):
            result = self.scan()
        self.assertEqual(result['reads'], 1)
        self.assertEqual(result['pages_scanned'], 2)
        self.assertEqual(result['sources_unavailable'], 0)

    def test_explicit_local_object_cache_recovers_without_fetching_or_changing_archive(self):
        self.page('seed.example', 'index.html', 'guild.example')
        self.commit()
        oid = self.git('rev-parse', 'HEAD:websites/seed.example/20000101000000/index.html')
        path = self.repo / '.git/objects' / oid[:2] / oid[2:]
        extra = self.root / 'extra-objects'
        cached = extra / oid[:2] / oid[2:]
        cached.parent.mkdir(parents=True)
        cached.write_bytes(path.read_bytes()); path.unlink()
        self.assertEqual(self.scan()['sources_unavailable'], 1)
        with patch.dict(os.environ, {'ARCHIVE_OBJECT_DIRECTORIES': str(extra)}):
            result = self.scan()
        self.assertEqual(result['pages_scanned'], 1)
        self.assertEqual(result['sources_unavailable'], 0)
        self.assertFalse(path.exists(), 'Object reads must not write to the source repository')
        self.assertEqual(self.store.db.execute('SELECT url FROM links').fetchone()[0], 'http://guild.example/')

    def test_missing_tree_does_not_block_other_hosts_or_claim_full_coverage(self):
        self.page('seed.example', 'nested/index.html', 'missing.example')
        self.page('other.example', 'index.html', 'present.example')
        self.commit()
        tree = self.git('rev-parse', 'HEAD:websites/seed.example/20000101000000/nested')
        (self.repo / '.git/objects' / tree[:2] / tree[2:]).unlink()
        result = self.scan()
        self.assertEqual(result['metadata_unavailable'], 1)
        self.assertEqual(result['pages_scanned'], 1)
        self.assertFalse(result['remaining'])
        self.assertEqual(result['hosts_complete'], 1)

    def test_metadata_byte_pause_does_not_poison_the_next_host_or_lose_its_cursor(self):
        for host in ('seed.example', 'other.example', 'third.example'):
            self.page(host, 'index.html', 'guild.' + host)
        self.commit()
        def bounded(archive, **kwargs):
            return SiteInventory(archive, max_bytes=100)
        with patch('archive_frontier.SiteInventory', side_effect=bounded):
            result = self.scan()
        self.assertEqual(result['pages_scanned'], 1)
        self.assertEqual(result['metadata_unavailable'], 0)
        self.assertEqual(result['remaining'], 2)
        result = self.scan()
        self.assertEqual(result['pages_scanned'], 3)
        self.assertEqual(result['metadata_unavailable'], 0)
        self.assertFalse(result['remaining'])

    def test_new_tree_resets_only_its_cursor_and_does_not_repeat_cached_evidence(self):
        self.page('seed.example', 'index.html', 'first.example')
        self.page('other.example', 'index.html', 'unchanged.example')
        self.commit(); self.scan()
        self.page('seed.example', 'links.html', 'new.example')
        self.commit()
        result = self.scan()
        self.assertEqual(result['reads'], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM links').fetchone()[0], 3)
        self.assertEqual(result['hosts_complete'], 2)

    def test_expired_deadline_preserves_cursor_and_leaves_more_work_visible(self):
        self.page('seed.example', 'index.html', 'new.example')
        self.commit()
        result = self.scan(deadline=1)
        self.assertEqual(result['reads'], 0)
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(self.scan()['reads'], 1)

    def test_campaigns_share_durable_cursor_even_when_an_older_run_resumes(self):
        for index in range(4):
            self.page('seed.example', f'links{index}.html', f'guild{index}.example')
        self.commit()
        self.args.max_per_seed = 1
        runs = [Store(self.root / f'run{index}') for index in range(2)]
        for run in runs:
            self.addCleanup(run.close)
        for run in [runs[0], runs[1], runs[0]]:
            advance(self.args, run, root=self.store.root, deadline=float('inf'))
        self.assertEqual(self.store.db.execute('SELECT pages FROM discovery_frontier').fetchone()[0], 3)
        self.assertEqual(runs[0].db.execute('SELECT COUNT(*) FROM links').fetchone()[0], 3)
        self.assertEqual(self.git('status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
