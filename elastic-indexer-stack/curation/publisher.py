"""Publish one approved batch through a separate bare, treeless Git repository.

Only changed ancestor trees are read. There is no worktree, index, status, glob,
archive checkout mutation, or force push. A marker recovers ambiguous pushes.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from common import CrawlError, digest
from captures import check_manifest, verified_path

REMOTE = "git@github.com:dbsanfte/eq-archives.git"


def git_failure(arguments, stderr):
    """Describe a known failure without exposing upstream output or arguments."""
    operation = next((value for value in arguments if value in
                      {'init','remote','config','fetch','rev-parse','cat-file','hash-object','commit-tree','push','ls-remote'}), 'operation')
    diagnostic = stderr.lower()
    hints = [(b'no user exists for uid', 'The publication container needs a Unix account for its runtime UID.'),
             (b'permission denied (publickey)', 'GitHub rejected the publication SSH credential.'),
             (b'host key verification failed', 'The GitHub SSH host identity could not be verified.'),
             (b'could not resolve hostname', 'The publication service could not resolve GitHub.'),
             (b'no space left on device', 'Publication staging has no free disk space.'),
             (b'connection timed out', 'The connection to GitHub timed out.')]
    hint = next((message for pattern,message in hints if pattern in diagnostic), 'Upstream details were omitted to protect credentials.')
    return f'Archive Git {operation} failed. {hint} The approved sources remain in staging.'


class Publisher:
    def __init__(self, root, remote=REMOTE, key_file="/run/secrets/archive_publish_key"):
        self.root = Path(root)
        self.repo = self.root / "publisher.git"
        self.reader = self.root / "publisher-reader.git"
        self.tree_cache = {}
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "EQ archives curation",
                    "GIT_AUTHOR_EMAIL": "curation@eqarchives.org", "GIT_COMMITTER_NAME": "EQ archives curation",
                    "GIT_COMMITTER_EMAIL": "curation@eqarchives.org"}
        self.private = None
        if remote == REMOTE:
            self.private = tempfile.TemporaryDirectory(prefix="curation-ssh-")
            key = Path(self.private.name) / "key"
            key.write_bytes(Path(key_file).read_bytes())
            key.chmod(0o600)
            known_hosts = Path(__file__).with_name("github-known-hosts")
            self.env["GIT_SSH_COMMAND"] = (f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes "
                                           f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts} "
                                           "-o ConnectTimeout=15")
        if not self.repo.exists():
            self.repo.mkdir(mode=0o700)
            self.git("init", "--bare", "--template=", "-q")
            self.git("remote", "add", "origin", remote)
            self.git("config", "remote.origin.promisor", "true")
            self.git("config", "remote.origin.partialclonefilter", "tree:0")
            self.git("config", "extensions.partialClone", "origin")
            self.git("config", "gc.auto", "0")
            self.git("config", "maintenance.auto", "false")
            self.git("config", "pack.window", "0")
            self.git("config", "pack.threads", "1")
        if self.git("remote", "get-url", "origin").decode().strip() != remote:
            raise CrawlError("Publisher remote differs from the configured archive repository")
        if not self.reader.exists():
            self.reader.mkdir(mode=0o700)
            self.git("init", "--bare", "--template=", "-q", local=True)
        if self.git("remote", local=True).strip():
            raise CrawlError("Publisher object reader must have no remotes")

    def close(self):
        if self.private:
            self.private.cleanup()

    def git(self, *arguments, data=None, optional=False, local=False):
        env = {**self.env, "GIT_OBJECT_DIRECTORY": str(self.repo / "objects")} if local else self.env
        try:
            result = subprocess.run(["git", "--git-dir", str(self.reader if local else self.repo), *arguments], input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=180)
        except subprocess.TimeoutExpired:
            raise CrawlError("Archive Git operation timed out; resume to check the remote publication marker") from None
        if result.returncode:
            if optional:
                return None
            raise CrawlError(git_failure(arguments,result.stderr))
        if b"filtering not recognized" in result.stderr or b"does not support filter" in result.stderr:
            raise CrawlError("Remote does not support bounded partial fetches")
        return result.stdout

    def fetch(self):
        self.git("fetch", "--no-tags", "--depth=1", "--filter=tree:0", "origin", "master")
        return self.git("rev-parse", "FETCH_HEAD").decode().strip()

    def object(self, kind, oid):
        value = self.git("cat-file", kind, oid, local=True, optional=True)
        if value is None:
            # Git's implicit lazy fetch uses blob:none and can recursively fetch
            # every descendant tree. Explicit wants with tree:0 retrieve only
            # the requested object; the remote-free reader cannot widen them.
            self.git("-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags", "--no-write-fetch-head",
                     "--filter=tree:0", "origin", oid)
            value = self.git("cat-file", kind, oid, local=True)
        return value

    def entries(self, tree):
        if not tree:
            return {}
        if tree in self.tree_cache:
            return self.tree_cache[tree].copy()
        entries = {}
        raw, position = self.object("tree", tree), 0
        while position < len(raw):
            end = raw.index(b"\0", position)
            mode, path = raw[position:end].split(b" ", 1)
            numeric = int(mode, 8)
            kind = "tree" if numeric == 0o40000 else "commit" if numeric == 0o160000 else "blob"
            entries[path.decode("utf-8", "surrogateescape")] = (f"{numeric:06o}", kind, raw[end + 1:end + 21].hex())
            position = end + 21
        self.tree_cache[tree] = entries
        return entries.copy()

    def lookup(self, tree, path):
        pieces = path.split("/")
        for index, piece in enumerate(pieces):
            entry = self.entries(tree).get(piece)
            if entry is None:
                return None
            if index == len(pieces) - 1:
                return entry
            if entry[1] != "tree":
                raise CrawlError("Archive destination collides with an existing file")
            tree = entry[2]

    def overlay(self, tree, changes):
        entries = self.entries(tree)
        grouped = {}
        for path, oid in changes.items():
            name, separator, remainder = path.partition("/")
            if separator:
                grouped.setdefault(name, {})[remainder] = oid
            else:
                old = entries.get(name)
                if old and old != ("100644", "blob", oid):
                    raise CrawlError("Archive destination already contains different data; publication paused")
                entries[name] = ("100644", "blob", oid)
        for name, children in grouped.items():
            old = entries.get(name)
            if old and old[1] != "tree":
                raise CrawlError("Archive destination collides with an existing file")
            subtree = self.overlay(old[2] if old else None, children)
            entries[name] = ("040000", "tree", subtree)
        # mktree --missing still lazily fetches promisor children to check their
        # types on older Git. Serialize canonical tree entries directly so an
        # unchanged subtree/blob never triggers an archive-sized fetch.
        ordered = sorted(entries.items(), key=lambda entry: entry[0].encode("utf-8", "surrogateescape") +
                         (b"/" if entry[1][1] == "tree" else b""))
        data = b"".join(f"{int(mode, 8):o} {name}".encode("utf-8", "surrogateescape") + b"\0" + bytes.fromhex(oid)
                        for name, (mode, kind, oid) in ordered)
        return self.git("hash-object", "-w", "-t", "tree", "--stdin", data=data).decode().strip()

    def publish(self, manifest, expected):
        if digest(manifest) != expected:
            raise CrawlError("Batch changed since publication approval")
        check_manifest(self.root, manifest)
        batch_id = manifest["batch_id"]
        marker_path = f"crawl-manifests/{batch_id}.json"
        marker = {"schema": 1, "batch_id": batch_id, "manifest_sha256": expected,
                  "sites": [{key: site[key] for key in ("id", "url", "scope", "manifest_sha256")} for site in manifest["sites"]],
                  "captures": [{key: capture[key] for key in ("url", "timestamp", "sha256", "bytes", "archive_path", "source")}
                               for capture in manifest["captures"]]}
        # Git streams one source at a time. Never materialize a whole site's
        # images/downloads in RAM, and keep the single site-level commit/push.
        changes = {capture['archive_path']: self.git('hash-object', '-w', '--',
                   str(verified_path(self.root, capture))).decode().strip() for capture in manifest['captures']}
        changes[marker_path] = self.git('hash-object', '-w', '--stdin',
            data=(json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()).decode().strip()
        for attempt in range(3):
            parent = self.fetch()
            tree = self.object("commit", parent).split(b"\n", 1)[0].removeprefix(b"tree ").decode()
            # `show commit:missing-path` recursively searches for diagnostic
            # suggestions, fetching unrelated trees. Resolve ancestors ourselves.
            marker_entry = self.lookup(tree, marker_path)
            if marker_entry:
                if marker_entry[1] != "blob":
                    raise CrawlError("Remote publication marker is not a file")
                existing = self.object("blob", marker_entry[2])
                if json.loads(existing).get("manifest_sha256") != expected:
                    raise CrawlError("Remote publication marker belongs to a different approved manifest")
                for path, oid in changes.items():
                    entry = self.lookup(tree, path)
                    if path != marker_path and (not entry or entry[1:] != ("blob", oid)):
                        raise CrawlError("Published source changed after its marker; indexing paused")
                return {"commit": parent, "marker": marker_path, "recovered": True}
            updated = self.overlay(tree, changes)
            commit = self.git("commit-tree", updated, "-p", parent,
                              data=f"Archive approved capture batch {batch_id}\n\nManifest SHA256: {expected}\n".encode()).decode().strip()
            # Fast-forward only. Ref races or uncertain failures are checked by
            # fetching the marker before another commit/push is attempted.
            result = self.git("push", "origin", commit + ":refs/heads/master", optional=True)
            if result is not None:
                return {"commit": commit, "marker": marker_path, "recovered": False}
        raise CrawlError("Archive publication could not be confirmed; resume to check the remote marker")


def publish(root, manifest, expected):
    publisher = Publisher(root)
    try:
        return publisher.publish(manifest, expected)
    finally:
        publisher.close()
