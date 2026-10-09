"""Synthetic regressions using URL forms verified in EQ Asylum archive captures."""
import json
import fcntl
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from common import CrawlError, Store, digest, within_scope
from ezboard import address, belongs, candidate, source_page
from ezboard_capture import Capture

BOARD = 'http://pub4.ezboard.com/beqasylum'
FORUM = 'http://pub4.ezboard.com/feqasylumgeneral'
MOVED = 'http://pub110.ezboard.com/feqasylumfrm25.showMessageRange?topicID=2358.topic&start=21&stop=37'
STAMP = '20000511120232'


def html(*urls, title='Lanys Community Forum'):
    return ('<title>' + title + '</title><p>EverQuest server discussions</p>' +
            ''.join('<a href="' + url.replace('&', '&amp;') + '">Forum</a>' for url in urls)).encode()


class FakeDownloader:
    instances = []
    catalogs = {}
    sources = {}
    failure = None

    def __init__(self, store, args):
        self.store, self.args, self.calls, self.closed = store, args, [], False
        self.instances.append(self)

    def call(self, job):
        self.calls.append(job)
        if self.failure and self.failure(job):
            raise CrawlError('Wayback connection failed after bounded retries')
        if job['op'] == 'ezboard_list':
            return self.catalogs.get((job['url'], job['from'], job['resume_key']), {'captures': [], 'resume_key': None})
        body = self.sources[(job['url'], job['timestamp'])]
        Path(job['destination']).write_bytes(body)
        return {'url': job['url'], 'timestamp': job['timestamp'], 'requested_timestamp': job['timestamp'],
                'bytes': len(body), 'sha256': digest(body), 'content_type': 'text/html'}

    def close(self):
        self.closed = True


class EzboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.capture = Capture(self.store)
        FakeDownloader.instances, FakeDownloader.catalogs, FakeDownloader.sources = [], {}, {}
        FakeDownloader.failure = None

    def listing(self, prefix, records, resume=None, next_key=None, tier=1):
        start = '19990101000000' if tier == 1 else '20020101000000'
        FakeDownloader.catalogs[(prefix, start, resume)] = {'captures': [
            {'url': url, 'timestamp': stamp, 'digest': 'CDX', 'length': '100'} for url, stamp in records], 'resume_key': next_key}

    def fixture(self):
        self.capture.plan(BOARD)
        self.listing(BOARD, [(BOARD, STAMP)])
        self.listing('http://pub4.ezboard.com/feqasylum', [(FORUM, STAMP)], next_key='next')
        page = FORUM + '?page=2'
        self.listing('http://pub4.ezboard.com/feqasylum', [(page, STAMP)], resume='next')
        self.listing('http://pub110.ezboard.com/feqasylum', [(MOVED, '20020602023020')], tier=2)
        FakeDownloader.sources = {(BOARD, STAMP): html(FORUM, 'http://pub110.ezboard.com/beqasylum'),
            (FORUM, STAMP): html(BOARD, page), (page, STAMP): html(BOARD),
            (MOVED, '20020602023020'): html('http://pub110.ezboard.com/beqasylum')}

    def test_sibling_forums_are_outside_generic_directory_but_in_verified_board(self):
        self.assertFalse(within_scope(FORUM, BOARD + '/'))
        self.assertTrue(belongs(source_page(html(BOARD), FORUM)[0], FORUM, 'eqasylum'))
        impostor = 'http://pub4.ezboard.com/feqasylumothergeneral'
        self.assertFalse(belongs(source_page(html('http://pub4.ezboard.com/beqasylumother'), impostor)[0], impostor, 'eqasylum', ['feqasylumothergeneral']))
        for url in (FORUM + '.showAddReplyScreenFromWeb?topicID=1.topic', 'http://pub4.ezboard.com/uuser.showPublicProfile',
                    'http://pub4.ezboard.com.evil.example/beqasylum', 'http://pub4.ezboard.com/bother', FORUM + '.showMessage'):
            self.assertIsNone(candidate(url, 'eqasylum'))
        self.assertEqual(address(MOVED)['kind'], 'message')

    def test_new_board_capture_includes_both_date_tiers_through_end_of_2006(self):
        self.capture.plan(BOARD)
        self.listing(BOARD, [(BOARD, '19990101000000')])
        self.listing(BOARD, [(BOARD, '20061231235959'), (BOARD, '20070101000000')], tier=2)
        FakeDownloader.sources = {(BOARD, stamp): html(FORUM) for stamp in ('19990101000000','20061231235959')}
        manifest = self.capture.run(FakeDownloader)
        self.assertEqual({c['timestamp'] for c in manifest['captures']}, {'19990101000000','20061231235959'})
        self.assertEqual(manifest['capture_window']['to'], '20061231235959')
        self.assertEqual(manifest['coverage']['state'], 'complete')
        self.assertTrue(all(call['to'] <= '20061231235959' for call in FakeDownloader.instances[0].calls))

    def test_saved_legacy_operator_plan_keeps_its_window_on_resume(self):
        self.capture.plan(BOARD)
        del self.capture.config['date_tiers']
        self.store.set('ezboard_config', self.capture.config)
        self.assertEqual(Capture(self.store).capture_window()['to'], '20071231235959')

    def test_pagination_moves_exact_query_paths_and_idempotent_resume(self):
        self.fixture()
        manifest = self.capture.run(FakeDownloader)
        self.assertEqual(manifest['coverage']['state'], 'complete')
        self.assertEqual(len(manifest['captures']), 4)
        moved = next(c for c in manifest['captures'] if c['url'] == MOVED)
        expected = 'websites/pub110.ezboard.com/20020602023020/' + MOVED.split('/', 3)[3]
        self.assertEqual(moved['archive_path'], expected)
        self.assertEqual((self.store.root / moved['path']).read_bytes(), FakeDownloader.sources[(MOVED, '20020602023020')])
        self.assertEqual(len(FakeDownloader.instances), 1)
        self.assertTrue(FakeDownloader.instances[0].closed)
        first_count = len(FakeDownloader.instances[0].calls)
        self.capture.run(FakeDownloader)
        self.assertEqual(len(FakeDownloader.instances), 1)
        self.assertEqual(len(FakeDownloader.instances[0].calls), first_count)
        self.assertEqual(self.capture.verify()['verified_captures'], 4)

    def test_interrupted_catalog_resumes_without_redownloading_saved_sources(self):
        self.fixture()
        FakeDownloader.failure = staticmethod(lambda job: job.get('resume_key') == 'next')
        first = self.capture.run(FakeDownloader)
        self.assertEqual(first['coverage']['state'], 'paused')
        self.assertEqual(len(first['captures']), 2)
        FakeDownloader.failure = None
        resumed = Capture(self.store).run(FakeDownloader)
        self.assertEqual(len(resumed['captures']), 4)
        self.assertNotIn(BOARD, [c['url'] for c in FakeDownloader.instances[-1].calls if c['op'] == 'capture'])

    def test_limits_do_not_reset_and_an_explicit_extension_retains_progress(self):
        self.fixture()
        self.capture.config['limits']['max_captures'] = 1
        self.store.set('ezboard_config', self.capture.config)
        result = self.capture.run(FakeDownloader)
        self.assertEqual(result['coverage']['state'], 'bounded')
        self.assertEqual(len(result['captures']), 1)
        self.capture.run(FakeDownloader)
        self.assertFalse(any(c['op'] == 'capture' for c in FakeDownloader.instances[-1].calls))
        self.capture.extend({'max_captures': 10})
        self.assertEqual(len(self.capture.run(FakeDownloader)['captures']), 4)
        with self.assertRaises(CrawlError):
            self.capture.extend({'max_captures': 1})

    def test_unrelated_prefix_and_soft_error_pages_are_excluded_with_sources_retained(self):
        self.capture.plan(BOARD)
        wrong = 'http://pub4.ezboard.com/feqasylumothergeneral'
        self.listing('http://pub4.ezboard.com/feqasylum', [(wrong, STAMP), (FORUM, STAMP)])
        FakeDownloader.sources = {(wrong, STAMP): html('http://pub4.ezboard.com/beqasylumother'),
                                  (FORUM, STAMP): html(title='System Message: Error')}
        result = self.capture.run(FakeDownloader)
        self.assertEqual(result['captures'], [])
        self.assertEqual(result['coverage']['counts']['excluded'], 2)
        self.assertEqual(len(result['notes']), 2)
        self.assertEqual(len(list((self.store.root / 'downloads').iterdir())), 2)

    def test_archive_collision_pauses_without_overwriting_first_capture(self):
        self.capture.plan(BOARD)
        other = BOARD + '/'
        self.listing(BOARD, [(BOARD, STAMP), (other, STAMP)])
        FakeDownloader.sources = {(BOARD, STAMP): html(), (other, STAMP): html(title='Different document')}
        result = self.capture.run(FakeDownloader)
        self.assertEqual(result['coverage']['state'], 'paused')
        self.assertIn('collide', result['coverage']['reason'])
        self.assertEqual(len(result['captures']), 1)
        self.assertEqual((self.store.root / result['captures'][0]['path']).read_bytes(), html())

    def test_changed_source_is_rejected(self):
        self.fixture(); manifest = self.capture.run(FakeDownloader)
        (self.store.root / manifest['captures'][0]['path']).write_bytes(b'changed')
        with self.assertRaises(CrawlError):
            self.capture.verify()

    def test_plan_requires_explicit_board_and_strict_transport_bounds(self):
        with self.assertRaises(CrawlError):
            self.capture.plan(FORUM)
        with self.assertRaises(CrawlError):
            self.capture.plan(BOARD, limits={'delay': -1})
        with self.assertRaises(CrawlError):
            self.capture.plan(BOARD, hosts=['evil.example'])

    def test_unlimited_transfer_is_valid_but_byte_time_and_request_budgets_stay_positive(self):
        planned = self.capture.plan(BOARD)
        limits = planned['limits']
        self.assertEqual((limits['delay'], limits['bytes_per_second']), (0, 0))
        for key, value in [('bytes_per_second', -1), ('bytes_per_second', False),
                           ('max_bytes', 0), ('max_requests', 0), ('max_seconds', 0), ('delay', -1), ('delay', False)]:
            with self.subTest(key=key, value=value), self.assertRaises(CrawlError):
                self.capture.validate_limits({**limits, key: value})
        # Existing standalone operator plans with explicit caps remain readable.
        self.capture.validate_limits({**limits, 'delay': 3, 'bytes_per_second': 131072})

    def test_repeated_cdx_cursor_pauses_and_keeps_saved_capture(self):
        self.fixture()
        FakeDownloader.catalogs[('http://pub4.ezboard.com/feqasylum', '19990101000000', 'next')]['resume_key']='next'
        result=self.capture.run(FakeDownloader)
        self.assertEqual(result['coverage']['state'],'paused')
        self.assertIn('continuation key',result['coverage']['reason'])
        self.assertEqual(len(result['captures']),2)

    def test_cli_plan_status_extension_and_concurrent_access_are_offline(self):
        directory=Path(self.directory.name)/'cli'
        command=[sys.executable,str(Path(__file__).with_name('ezboard_capture.py')),'--work-dir',str(directory)]
        planned=subprocess.run(command+['plan','--url','https://web.archive.org/web/2000/'+BOARD,'--host','pub110.ezboard.com',
                                        '--max-captures','1'],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(planned.stdout)['state'],'planned')
        status=json.loads(subprocess.check_output(command+['status']))
        self.assertEqual(status['hosts'],2);self.assertEqual(status['transport'],{})
        extended=json.loads(subprocess.check_output(command+['extend','--max-captures','5']))
        self.assertEqual(extended['limits']['max_captures'],5)
        self.assertEqual(json.loads(subprocess.check_output(command+['verify']))['verified_captures'],0)
        with (directory/'.ezboard.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            busy=subprocess.run(command+['status'],capture_output=True,text=True)
            self.assertEqual(busy.returncode,1);self.assertIn('Another board capture command',busy.stderr)
        repo=Path(self.directory.name)/'checkout';(repo/'.git').mkdir(parents=True)
        unsafe=subprocess.run([*command[:3],str(repo/'state'),'status'],capture_output=True,text=True)
        self.assertEqual(unsafe.returncode,1);self.assertFalse((repo/'state').exists())


if __name__ == '__main__':
    unittest.main()
