#!/usr/bin/env python3
"""Stage one SitePowerUp board and its dated ASP message views from Wayback."""
import argparse
import json

from common import CrawlError
from ezboard_capture import Capture as BoardCapture, DEFAULT_LIMITS, locked_store
import sitepowerup

LIMITS = {**DEFAULT_LIMITS, 'max_hosts': 2}


class Capture(BoardCapture):
    def __init__(self, store):
        super().__init__(store, sitepowerup)

    def plan(self, url, limits=None):
        scope = sitepowerup.board_url(url)
        if not scope:
            raise CrawlError('Use a SitePowerUp board or message URL with a numeric BoardID')
        return super().plan(scope, hosts=('sitepowerup.com', 'www.sitepowerup.com'), limits={**LIMITS, **(limits or {})})

    def learn(self, page, result):
        # The board-specific CDX prefixes enumerate indexes, messages and
        # pagination independently of the linked index sample. Never submit forms.
        for link in page.links:
            item = sitepowerup.candidate(link['url'], self.config['board'])
            if item:
                self.add_host(item['host'], {key: result[key] for key in ('url', 'timestamp', 'sha256')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Save a board and bounds; no network requests')
    plan.add_argument('--url', required=True)
    extend = commands.add_parser('extend', help='Increase cumulative limits without resetting usage')
    for key in ('max_captures', 'max_catalog_rows', 'max_requests', 'max_bytes', 'max_seconds'):
        for command in (plan, extend):
            command.add_argument('--' + key.replace('_', '-'), type=int)
    for name in ('capture', 'status', 'verify'):
        commands.add_parser(name)
    args = parser.parse_args()
    try:
        with locked_store(args.work_dir) as store:
            capture = Capture(store)
            limits = {key: getattr(args, key) for key in LIMITS if getattr(args, key, None) is not None}
            if args.command == 'plan':
                result = capture.plan(args.url, limits)
            elif args.command == 'extend':
                result = capture.extend(limits)
            elif args.command == 'capture':
                result = capture.run(progress=lambda status: print(json.dumps(status), flush=True))['coverage']
            elif args.command == 'verify':
                result = capture.verify()
            else:
                result = capture.status()
            print(json.dumps(result, indent=2))
            return 2 if result.get('state') in ('bounded', 'paused') else 0
    except (CrawlError, OSError) as error:
        print(str(error))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
