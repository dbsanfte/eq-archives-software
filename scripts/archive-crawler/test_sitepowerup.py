"""SitePowerUp boards are query-defined accounts, never whole /mb directories."""
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from common import CrawlError, Store, capture_scope, digest, site_identity, within_scope
from sitepowerup import address, archived_board, belongs, board_url, candidate_url, source_page
from sitepowerup_capture import Capture

BOARD = 'http://www.sitepowerup.com/mb/view.asp?Action=Display&BoardID=102010'
MESSAGE = 'http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=102010&Reply=12155'
PREFIX = MESSAGE.rsplit('&', 1)[0]
STAMP = '20000109050003'


def html(board='102010', reply=None):
    return (f'<title>Words of Enchantment</title><p>EverQuest guild history.</p>'
            f'<a href="view.asp?Action=Display&amp;BoardID=104254">Another board</a>'
            f'<a href="view.asp?Action=Display&amp;BoardID={board}">Return</a>'
            f'<input type="hidden" name="BoardID" value="{board}">' +
            (f'<input type="hidden" name="Reply" value="{reply}">' if reply else '')).encode()


class Downloader:
    catalogs, sources, instances, failure = {}, {}, [], None

    def __init__(self, store, args):
        self.store, self.args, self.calls, self.closed = store, args, [], False
        self.instances.append(self)

    def call(self, job):
        self.calls.append(job)
        if self.failure and self.failure(job):
            raise CrawlError('Wayback connection failed after bounded retries')
        if job['op'] == 'sitepowerup_list':
            return self.catalogs.get((job['url'], job['from'], job.get('resume_key')), {'captures': [], 'resume_key': None})
        assert job['op'] == 'capture'
        body = self.sources[(job['url'], job['timestamp'])]
        Path(job['destination']).write_bytes(body)
        return {'url': job['url'], 'timestamp': job['timestamp'], 'requested_timestamp': job['timestamp'],
                'bytes': len(body), 'sha256': digest(body), 'content_type': 'text/html'}

    def close(self):
        self.closed = True


def listing(url, records, level=1, resume=None, next_key=None):
    Downloader.catalogs[(url, '19990101000000' if level == 1 else '20020101000000', resume)] = {
        'captures': [{'url': source, 'timestamp': stamp, 'digest': 'same-content', 'length': '200'} for source, stamp in records], 'resume_key': next_key}


