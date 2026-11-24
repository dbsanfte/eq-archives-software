"""Publish one approved batch through a separate bare, treeless Git repository.

Only changed ancestor trees are read. There is no worktree, index, status, glob,
archive checkout mutation, or force push. A marker recovers ambiguous pushes.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import partial
from shutil import rmtree

from common import CrawlError, digest
from captures import check_manifest, verified_path, archive_path
from publication_git import PublicationError, lease, recover, run as run_git, category
from publication_files import split as split_publication_files
from publication_pack import TreePack

REMOTE = "git@github.com:dbsanfte/eq-archives.git"
PACK_NAME = re.compile(r'pack-[a-f0-9]{40}\.idx')
PREPARATION_ROOT = 'publication-preparations'
PREPARATION = 'archive-'


def git_failure(arguments, stderr):
    """Describe a known failure without exposing upstream output or arguments."""
    operation = next((value for value in arguments if value in
                      {'init','remote','config','fetch','rev-parse','cat-file','hash-object','index-pack',
                       'pack-objects','commit-tree','push','ls-remote'}), 'operation')
    diagnostic = stderr.lower()
    hints = [(b'no user exists for uid', 'The publication container needs a Unix account for its runtime UID.'),
             (b'permission denied (publickey)', 'GitHub rejected the publication SSH credential.'),
             (b'host key verification failed', 'The GitHub SSH host identity could not be verified.'),
             (b'could not resolve hostname', 'The publication service could not resolve GitHub.'),
             (b'no space left on device', 'Publication staging has no free disk space.'),
             (b'connection timed out', 'The connection to GitHub timed out.')]
    hint = ('Publisher Git lock is busy; abandoned locks recover before the next retry.' if category(stderr)[0]=='git_lock' else next((message for pattern,message in hints if pattern in diagnostic), 'Upstream details were omitted to protect credentials.'))
    return f'Archive Git {operation} failed. {hint} The approved sources remain in staging.'


def git_environment():
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "EQ archives curation",
            "GIT_AUTHOR_EMAIL": "curation@eqarchives.org", "GIT_COMMITTER_NAME": "EQ archives curation",
            "GIT_COMMITTER_EMAIL": "curation@eqarchives.org"}


def repository_git(repo, *arguments, env, data=None, optional=False, local=False, descriptor=None):
    try:
        result = run_git(["git", "--git-dir", str(repo), *arguments], data=data, env=env,
                         timeout=600 if any(a in ('fetch','push','ls-remote','pack-objects') for a in arguments) else 180,
                         descriptor=descriptor)
    except subprocess.TimeoutExpired:
        raise PublicationError("Archive Git operation timed out; its child processes were stopped. Publication will retry and verify the remote marker.",'timeout') from None
    if result.returncode:
        code, retryable = category(result.stderr)
        if optional and (local or code != 'credentials'):
            return None
        raise PublicationError(git_failure(arguments, result.stderr), code, retryable)
    if b"filtering not recognized" in result.stderr or b"does not support filter" in result.stderr:
        raise CrawlError("Remote does not support bounded partial fetches")
    return result.stdout


def tree_bytes(entries):
    ordered = sorted(entries.items(), key=lambda entry: entry[0].encode("utf-8", "surrogateescape") +
                     (b"/" if entry[1][1] == "tree" else b""))
    return b"".join(f"{int(mode, 8):o} {name}".encode("utf-8", "surrogateescape") + b"\0" + bytes.fromhex(oid)
                    for name, (mode, kind, oid) in ordered)


@dataclass
class Branch:
    files: dict = field(default_factory=dict)
    directories: dict = field(default_factory=dict)
    oid: str = ''

    def stage(self, pack):
        entries = {name: ('100644', 'blob', oid) for name, oid in self.files.items()}
        for name, branch in self.directories.items():
            entries[name] = ('040000', 'tree', branch.stage(pack))
        self.oid = pack.add(tree_bytes(entries))
        return self.oid


@dataclass
class Prepared:
    workspace: tempfile.TemporaryDirectory
    manifest: dict
    expected: str
    marker: str
    changes: Branch
    exclusions: dict
    indexes: tuple

    @property
    def identifier(self):
        return self.manifest['batch_id']

    def close(self):
        self.workspace.cleanup()


def cleanup_preparations(root):
    # The controller calls this with its process-wide lease, before starting
    # preparations. Only directories made by tempfile under that root qualify.
    directory = Path(root) / PREPARATION_ROOT
    if directory.is_symlink():
        raise CrawlError('Publication preparation directory is unsafe')
    for path in directory.glob(PREPARATION + '*'):
        if re.fullmatch(PREPARATION + r'[a-z0-9_]{8}', path.name) and not path.is_symlink() and path.is_dir():
            rmtree(path)


def prepare(root, manifest, expected, progress=lambda *_: None):
    if digest(manifest) != expected:
        raise CrawlError("Batch changed since publication approval")
    if not re.fullmatch(r'[a-f0-9]{32}', manifest['batch_id']):
        raise CrawlError('Invalid publication batch identity')
    progress('verifying_sources', 0)
    check_manifest(root, manifest)
    captures, private = split_publication_files(manifest['captures'])
    if not captures:
        raise PublicationError('Every approved file exceeds the 100 MiB GitHub file limit. Original files remain in staging.',
                               'blob_limit', False)
    exclusions = {'retained_private': private} if private else {}
    marker_path = f"crawl-manifests/{manifest['batch_id']}.json"
    marker = {"schema": 1, "batch_id": manifest["batch_id"], "manifest_sha256": expected,
              "sites": [{key: site[key] for key in ("id", "url", "scope", "manifest_sha256")} for site in manifest["sites"]],
              "captures": [{key: capture[key] for key in ("url", "timestamp", "sha256", "bytes", "archive_path", "source")}
                           for capture in captures], **exclusions}
    directory = Path(root) / PREPARATION_ROOT
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise CrawlError('Publication preparation directory is unsafe')
    workspace = tempfile.TemporaryDirectory(prefix=PREPARATION, dir=directory)
    try:
        repo = Path(workspace.name) / 'source.git'
        repo.mkdir(mode=0o700)
        git = partial(repository_git, repo, env=git_environment())
        git('init', '--bare', '--template=', '--object-format=sha1', '-q')
        changes = hash_sources(root, captures, git, progress)
        changes[marker_path] = git('hash-object', '-w', '--stdin',
            data=(json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()).decode().strip()
        progress('packing_sources', len(captures))
        objects = b''.join(oid.encode() + b'\n' for oid in sorted(set(changes.values())))
        git('pack-objects', '--window=0', '--threads=1', '--max-pack-size=128m',
            str(repo / 'objects/pack/pack'), data=objects)
        progress('building_trees', len(captures))
        tree = Branch()
        for path, oid in changes.items():
            *parents, name = path.split('/')
            branch = tree
            for component in parents:
                if component not in branch.directories:
                    branch.directories[component] = Branch()
                branch = branch.directories[component]
            branch.files[name] = oid
        with TreePack(lambda data: git('index-pack', '--stdin', data=data)) as pack:
            tree.stage(pack)
        indexes = tuple(sorted((repo / 'objects/pack').glob('*.idx')))
        if not indexes or any(not PACK_NAME.fullmatch(path.name) or not path.with_suffix('.pack').is_file()
                              for path in indexes):
            raise CrawlError('Prepared Git packs are incomplete')
        for index in indexes:
            for path in (index.with_suffix('.pack'), index):
                path.rename(Path(workspace.name) / path.name)
        # Dispose of the temporary loose objects on this preparation lane.
        # The push worker will only have a few pack files to release.
        rmtree(repo)
        return Prepared(workspace, manifest, expected, marker_path, tree, exclusions,
                        tuple(Path(workspace.name) / index.name for index in indexes))
    except BaseException:
        workspace.cleanup()
        raise


class Publisher:
    def __init__(self, root, remote=REMOTE, key_file="/run/secrets/archive_publish_key"):
        self.root = Path(root)
        self.repo = self.root / "publisher.git"
        self.reader = self.root / "publisher-reader.git"
        self.tree_cache = {}
        self.env = git_environment()
        self.private = None
        self.lease=lease(self.root)
        try:
            self.recovered_locks=recover(self.root,self.repo)
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
                self.git("init", "--bare", "--template=", "--object-format=sha1", "-q")
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
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.private:self.private.cleanup()
        finally:
            if getattr(self,'lease',None):self.lease.close()

    def git(self, *arguments, data=None, optional=False, local=False):
        env = {**self.env, "GIT_OBJECT_DIRECTORY": str(self.repo / "objects")} if local else self.env
        return repository_git(self.reader if local else self.repo, *arguments, env=env, data=data,
                              optional=optional, local=local,
                              descriptor=self.lease.fileno() if getattr(self, 'lease', None) else None)

    def fetch(self):
        self.git("-c", "fetch.negotiationAlgorithm=noop", "fetch", "--refetch", "--no-tags", "--depth=1", "--filter=tree:0", "origin", "master")
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
            return self.tree_cache[tree]
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
        return entries

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

    def overlay(self, tree, change, pack):
        if tree is None or tree == change.oid:
            return change.oid
        original = self.entries(tree)
        entries = original.copy()
        for name, oid in change.files.items():
            old = entries.get(name)
            if old and (old[0] not in ('100644', '100755') or old[1:] != ('blob', oid)):
                raise CrawlError("Archive destination already contains different data; publication paused")
            entries[name] = old or ("100644", "blob", oid)
        for name, branch in change.directories.items():
            old = entries.get(name)
            if old and old[1] != "tree":
                raise CrawlError("Archive destination collides with an existing file")
            subtree = self.overlay(old[2] if old else None, branch, pack)
            entries[name] = ("040000", "tree", subtree)
        return tree if entries == original else pack.add(tree_bytes(entries))

    def verify_tree(self, tree, change):
        if tree == change.oid:
            return
        entries = self.entries(tree)
        for name, oid in change.files.items():
            if entries.get(name, (None, None, None))[1:] != ('blob', oid):
                raise CrawlError("Published source changed after its marker; indexing paused")
        for name, branch in change.directories.items():
            entry = entries.get(name)
            if not entry or entry[1] != 'tree':
                raise CrawlError("Published source changed after its marker; indexing paused")
            self.verify_tree(entry[2], branch)

    def install(self, prepared):
        directory = self.repo / 'objects/pack'
        # Git discovers a pack through its index. Publish the completed pack
        # first and link its index last, under the exclusive publisher lease.
        for index in prepared.indexes:
            for path in (index.with_suffix('.pack'), index):
                try:
                    os.link(path, directory / path.name)
                except FileExistsError:
                    pass

    def hash_sources(self, captures):
        return hash_sources(self.root, captures, self.git)

    def publish(self, manifest, expected):
        prepared = prepare(self.root, manifest, expected)
        try:
            result = self.publish_many([prepared])[prepared.identifier]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            prepared.close()

    def publish_many(self, sites, progress=lambda *_: None):
        """Build one commit per site and send one fast-forward ref update."""
        outcomes = {}
        sent_parent, sent_commits = None, {}
        for site in sites:
            self.install(site)
        for _ in range(3):
            self.tree_cache.clear()
            for site in sites:
                if site.identifier not in outcomes:
                    progress(site, 'checking_head')
            base = parent = self.fetch()
            base_tree = tree = self.object("commit", base).split(b"\n", 1)[0].removeprefix(b"tree ").decode()
            commits = {}
            for site in sites:
                if site.identifier in outcomes:
                    continue
                try:
                    marker_entry = self.lookup(base_tree, site.marker)
                    if marker_entry:
                        if marker_entry[1] != 'blob':
                            raise CrawlError("Remote publication marker is not a file")
                        marker = json.loads(self.object("blob", marker_entry[2]))
                        if (marker.get('manifest_sha256') != site.expected or
                                marker.get('retained_private', []) != site.exclusions.get('retained_private', [])):
                            raise CrawlError("Remote publication marker belongs to a different approved manifest")
                        # A recovered receipt proves the exact marker and every
                        # prepared path in the remote base, in one tree walk.
                        self.verify_tree(base_tree, site.changes)
                        commit = sent_commits.get(site.identifier, base) if base == sent_parent else base
                        outcomes[site.identifier] = self.receipt(site, commit, recovered=True)
                        continue
                    progress(site, 'merging_trees')
                    with TreePack(lambda data: self.git('index-pack', '--stdin', data=data)) as pack:
                        updated = self.overlay(tree, site.changes, pack)
                    commit = self.git('commit-tree', updated, '-p', parent,
                                      data=f"Archive approved capture batch {site.identifier}\n\nManifest SHA256: {site.expected}\n".encode()).decode().strip()
                    tree, parent = updated, commit
                    commits[site.identifier] = commit
                except PublicationError:
                    raise
                except CrawlError as error:
                    outcomes[site.identifier] = error
            if not commits:
                return outcomes
            for site in sites:
                if site.identifier in commits:
                    progress(site, 'pushing')
            # The next attempt fetches a new base and checks every marker
            # before rebuilding the remaining commit chain.
            if self.git('push', 'origin', parent + ':refs/heads/master', optional=True) is not None:
                for site in sites:
                    if site.identifier in commits:
                        outcomes[site.identifier] = self.receipt(site, commits[site.identifier], recovered=False)
                return outcomes
            sent_parent, sent_commits = parent, commits
        raise PublicationError("Archive publication could not be confirmed; a retry will check the remote marker first.",'uncertain')

    def receipt(self, site, commit, *, recovered):
        return {'commit': commit, 'marker': site.marker, 'recovered': recovered, **site.exclusions}


def hash_sources(root, captures, git, progress=lambda *_: None):
    changes = {}
    for offset in range(0, len(captures), 512):
        batch = captures[offset:offset + 512]
        lines = []
        expected = []
        for capture in batch:
            blob = hashlib.sha1(b'blob %d\0' % capture['bytes'])
            path = os.fsencode(verified_path(root, capture, observe=blob.update))
            expected.append(blob.hexdigest().encode())
            # Quote bytes in Git's C syntax, including control characters.
            quoted = b''.join(b'\\' + bytes([value]) if value in (34, 92)
                              else f'\\{value:03o}'.encode() if value < 32 or value == 127
                              else bytes([value]) for value in path)
            lines.append(b'"' + quoted + b'"\n')
        output = git('hash-object', '-w', '--stdin-paths', '--no-filters', data=b''.join(lines))
        objects = output.splitlines()
        if objects != expected:
            raise CrawlError('Archive blob batch was incomplete or invalid; publication paused')
        changes.update((archive_path(capture), oid.decode('ascii'))
                       for capture, oid in zip(batch, objects, strict=True))
        progress('hashing_sources', offset + len(batch))
    return changes


def publish(root, sites, progress=lambda *_: None):
    publisher = Publisher(root)
    try:
        return {identifier: {**result, 'recovered_git_locks': publisher.recovered_locks}
                if isinstance(result, dict) else result
                for identifier, result in publisher.publish_many(sites, progress).items()}
    finally:
        publisher.close()
