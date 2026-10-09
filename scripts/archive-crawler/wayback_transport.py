"""Fair, process-local sharing of the portal's single Wayback connection.

Worker contexts opt in; standalone operator commands retain their own client.
Each turn is one bounded downloader command, never an entire site or campaign.
"""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import subprocess
import threading
import time

from common import CrawlError

current_transport = ContextVar('wayback_transport', default=None)


class TransportUnavailable(CrawlError):
    """A stopped worker must pause, rather than skip sites or start paid work."""


class SharedWayback:
    def __init__(self, stop, origin=None):
        self.stop = stop
        self.origin = origin  # Only the Ruby client's loopback fixture is allowed.
        self.condition = threading.Condition()
        self.waiters = deque()
        self.process = None
        self.failed = False

    @contextmanager
    def bind(self):
        token = current_transport.set(self)
        try:
            yield
        finally:
            current_transport.reset(token)

    @contextmanager
    def turn(self, deadline):
        ticket = object()
        with self.condition:
            self.waiters.append(ticket)
        try:
            with self.condition:
                while True:
                    self.require_available()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CrawlError('Wayback wall-clock budget reached while waiting for transport')
                    if self.waiters[0] is ticket:
                        break
                    self.condition.wait(min(remaining, 1))
            yield
        finally:
            with self.condition:
                self.waiters.remove(ticket)
                self.condition.notify_all()

    def request(self, job):
        """Called only by the turn holder; limits and accounting belong to it."""
        try:
            if self.process is None:
                self.process = subprocess.Popen(['ruby', str(Path(__file__).with_name('downloader.rb'))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if self.origin:
                job = {**job, 'transport': {**job['transport'], 'origin': self.origin}}
            self.process.stdin.write(json.dumps(job) + '\n')
            self.process.stdin.flush()
            response = json.loads(self.process.stdout.readline())
            if not isinstance(response, dict) or type(response.get('ok')) is not bool or not isinstance(response.get('transport'), dict):
                raise ValueError('Invalid transport response')
            return response
        except (OSError, ValueError):
            # Never open a second connection after an uncertain/lost response.
            # Both workers pause; their saved budgets survive process recovery.
            self.failed = True
            raise TransportUnavailable('Shared Wayback transport stopped; upstream output omitted') from None

    def require_available(self):
        if self.stop.is_set():
            raise TransportUnavailable('Worker stopped; resume explicitly')
        if not self.healthy():
            raise TransportUnavailable('Shared Wayback transport stopped; resume explicitly after worker recovery')

    def healthy(self):
        return not self.failed and (self.process is None or self.process.poll() is None)

    def wake(self):
        with self.condition:
            self.condition.notify_all()

    def close(self):
        # The process lease holder calls this only after every worker stopped.
        if self.process is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process = None
