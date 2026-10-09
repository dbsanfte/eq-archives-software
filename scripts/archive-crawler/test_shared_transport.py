"""Real HTTP/Ruby regressions for independent workers sharing one connection."""
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from acquisition import Downloader
from common import CrawlError, Store
from test_downloader import FixtureServer
from wayback_transport import SharedWayback, current_transport


class SharedTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.stop = threading.Event()
        self.shared = SharedWayback(self.stop, 'http://127.0.0.1:' + str(self.server.server_port))
        self.addCleanup(self.close)

    def close(self):
        self.shared.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()

    def args(self, **overrides):
        return SimpleNamespace(**{'delay': .04, 'bytes_per_second': 10000000, 'max_requests': 2,
            'max_page_bytes': 4096, 'max_bytes': 8192, 'max_seconds': 30, **overrides})

    def job(self, name):
        return {'op': 'capture_file', 'url': f'http://{name}.example/', 'timestamp': '20000101000000',
                'from': '19990101000000', 'to': '20061231235959', 'destination': str(self.root / (name + '.html'))}

    def test_workers_take_fair_turns_and_keep_one_connection_with_separate_durable_budgets(self):
        entered, release = threading.Event(), threading.Event()
        errors, usage = [], {}
        original = self.shared.request

        def gated(job):
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return original(job)

        def work(name, count):
            store = Store(self.root / name)
            try:
                with self.shared.bind():
                    client = Downloader(store, self.args(max_requests=count))
                    for _ in range(count):
                        client.call(self.job(name))
                    with self.assertRaisesRegex(CrawlError, 'Cumulative Wayback budget'):
                        client.call(self.job(name))
                    client.close()
                    with self.assertRaisesRegex(CrawlError, 'session is closed'):
                        client.call(self.job(name))
                    usage[name] = store.get('wayback_transport')
                    with self.assertRaisesRegex(CrawlError, 'Cumulative Wayback budget'):
                        Downloader(store, self.args(max_requests=count))
            except BaseException as error:
                errors.append(error)
            finally:
                store.close()

        with patch.object(self.shared, 'request', side_effect=gated):
            capture = threading.Thread(target=work, args=('capture', 2))
            candidate = threading.Thread(target=work, args=('candidate', 1))
            capture.start()
            self.assertTrue(entered.wait(5))
            candidate.start()
            limit = time.monotonic() + 5
            while len(self.shared.waiters) < 2 and time.monotonic() < limit:
                time.sleep(.01)
            self.assertEqual(len(self.shared.waiters), 2)
            release.set()
            capture.join(5)
            candidate.join(5)
        self.assertFalse(capture.is_alive() or candidate.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([url.split('http://')[1] for url, _ in self.server.requests],
                         ['capture.example/', 'candidate.example/', 'capture.example/'])
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(usage['capture']['requests'], 2)
        self.assertEqual(usage['candidate']['requests'], 1)
        self.assertEqual(usage['candidate']['bytes'], len(self.server.body))
        self.assertEqual(usage['capture']['bytes'], 2 * len(self.server.body))
        for (_, start), (_, end) in zip(self.server.requests, self.server.requests[1:]):
            self.assertGreaterEqual(end - start, .03)
        self.assertIsNone(current_transport.get())
        self.assertTrue(self.shared.healthy())

    def test_shared_backoff_survives_a_session_hitting_its_time_budget(self):
        self.server.statuses = [429, 200]
        store = Store(self.root / 'capture')
        other = Store(self.root / 'candidate')
        try:
            with self.shared.bind():
                first = Downloader(store, self.args(max_seconds=.2))
                with self.assertRaisesRegex(CrawlError, 'wall-clock budget'):
                    first.call(self.job('capture'))
                first.close()
                second = Downloader(other, self.args())
                second.call(self.job('candidate'))
                second.close()
            self.assertGreaterEqual(self.server.requests[1][1] - self.server.requests[0][1], 4.9)
            self.assertEqual(self.server.connections, 1)
            self.assertEqual(store.get('wayback_transport')['requests'], 1)
            self.assertEqual(other.get('wayback_transport')['requests'], 1)
        finally:
            store.close()
            other.close()

    def test_waiting_counts_toward_deadline_without_charging_other_jobs_requests(self):
        self.waiting_client(stopping=False)

    def test_shutdown_wakes_waiters_without_starting_another_request(self):
        self.waiting_client(stopping=True)

    def waiting_client(self, stopping):
        errors, usage = [], []
        def work():
            store = Store(self.root / 'candidate')
            try:
                with self.shared.bind():
                    client = Downloader(store, self.args(max_seconds=.15 if not stopping else 20))
                    try:
                        client.call(self.job('candidate'))
                    except CrawlError as error:
                        errors.append(str(error))
                    usage.append(store.get('wayback_transport'))
                    client.close()
            finally:
                store.close()
        with self.shared.turn(time.monotonic() + 10):
            thread = threading.Thread(target=work)
            thread.start()
            if stopping:
                self.stop.set()
                self.shared.wake()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertIn('Worker stopped' if stopping else 'wall-clock budget', errors[0])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(usage[0].get('requests', 0), 0)
        if not stopping:
            self.assertGreaterEqual(usage[0]['seconds'], .15)
        self.assertEqual(list(self.shared.waiters), [])

    def test_lost_response_pauses_every_worker_and_preserves_uncertain_reservation(self):
        process = Mock(stdout=io.StringIO('invalid output\n'))
        process.poll.return_value = None
        store = Store(self.root / 'candidate')
        try:
            with patch('wayback_transport.subprocess.Popen', return_value=process), self.shared.bind():
                client = Downloader(store, self.args())
                with self.assertRaisesRegex(CrawlError, 'upstream output omitted'):
                    client.call(self.job('candidate'))
                self.assertFalse(self.shared.healthy())
                saved = store.get('wayback_transport')
                self.assertEqual((saved['requests'], saved['bytes']), (2, 8192))
                with self.assertRaisesRegex(CrawlError, 'worker recovery'):
                    client.call(self.job('candidate'))
                self.assertEqual(process.stdin.write.call_count, 1)
                client.close()
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