class IdentityTests(unittest.TestCase):
    def test_messages_group_under_their_board_without_merging_other_boards(self):
        board = 'http://www.sitepowerup.com/mb/view.asp?Action=Display&BoardID=102010'
        message = 'http://www.sitepowerup.com/mb/view.asp?Action=Reply&BoardID=102010&Reply=12155'
        self.assertEqual(site_identity(board), site_identity(message))
        self.assertNotEqual(site_identity(board), site_identity(message.replace('102010', '104254')))
        self.assertFalse(within_scope(message, board))  # Existing exact approvals do not widen.

    def test_parameter_case_order_and_host_aliases_do_not_change_the_board(self):
        alias = 'https://sitepowerup.com/MB/VIEW.ASP?reply=12155&BOARDID=102010&action=reply&page=2'
        self.assertEqual(site_identity(alias), site_identity(BOARD))
        self.assertEqual(address(alias)['url'], alias)
        self.assertEqual(board_url(alias), BOARD)
        self.assertEqual(capture_scope(alias, 'sitepowerup'), BOARD)
        self.assertEqual(capture_scope(MESSAGE, 'page'), MESSAGE)

    def test_forms_unrelated_hosts_and_ambiguous_ids_are_never_capture_candidates(self):
        for url in (BOARD.replace('Display', 'Post'), BOARD.replace('Display', 'Delete'),
                    BOARD + '&boardid=999', BOARD + '&BoardID=102010', BOARD.replace('102010','1%262'),
                    BOARD.replace('102010','0'), BOARD.replace('102010',''),
                    BOARD.replace('view.asp','post.asp'), PREFIX,
                    'http://www.sitepowerup.com/mb/'):
            self.assertIsNone(candidate_url(url), url)
        for url in (BOARD.replace('.com', '.com.evil.example'), BOARD.replace('.com/', '.com:8080/'), 'bad'):
            self.assertIsNone(address(url), url)
        with self.assertRaises(CrawlError):capture_scope('http://guild.example/', 'sitepowerup')

    def test_hidden_identity_and_return_links_verify_the_saved_message(self):
        self.assertTrue(belongs(source_page(html(reply='12155'), MESSAGE)[0], MESSAGE, '102010'))
        self.assertTrue(belongs(source_page(html(), BOARD)[0], BOARD, '102010'))
        for source in (html('999'), html(reply='99'), b'<title>Error</title><p>Not found</p>', b'<p>Login required</p>', b''):
            self.assertFalse(belongs(source_page(source, MESSAGE)[0], MESSAGE, '102010'))
        self.assertFalse(belongs(source_page(html(), BOARD)[0], BOARD, '999'))

    def test_legacy_query_filenames_are_board_coverage_only(self):
        for path in ('mb/view.asp_BoardID=102010', 'mb/view.asp_Action=Reply_and_BoardID=102010_and_Reply=12155',
                     'mb/view.asp?reply=12155&BoardID=102010&Action=Reply'):
            self.assertEqual(archived_board(path), '102010')
        for path in ('mb/view.asp_Action=Post_and_BoardID=102010', 'mb/view.asp_BoardID=1020100_and_BoardID=102010', 'user/login.asp_BoardID=102010'):
            self.assertIsNone(archived_board(path))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.store = Store(directory.name);self.addCleanup(self.store.close)
        self.capture = Capture(self.store)
        Downloader.catalogs, Downloader.sources, Downloader.instances, Downloader.failure = {}, {}, [], None

    def fixture(self):
        self.capture.plan(MESSAGE)
        listing(BOARD, [(BOARD, STAMP)])
        listing(PREFIX, [(MESSAGE, STAMP), (MESSAGE.replace('102010','1020100'), STAMP)], next_key='next')
        page = MESSAGE + '&Page=2'
        listing(PREFIX, [(page, STAMP)], resume='next')
        late = 'https://sitepowerup.com/mb/view.asp?reply=12155&boardid=102010&action=reply'
        listing(PREFIX, [(late, '20061231235959'), (late, '20070101000000')], level=2)
        Downloader.sources = {(BOARD, STAMP): html(), (MESSAGE, STAMP): html(reply='12155'),
                              (page, STAMP): html(reply='12155'), (late, '20061231235959'): html(reply='12155')}

    def test_complete_dated_board_capture_preserves_query_identity_and_layout(self):
        self.fixture();result = self.capture.run(Downloader)
        self.assertEqual(result['strategy'], 'sitepowerup-v1')
        self.assertEqual(result['coverage']['state'], 'complete')
        self.assertEqual(len(result['captures']), 4)
        self.assertEqual(result['capture_window']['to'], '20061231235959')
        self.assertTrue(all(c['timestamp'] <= '20061231235959' for c in result['captures']))
        for capture in result['captures']:
            self.assertIn('/mb/view.asp?', capture['archive_path'])
            self.assertEqual((self.store.root/capture['path']).read_bytes(), Downloader.sources[(capture['url'],capture['timestamp'])])
        self.assertEqual(len(Downloader.instances), 1)
        self.assertTrue(Downloader.instances[0].closed)
        self.assertFalse(any('1020100' in job['url'] for job in Downloader.instances[0].calls if job['op']=='capture'))
        self.capture.run(Downloader)
        self.assertEqual(len(Downloader.instances), 1)
        self.assertEqual(self.capture.verify()['verified_captures'], 4)

    def test_catalog_resume_does_not_redownload_completed_pages(self):
        self.fixture();Downloader.failure = staticmethod(lambda job:job.get('resume_key') == 'next')
        result = self.capture.run(Downloader)
        self.assertEqual(result['coverage']['state'], 'paused')
        self.assertEqual(len(result['captures']), 2)
        Downloader.failure = None
        self.assertEqual(len(Capture(self.store).run(Downloader)['captures']), 4)
        self.assertNotIn(MESSAGE, [c['url'] for c in Downloader.instances[-1].calls if c['op']=='capture'])

    def test_cumulative_limits_and_source_checks_survive_resume(self):
        self.fixture();self.capture.config['limits']['max_captures'] = 1
        self.store.set('sitepowerup_config', self.capture.config)
        self.assertEqual(self.capture.run(Downloader)['coverage']['state'], 'bounded')
        self.capture.extend({'max_captures': 5})
        result = self.capture.run(Downloader)
        self.assertEqual(len(result['captures']), 4)
        (self.store.root/result['captures'][0]['path']).write_bytes(b'changed')
        with self.assertRaises(CrawlError):self.capture.verify()

    def test_unverified_source_is_retained_but_never_exported_for_publication(self):
        self.fixture();Downloader.sources[(MESSAGE, STAMP)] = html('999')
        result = self.capture.run(Downloader)
        self.assertEqual(len(result['captures']), 3)
        self.assertEqual(result['coverage']['counts']['excluded'], 1)
        self.assertIn('membership', result['notes'][0]['reason'])

    def test_operator_plans_cannot_mix_board_platforms(self):
        self.capture.plan(BOARD)
        from ezboard_capture import Capture as EzboardCapture
        with self.assertRaises(CrawlError):EzboardCapture(self.store)
        with self.assertRaises(CrawlError):self.capture.plan(BOARD)

    def test_operator_cli_plans_inspects_extends_and_replays_completed_checkpoint_offline(self):
        root=self.store.root/'operator'
        command=[sys.executable,str(Path(__file__).with_name('sitepowerup_capture.py')),'--work-dir',str(root)]
        def invoke(*args):return subprocess.run(command+list(args),capture_output=True,text=True)
        self.assertEqual(invoke('plan','--url','https://web.archive.org/web/2000/'+MESSAGE,'--max-captures','2').returncode,0)
        status=invoke('status');self.assertEqual(status.returncode,0)
        self.assertEqual(json.loads(status.stdout)['board'],'102010')
        self.assertEqual(invoke('verify').returncode,0)
        self.assertEqual(invoke('extend','--max-captures','3').returncode,0)
        self.assertEqual(invoke('extend','--max-captures','1').returncode,2)
        self.assertEqual(invoke('plan','--url',BOARD).returncode,2)
        checkpoint=Store(root)
        checkpoint.db.execute('UPDATE ez_queries SET done=1');checkpoint.db.commit();checkpoint.close()
        result=invoke('capture');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
