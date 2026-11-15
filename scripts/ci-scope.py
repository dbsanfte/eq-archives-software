"""Skip expensive CI only for exact README.md paths; keep required checks present."""
import json
import os
from pathlib import Path
import re
import subprocess


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Missing or invalid event commit')
    return value


def decision(event_name, event, *, repo='.'):
    if event_name == 'workflow_dispatch':
        return True, 'Manual dispatch explicitly requests validation.'
    if event_name == 'pull_request':
        pr = event['pull_request']
        if pr.get('draft') is True:
            return False, 'Draft PR; validate when it becomes ready for review.'
        base, head = commit(pr['base']['sha']), commit(pr['head']['sha'])
        revision = base + '...' + head
    elif event_name == 'push':
        base, head = commit(event['before']), commit(event['after'])
        if base == '0' * 40 or head == '0' * 40:
            return True, 'New or deleted ref; use full validation.'
        revision = base + '..' + head
    else:
        return True, 'Unrecognized event; use full validation.'
    # NUL delimiters preserve unusual names; --no-renames examines both ends
    # so moving application code to README.md cannot exempt its deletion.
    paths = subprocess.run(
        ['git', 'diff', '--name-only', '--no-renames', '-z', revision, '--'],
        cwd=repo, check=True, capture_output=True).stdout.split(b'\0')
    paths = [path for path in paths if path]
    if paths and all(path.rsplit(b'/', 1)[-1] == b'README.md' for path in paths):
        return False, f'Only README.md files changed ({len(paths)}); skip builds and deployment.'
    return True, 'Changes include other paths, or no diff; use full validation.'


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    required, reason = decision(os.environ['GITHUB_EVENT_NAME'], event)
    # Write only after a successful complete diff. Unknown history cannot
    # produce a false exemption; the dependent required check fails closed.
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'required={str(required).lower()}\n')
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary, 'a') as output:
            output.write(reason + '\n')
    print(reason)


if __name__ == '__main__':
    main()
