"""Exercise CI exemptions against real Git histories, including complete PR diffs."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('ci-scope.py')
spec = importlib.util.spec_from_file_location('ci_scope', SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.base = self.save({'README.md': 'overview', 'app.py': 'code',
                               'docs/README.md': 'guide'})

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()

    def save(self, files):
        for name, text in files.items():
            path = self.root / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        self.git('add', '-A')
        self.git('commit', '-qm', 'test')
        return self.git('rev-parse', 'HEAD')

    def pr(self, head, *, base=None, draft=False):
        return {'pull_request': {'draft': draft, 'base': {'sha': base or self.base},
                                 'head': {'sha': head}}}

    def test_root_and_nested_readmes_skip_pr_and_push(self):
        head = self.save({'README.md': 'new', 'docs/README.md': 'new guide'})
        for event, payload in [('pull_request', self.pr(head)),
                               ('push', {'before': self.base, 'after': head})]:
            with self.subTest(event=event):
                self.assertFalse(scope.decision(event, payload, repo=self.root)[0])

    def test_mixed_change_runs(self):
        head = self.save({'README.md': 'new', 'app.py': 'changed'})
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])
        self.assertTrue(scope.decision('push', {'before': self.base, 'after': head}, repo=self.root)[0])

    def test_code_in_earlier_commit_cannot_hide_behind_readme_push(self):
        self.save({'app.py': 'changed'})
        head = self.save({'README.md': 'latest'})
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_other_docs_are_not_exempt(self):
        head = self.save({'docs/pipeline.svg': '<svg/>', 'AGENTS.md': 'instructions'})
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_readme_named_directory_does_not_hide_code(self):
        head = self.save({'new/README.md/app.py': 'code'})
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_renaming_code_to_readme_still_runs(self):
        head = self.save({'app.py': None, 'new/README.md': 'code'})
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_readme_deletion_skips(self):
        head = self.save({'README.md': None})
        self.assertFalse(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_complete_diff_not_first_300_files(self):
        files = {f'docs/{n:04}/README.md': 'doc' for n in range(350)}
        files['z/app.py'] = 'code'
        head = self.save(files)
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_base_branch_changes_are_not_part_of_pr(self):
        self.git('checkout', '-qb', 'feature')
        head = self.save({'README.md': 'feature'})
        self.git('checkout', '-q', 'master')
        base = self.save({'app.py': 'unrelated base change'})
        self.assertFalse(scope.decision('pull_request', self.pr(head, base=base), repo=self.root)[0])

    def test_draft_skips_then_ready_runs_same_head(self):
        head = self.save({'app.py': 'changed'})
        self.assertFalse(scope.decision('pull_request', self.pr(head, draft=True), repo=self.root)[0])
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_manual_dispatch_always_runs(self):
        self.assertTrue(scope.decision('workflow_dispatch', {}, repo=self.root)[0])

    def test_empty_diff_and_unknown_event_run(self):
        self.assertTrue(scope.decision('pull_request', self.pr(self.base), repo=self.root)[0])
        self.assertTrue(scope.decision('unknown', {}, repo=self.root)[0])

    def test_new_ref_runs(self):
        self.assertTrue(scope.decision('push', {'before': '0'*40, 'after': self.base}, repo=self.root)[0])

    def test_unusual_paths_remain_distinct(self):
        head = self.save({'README.md\napp.py': 'code'})
        self.assertTrue(scope.decision('pull_request', self.pr(head), repo=self.root)[0])

    def test_invalid_revision_is_rejected(self):
        with self.assertRaises(ValueError):
            scope.decision('pull_request', self.pr('--output=bad'), repo=self.root)

    def test_readme_cli_emits_explicit_exemption_and_summary(self):
        head = self.save({'README.md': 'updated guide'})
        event = self.root/'event.json';output = self.root/'output';summary = self.root/'summary'
        event.write_text(json.dumps(self.pr(head)))
        subprocess.run(['python3', str(SCRIPT.resolve())], cwd=self.root, check=True,
            env={**os.environ, 'GITHUB_EVENT_NAME': 'pull_request',
                 'GITHUB_EVENT_PATH': str(event), 'GITHUB_OUTPUT': str(output),
                 'GITHUB_STEP_SUMMARY': str(summary)}, capture_output=True)
        self.assertEqual(output.read_text(), 'required=false\n')
        self.assertIn('Only README.md files changed', summary.read_text())

    def test_missing_history_cannot_write_skip_output(self):
        event = self.root/'event.json';output = self.root/'output'
        event.write_text(json.dumps(self.pr('a'*40)))
        result = subprocess.run(['python3', str(SCRIPT.resolve())], cwd=self.root,
            env={**os.environ, 'GITHUB_EVENT_NAME': 'pull_request',
                 'GITHUB_EVENT_PATH': str(event), 'GITHUB_OUTPUT': str(output)},
            capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
