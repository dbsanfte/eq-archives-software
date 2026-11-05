"""Private SQLite state shared by indexing processes, never archive sources."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
import time


class SQLiteState:
    def __init__(self,path,*,max_bytes=None,durable=False):
        self.path=Path(path)
        if not self.path.is_absolute():raise ValueError('Indexing state requires an absolute private path')
        self.max_bytes=max_bytes;self.durable=durable;self.local=threading.local()

    def connection(self):
        if getattr(self.local,'db',None) is not None:return self.local.db
        self.path.parent.mkdir(mode=0o770,parents=True,exist_ok=True)
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o660)
        except FileExistsError:
            if self.path.is_symlink():raise ValueError('Indexing state cannot be a symbolic link')
        else:
            try:os.fchmod(fd,0o660)
            finally:os.close(fd)
        db=sqlite3.connect(self.path,timeout=.25,isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=250')
            if not db.execute('PRAGMA page_count').fetchone()[0]:db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous='+('FULL' if self.durable else 'NORMAL'))
            db.execute('PRAGMA wal_autocheckpoint=128');db.execute('PRAGMA journal_size_limit=1048576')
            if self.max_bytes:
                page=db.execute('PRAGMA page_size').fetchone()[0]
                actual=db.execute('PRAGMA max_page_count='+str(self.max_bytes//page)).fetchone()[0]
                if actual*page>self.max_bytes:raise ValueError('Existing cache exceeds the configured file bound')
        except BaseException:
            db.close();raise
        self.local.db=db
        return db

    @contextmanager
    def transaction(self,*,attempts=1):
        db=self.connection()
        for attempt in range(attempts):
            try:db.execute('BEGIN IMMEDIATE');break
            except sqlite3.OperationalError as error:
                if not locked(error) or attempt+1==attempts:raise
                time.sleep(.05*(attempt+1))
        try:
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:db.execute('ROLLBACK')
            raise

    def close(self):
        db=getattr(self.local,'db',None)
        if db is not None:db.close();self.local.db=None


def locked(error):
    return getattr(error,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)
