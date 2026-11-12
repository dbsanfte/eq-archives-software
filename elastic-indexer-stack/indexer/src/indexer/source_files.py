"""Exact, bounded local sources for policy repair; no fetch, clone or traversal."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

LIMIT=8*1024*1024


class SourceError(ValueError):
    def __init__(self, reason, source_sha256=None):
        self.reason,self.source_sha256=reason,source_sha256
        super().__init__(reason)


def relative(identifier):
    if (not isinstance(identifier,str) or '\0' in identifier or len(identifier)>16384
            or identifier.startswith('/') or any(p in ('','.','..') for p in identifier.split('/'))
            or identifier.split('/')[0] not in ('websites','newsgroups','mailing-lists')):
        raise SourceError('source_identity')
    return Path(identifier)


class Sources:
    def __init__(self, roots, publication=None):
        self.roots=[Path(p).resolve() for p in roots]
        self.publication=Path(publication).resolve() if publication is not None else None
        self.temporary=None;self.environment=None;self.reader=None;self.commits=None

    def __enter__(self):return self

    def __exit__(self,*_):self.close()

    def close(self):
        if self.temporary:self.temporary.cleanup()

    @staticmethod
    def checked(data, expected):
        if len(data)>LIMIT:raise SourceError('source_too_large')
        if expected and hashlib.sha256(data).hexdigest()!=expected:
            raise SourceError('source_integrity',hashlib.sha256(data).hexdigest())
        return data

    def read(self, source):
        path=relative(source['id'])
        previous=(source.get('text_passage_provenance') or {}).get('decoding') or {}
        hashes=[v for v in (source.get('archive_source_sha256'),previous.get('source_sha256')) if v]
        if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in hashes) or len(set(hashes))>1:
            raise SourceError('source_integrity')
        expected=hashes[0] if hashes else None
        for root in self.roots:
            original=root/path
            try:resolved=original.resolve()
            except (OSError,RuntimeError):continue
            if not resolved.is_relative_to(root) or original.is_symlink():raise SourceError('source_identity')
            try:
                if not resolved.is_file():continue
                with resolved.open('rb') as stream:data=stream.read(LIMIT+1)
                return self.checked(data,expected)
            except OSError:continue
        self.open_git()
        commit=source.get('archive_commit')
        commits=[commit] if isinstance(commit,str) and re.fullmatch('[0-9a-f]{40}',commit) else self.commits
        for commit in commits:
            row=self.git('ls-tree','-z',commit,'--',source['id'],optional=True)
            if not row:continue
            records=row.rstrip(b'\0').split(b'\0')
            if len(records)!=1:raise SourceError('source_identity')
            metadata,name=records[0].split(b'\t',1);mode,kind,oid=metadata.split()
            if mode not in (b'100644',b'100755') or kind!=b'blob' or name.decode()!=source['id']:
                raise SourceError('source_identity')
            size=int(self.git('cat-file','-s',oid.decode()))
            if size>LIMIT:raise SourceError('source_too_large')
            data=self.git('cat-file','blob',oid.decode())
            if len(data)!=size or hashlib.sha1(b'blob '+str(size).encode()+b'\0'+data).hexdigest()!=oid.decode():
                raise SourceError('source_integrity')
            return self.checked(data,expected)
        raise SourceError('source_unavailable')

    def open_git(self):
        if self.reader:return
        environment={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull,
                           GIT_NO_LAZY_FETCH='1',GIT_TERMINAL_PROMPT='0',GIT_NO_REPLACE_OBJECTS='1',
                           GIT_LITERAL_PATHSPECS='1')
        directories=[p for p in [*(root/'.git/objects' for root in self.roots),*([self.publication/'objects'] if self.publication else [])] if p.is_dir()]
        if not directories:raise SourceError('source_unavailable')
        self.temporary=tempfile.TemporaryDirectory(prefix='policy-source-')
        self.reader=Path(self.temporary.name)/'reader.git'
        try:
            subprocess.run(['git','init','--bare','--template=','-q',str(self.reader)],check=True,
                           env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            self.commits=[]
            for root in self.roots:
                result=subprocess.run(['git','-c',f'safe.directory={root}','-C',str(root),'rev-parse','--verify','HEAD'],
                                      env=environment,capture_output=True,timeout=10)
                value=result.stdout.decode().strip()
                if result.returncode==0 and re.fullmatch('[0-9a-f]{40}',value) and value not in self.commits:
                    self.commits.append(value)
        except (OSError,subprocess.SubprocessError):raise SourceError('source_unavailable') from None
        self.environment={**environment,'GIT_OBJECT_DIRECTORY':str(directories[0]),
                          'GIT_ALTERNATE_OBJECT_DIRECTORIES':os.pathsep.join(map(str,directories[1:]))}
        if self.git('remote').strip():raise SourceError('source_identity')

    def git(self,*arguments,optional=False):
        try:
            result=subprocess.run(['git','--git-dir',str(self.reader),*arguments],env=self.environment,
                                  capture_output=True,timeout=10)
            if result.returncode:
                if optional:return b''
                raise SourceError('source_unavailable')
            return result.stdout
        except (OSError,subprocess.SubprocessError):raise SourceError('source_unavailable') from None
