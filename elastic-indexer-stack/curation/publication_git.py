"""Exclusive publisher ownership and bounded Git process/lock recovery."""
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import uuid

from common import CrawlError


class PublicationError(CrawlError):
    def __init__(self,message,code='transport',retryable=True):
        self.code,self.retryable=code,retryable
        super().__init__(message)


def lease(root):
    fd=os.open(Path(root)/'publisher.lease',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    handle=os.fdopen(fd,'a')
    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise PublicationError('Another publisher owns the Git repository; publication will retry.','busy') from None
    return handle


def recover(root,repo):
    """Caller holds the lease. Preserve fixed, abandoned locks; never walk Git."""
    repo=Path(repo)
    if repo.is_symlink():raise PublicationError('Publisher repository identity is unsafe.','integrity',False)
    # Also protect legacy/out-of-band Git processes that do not know our lease.
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():continue
        try:
            args=(process/'cmdline').read_bytes().split(b'\0')
            if not args or not Path(os.fsdecode(args[0])).name.startswith('git'):continue
            locations=[os.fsencode(repo),os.fsencode(repo.parent/'publisher-reader.git')]
            env=dict(part.split(b'=',1) for part in (process/'environ').read_bytes().split(b'\0') if b'=' in part)
            working=(process/'cwd').resolve()
            if (any(value in args or b'--git-dir='+value in args or env.get(b'GIT_DIR')==value for value in locations)
                    or working==repo.resolve() or env.get(b'GIT_OBJECT_DIRECTORY')==os.fsencode(repo/'objects')):
                raise PublicationError('A Git process still owns publication work; locks were retained.','busy')
        except (FileNotFoundError,ProcessLookupError,PermissionError):continue
    moved=[]
    for name in ('shallow.lock','FETCH_HEAD.lock','config.lock','packed-refs.lock','refs/remotes/origin/master.lock'):
        path=repo/name
        try:info=path.lstat()
        except FileNotFoundError:continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or not path.parent.resolve().is_relative_to(repo.resolve())):
            raise PublicationError('Publisher Git lock ownership is unsafe; operator attention is required.','integrity',False)
        destination=Path(root)/'publisher-recovery'
        destination.mkdir(mode=0o700,exist_ok=True)
        if destination.is_symlink():raise PublicationError('Publisher recovery directory is unsafe.','integrity',False)
        os.rename(path,destination/(uuid.uuid4().hex+'-'+name.replace('/','_')))
        moved.append(name)
    return moved


def run(command,*,env,data=None,timeout=180,descriptor=None):
    """Reap Git and its SSH/pack children before releasing publisher ownership."""
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             env=env,start_new_session=True,pass_fds=(() if descriptor is None else (descriptor,)))
    try:
        stdout,stderr=process.communicate(data,timeout=timeout)
        return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
    except BaseException:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.communicate()
        raise


def category(stderr):
    lower=stderr.lower()
    if any(value in lower for value in (b'permission denied',b'host key verification failed',b'repository not found',b'read-only')):
        return 'credentials',False
    if b'.lock' in lower and b'file exists' in lower:return 'git_lock',True
    if any(value in lower for value in (b'could not resolve',b'timed out',b'connection reset',b'connection closed',b'remote end hung up',b'early eof',b'broken pipe',b'network is unreachable',b'temporary failure',b'ssh_exchange_identification',b'kex_exchange_identification')):
        return 'transport',True
    if b'no space left on device' in lower:return 'storage',True
    return 'git_failure',False
