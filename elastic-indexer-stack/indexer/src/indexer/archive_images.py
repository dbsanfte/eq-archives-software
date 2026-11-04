"""Streaming, remote-free raster inventory and durable per-host cursors."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess

RASTER=re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|tiff?)(?:\?|$)",re.I)


class Inventory:
    def __init__(self,repo,state,*,revision=None):
        self.repo=Path(repo).resolve();self.state=Path(state)
        self.state.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=self.state.with_suffix('.lock').open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('An image worker already owns this inventory') from None
        try:
            self._initialize(revision)
        except BaseException:
            if getattr(self,'db',None):self.db.rollback()
            self.close()
            raise

    def _initialize(self,revision):
        self.db=sqlite3.connect(self.state)
        self.cache_writes=0
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS hosts(name TEXT PRIMARY KEY,tree TEXT,ordinal INTEGER DEFAULT 0,complete INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS gaps(id TEXT PRIMARY KEY,blob TEXT,reason TEXT);
            CREATE TABLE IF NOT EXISTS cache(blob TEXT PRIMARY KEY,version TEXT,value TEXT,used INTEGER);''')
        self.revision=revision or subprocess.check_output(['git','-c',f'safe.directory={self.repo}','-C',str(self.repo),'rev-parse','HEAD'],text=True).strip()
        if not re.fullmatch('[a-f0-9]{40}',self.revision):
            raise ValueError('Image inventory requires an exact archive revision')
        previous=self.db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
        if previous and previous[0]!=self.revision:
            raise ValueError('Image inventory revision changed; preserve its source snapshot')
        common=subprocess.check_output(['git','-c',f'safe.directory={self.repo}','-C',str(self.repo),'rev-parse','--git-common-dir'],text=True).strip()
        objects=(self.repo/common/'objects').resolve()
        self.reader=self.state.parent/'image-object-reader.git'
        if not self.reader.exists():
            subprocess.run(['git','init','--bare','--template=','-q',str(self.reader)],check=True)
        self.env={**os.environ,'GIT_OBJECT_DIRECTORY':str(objects),'GIT_NO_LAZY_FETCH':'1',
                  'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_SYSTEM':os.devnull}
        if self.git('remote').strip():
            raise ValueError('Image object reader must have no remotes')
        if not previous:
            for metadata,name in self.tree(self.revision+':websites',recursive=False):
                mode,kind,oid=metadata.split()
                if kind=='tree' and not name.lower().startswith(('ad.','ads.')):
                    self.db.execute('INSERT INTO hosts(name,tree) VALUES (?,?)',(name,oid))
            self.db.execute("INSERT INTO metadata VALUES ('revision',?)",(self.revision,));self.db.commit()

    def git(self,*args):
        result=subprocess.run(['git','--git-dir',str(self.reader),*args],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        if result.returncode:raise ValueError('Archive object unavailable locally; no fetch attempted')
        return result.stdout

    def tree(self,tree,*,recursive=True,max_bytes=512*1024*1024):
        command=['git','--git-dir',str(self.reader),'ls-tree','-rz' if recursive else '-z',tree]
        process=subprocess.Popen(command,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        buffer=b'';consumed=0
        selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
        try:
            while True:
                if not selector.select(timeout=30):raise ValueError('Archive metadata read stalled')
                block=os.read(process.stdout.fileno(),65536)
                if not block:break
                consumed+=len(block)
                if consumed>max_bytes:raise ValueError('Archive host metadata bound reached; inventory remains incomplete')
                buffer+=block
                while b'\0' in buffer:
                    row,buffer=buffer.split(b'\0',1)
                    metadata,name=row.split(b'\t',1)
                    yield metadata.decode('ascii'),name.decode('utf-8','surrogateescape')
            if buffer or process.wait(timeout=10):raise ValueError('Incomplete local archive tree')
        finally:
            selector.close();process.stdout.close()
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()

    def pending_hosts(self,hosts=None):
        selected=set(hosts or [])
        for name,tree,ordinal in self.db.execute('SELECT name,tree,ordinal FROM hosts WHERE complete=0 ORDER BY name').fetchall():
            if not selected or name in selected:yield name,tree,ordinal

    def batches(self,host,tree,after,size=50):
        batch=[];last=after
        for ordinal,(metadata,name) in enumerate(self.tree(tree),1):
            last=ordinal
            if ordinal<=after:continue
            mode,kind,oid=metadata.split()
            if kind=='blob' and mode in ('100644','100755') and RASTER.search(name):
                if any(ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF for c in name):
                    key='unsafe-image-path:'+hashlib.sha256(name.encode('utf-8','surrogateescape')).hexdigest()
                    self.gap(key,oid,'unsafe_archive_path')
                    continue
                if any(part in ('..','') for part in name.split('/')) or name.startswith('/'):
                    raise ValueError('Unsafe archive image path')
                batch.append((ordinal,f'websites/{host}/{name}',oid))
                if len(batch)>=size:
                    yield batch,ordinal,False;batch=[]
        yield batch,last,True

    def checkpoint(self,host,ordinal,complete):
        self.db.execute('UPDATE hosts SET ordinal=?,complete=? WHERE name=?',(ordinal,int(complete),host));self.db.commit()

    def gap(self,record_id,blob,reason):
        self.db.execute('INSERT OR REPLACE INTO gaps VALUES (?,?,?)',(record_id,blob,reason))

    def clear_gap(self,record_id):
        self.db.execute('DELETE FROM gaps WHERE id=?',(record_id,))

    def retry_batches(self,size=50,hosts=None):
        after='';selected=set(hosts or [])
        while True:
            rows=self.db.execute("SELECT id,blob FROM gaps WHERE reason='source_unavailable' AND id>? ORDER BY id LIMIT ?",(after,size)).fetchall()
            if not rows:return
            after=rows[-1][0]
            batch=[(i,key,blob) for i,(key,blob) in enumerate(rows,1) if not selected or key.split('/')[1] in selected]
            if batch:yield batch,0,False

    def missing_sources(self):
        return self.db.execute("SELECT COUNT(*) FROM gaps WHERE reason='source_unavailable'").fetchone()[0]

    def cached(self,blob,version):
        row=self.db.execute('SELECT value FROM cache WHERE blob=? AND version=?',(blob,version)).fetchone()
        if row:self.db.execute('UPDATE cache SET used=? WHERE blob=?',(__import__('time').time_ns(),blob))
        return json.loads(row[0]) if row else None

    def cache(self,blob,version,value):
        self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)',(blob,version,json.dumps(value),__import__('time').time_ns()))
        self.cache_writes+=1
        if self.cache_writes%512==0:
            self.db.execute('DELETE FROM cache WHERE blob IN (SELECT blob FROM cache ORDER BY used DESC LIMIT -1 OFFSET 10000)')

    def close(self):
        if getattr(self,'db',None):
            self.db.commit();self.db.close();self.db=None
        if getattr(self,'lock',None) and not self.lock.closed:
            self.lock.close()
