"""Bounded spawned preparation processes with per-record crash identity."""
from dataclasses import dataclass
import multiprocessing
import time

from indexer import gemma
from indexer.source_files import Sources, SourceError
from policy_preparation import prepare


class WorkerLost(Exception):
    pass


def worker(connection,roots,publication,tasks):
    # Spawn, not fork: no parent's HTTP connections, SQLite handles or leases.
    sources=Sources(roots,publication)
    try:
        gemma.load_tokenizer()
        for _ in range(tasks):
            hit=connection.recv()
            if hit is None:return
            try:result=('ready',prepare(hit,sources))
            except SourceError as error:result=('source',error.reason,error.source_sha256)
            connection.send(result)
    except (EOFError,BrokenPipeError):pass
    finally:
        sources.close();connection.close()


@dataclass
class Slot:
    process:object
    connection:object
    busy:bool=False
    completed:int=0


class Pending:
    def __init__(self,pool,slot):
        self.pool,self.slot=pool,slot
        self.started=time.monotonic()

    def result(self,check):
        slot=self.slot
        received=False
        try:
            while True:
                check()
                if time.monotonic()-self.started>self.pool.timeout:
                    raise SourceError('preparation_timeout')
                if slot.connection.poll(.05):
                    response=slot.connection.recv();received=True;break
                if not slot.process.is_alive():raise WorkerLost()
            if response[0]=='source':raise SourceError(response[1],response[2])
            if response[0]!='ready':raise WorkerLost()
            return response[1]
        except (EOFError,BrokenPipeError,OSError):raise WorkerLost() from None
        finally:
            if not received:self.pool.discard(slot)
            else:slot.busy=False;slot.completed+=1

    def cancel(self):
        if self.slot.busy:self.pool.discard(self.slot)


class PreparationPool:
    def __init__(self,roots,publication,workers=2,*,tasks=128,timeout=120):
        if (type(workers) is not int or not 1<=workers<=4 or type(tasks) is not int or not 1<=tasks<=256
                or type(timeout) not in (int,float) or not 0<timeout<=300):
            raise ValueError('Invalid preparation pool bounds')
        self.roots,self.publication=tuple(map(str,roots)),str(publication) if publication is not None else None
        self.workers,self.tasks,self.timeout=workers,tasks,timeout
        self.context=multiprocessing.get_context('spawn');self.slots=[]

    def submit(self,hit):
        slot=next((s for s in self.slots if not s.busy),None)
        if slot and (slot.completed>=self.tasks or not slot.process.is_alive()):
            self.retire(slot);self.slots.remove(slot);slot=None
        if slot is None:
            if len(self.slots)>=self.workers:raise ValueError('Preparation queue is full')
            parent,child=self.context.Pipe()
            process=self.context.Process(target=worker,args=(child,self.roots,self.publication,self.tasks),
                                         name='policy-preparation',daemon=True)
            try:process.start()
            except BaseException:parent.close();child.close();raise
            child.close();slot=Slot(process,parent);self.slots.append(slot)
        try:slot.connection.send(hit)
        except (BrokenPipeError,EOFError,OSError):
            self.discard(slot);raise WorkerLost() from None
        slot.busy=True
        return Pending(self,slot)

    def discard(self,slot):
        if slot in self.slots:self.slots.remove(slot);self.retire(slot)

    def cancel_pending(self):
        for slot in self.slots.copy():
            if slot.busy:self.discard(slot)

    @staticmethod
    def retire(slot):
        slot.connection.close()
        if slot.process.is_alive():slot.process.terminate()
        slot.process.join(timeout=2)
        if slot.process.is_alive():slot.process.kill();slot.process.join(timeout=2)
        slot.process.close()

    def close(self):
        # Pending work has no side effects. Terminate/reap promptly on a yield;
        # foreground imports must not wait for speculative HTML preparation.
        for slot in self.slots:self.retire(slot)
        self.slots.clear()
