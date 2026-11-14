"""Independent bounded batching workers for embeddings and reranking."""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time
import threading


class Busy(Exception):
    pass


@dataclass
class Item:
    tokens: list
    future: asyncio.Future
    deadline: float
    group: "RequestGroup"


@dataclass(eq=False)
class RequestGroup:
    created: float
    progressed: float
    remaining: int = 0
    requests: int = 0


class BatchBudget:
    """Bound actual dense work and attention space, including exact causal reuse."""

    def __init__(self, causal, tokens=4096, attention=4 * 1024 * 1024):
        self.causal, self.tokens, self.attention = causal, tokens, attention
        self.rows, self.blocks = set(), {}
        self.dense, self.longest, self.shortest = 0, 0, 0

    def add(self, row):
        key = tuple(row)
        if key in self.rows:
            return True
        padded = (len(row) + 31) // 32 * 32
        longest = max(self.longest, padded)
        shortest = min(self.shortest or padded, padded)
        if longest > 2 * shortest:
            return False
        if (len(self.rows) + 1) * longest * longest > self.attention:
            return False
        pending = {}
        if self.causal:
            parent = -1
            for start in range(0, len(row), 32):
                block = (parent, tuple(row[start : start + 32]))
                offset = self.blocks.get(block, pending.get(block))
                if offset is None:
                    offset = self.dense + 32 * len(pending)
                    pending[block] = offset
                parent = offset
            cost = 32 * len(pending)
        else:
            cost = padded
        if self.dense + cost > self.tokens:
            return False
        self.rows.add(key)
        self.blocks.update(pending)
        self.longest, self.dense = longest, self.dense + cost
        self.shortest = shortest
        return True


class Scheduler:
    def __init__(self, engine, batch=32, capacity=1024, wait=0.001, interactive_burst=0.5,
                 rerank_fair_wait=1.0):
        self.engine, self.batch, self.capacity, self.wait = engine, batch, capacity, wait
        self.interactive_burst = interactive_burst
        self.rerank_fair_wait = rerank_fair_wait
        self.groups = {}
        self.changed = asyncio.Event()
        self.queues = {"embed": deque(), "rerank": deque()}
        self.wake = {kind: asyncio.Event() for kind in self.queues}
        self.workers = {
            kind: ThreadPoolExecutor(max_workers=1, thread_name_prefix="cuda-" + kind) for kind in self.queues
        }
        self.tasks = []
        self.closed = False
        self.failed = False
        self.running_since = {kind: None for kind in self.queues}
        self.counters = {"batches": 0, "items": 0, "expired": 0, "errors": 0}

    def start(self):
        self.tasks = [asyncio.create_task(self._run(kind)) for kind in self.queues]

    async def close(self):
        self.closed = True
        self.changed.set()
        for wake in self.wake.values():
            wake.set()
        await asyncio.gather(*self.tasks)
        for worker in self.workers.values():
            worker.shutdown(wait=True, cancel_futures=True)

    async def submit(self, kind, rows, deadline, group=None):
        if deadline <= time.monotonic():
            raise asyncio.TimeoutError()
        if kind not in self.queues or not rows:
            raise ValueError("Unknown worker or empty batch")
        # Each model owns its capacity: imports cannot fill the reranking queue.
        if self.closed or self.failed or len(self.queues[kind]) + len(rows) > self.capacity:
            raise Busy("GPU queue is full or unavailable")
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in rows]
        key = (kind, object() if group is None else group)
        now = time.monotonic()
        owner = self.groups.setdefault(key, RequestGroup(now, now))
        owner.requests += 1
        owner.remaining += sum((len(row) + 31) // 32 * 32 for row in rows)
        self.queues[kind].extend(Item(row, f, deadline, owner) for row, f in zip(rows, futures))
        self.wake[kind].set()
        self.changed.set()
        joined = asyncio.gather(*futures)
        try:
            return await asyncio.wait_for(joined, max(0, deadline - time.monotonic()))
        finally:
            for f in futures:
                if not f.done():
                    f.cancel()
            # Release bookkeeping before the next await: a disconnect followed
            # by the lifecycle timeout can cancel this drain a second time.
            owner.requests -= 1
            if owner.requests == 0:
                self.groups.pop(key, None)
            await asyncio.gather(joined, return_exceptions=True)

    def _take_batch(self, kind):
        queue = self.queues[kind]
        pending, now = [], time.monotonic()
        while queue:
            item = queue.popleft()
            if item.future.done() or item.deadline <= now:
                item.group.remaining -= (len(item.tokens) + 31) // 32 * 32
                if not item.future.done():
                    item.future.set_exception(asyncio.TimeoutError())
                    self.counters["expired"] += 1
            else:
                pending.append(item)
        primary = None
        if kind == "rerank" and pending:
            # Prefer finishing an entire search over partially serving many.
            # An aged group receives a GPU quantum even under fresh short work.
            def priority(item):
                owner = item.group
                aged = now - owner.progressed >= self.rerank_fair_wait
                return (0 if aged else 1, owner.progressed if aged else owner.remaining, owner.created)

            pending.sort(key=priority)  # Stable within each group: retain source order.
            primary = pending[0].group
        batch, budget = [], BatchBudget(causal=kind == "rerank")
        limited = False
        for item in pending:
            if primary is not None and item.group is not primary and not limited:
                # Spare capacity is not free latency. Allow only a small amount
                # of extra work to piggyback on the group we are trying to finish.
                budget.tokens = min(budget.tokens, max(128, (budget.dense * 11 // 10 + 31) // 32 * 32))
                budget.attention = min(budget.attention,
                                       max(128 * 128, len(budget.rows) * budget.longest ** 2 * 11 // 10))
                limited = True
            if len(batch) < self.batch and budget.add(item.tokens):
                batch.append(item)
                item.group.remaining -= (len(item.tokens) + 31) // 32 * 32
            else:
                queue.append(item)
        return batch

    async def _embedding_turn(self):
        # Give interactive searches a short GPU burst before another bulk batch.
        # An in-flight embedding forward still overlaps on its independent stream.
        # The finite burst also lets embeddings progress during continuous search;
        # there is no delay at all when reranking is idle.
        until = time.monotonic() + self.interactive_burst
        while (self.queues['rerank'] or self.running_since['rerank'] is not None):
            if self.closed or self.failed:
                return
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                return

    async def _run(self, kind):
        queue, wake = self.queues[kind], self.wake[kind]
        while not self.closed and not self.failed:
            if not queue:
                wake.clear()
                await wake.wait()
                if self.closed or self.failed:
                    break
                # Embeddings batch work already queued on this event-loop turn,
                # without imposing a timer on an otherwise idle single query.
                # Reranking retains its short window for related HTTP batches.
                await asyncio.sleep(0 if kind == "embed" else self.wait)
            if kind == 'embed':
                await self._embedding_turn()
                if self.closed or self.failed:
                    break
            batch = self._take_batch(kind)
            if not batch:
                continue
            try:
                self.running_since[kind] = time.monotonic()
                values = await asyncio.get_running_loop().run_in_executor(
                    self.workers[kind], self.engine.run, kind, [item.tokens for item in batch]
                )
                if self.failed:
                    raise RuntimeError("Shared CUDA context is unavailable")
                if len(values) != len(batch):
                    raise RuntimeError("Incomplete GPU batch")
                self.counters["batches"] += 1
                self.counters["items"] += len(batch)
                completed = time.monotonic()
                for item, value in zip(batch, values):
                    item.group.progressed = completed
                    if not item.future.done():
                        item.future.set_result(value)
            except Exception:
                # CUDA faults can poison the context. Fail closed, report unhealthy
                # and let the service manager restart; never fabricate zero scores.
                self.failed = True
                for event in self.wake.values():
                    event.set()
                self.counters["errors"] += 1
                for item in batch:
                    if not item.future.done():
                        item.future.set_exception(RuntimeError("GPU inference unavailable"))
                break
            finally:
                self.running_since[kind] = None
                self.changed.set()
        while queue:
            item = queue.popleft()
            if not item.future.done():
                item.future.set_exception(Busy("GPU worker stopped"))

    def healthy(self):
        return (
            not self.closed
            and not self.failed
            and all(start is None or time.monotonic() - start < 12 for start in self.running_since.values())
        )


def unique_batch(model, rows, **kwargs):
    """Share exact complete inputs inside this batch; preserve all caller slots."""
    unique, positions, indices = [], {}, []
    for row in rows:
        key = tuple(row)
        if key not in positions:
            positions[key] = len(unique)
            unique.append(row)
        indices.append(positions[key])
    values = model.run(unique, **kwargs)
    if len(values) != len(unique):
        raise RuntimeError("Incomplete GPU batch")
    return [values[i] for i in indices]


class Engine:
    def __init__(self, root, library=None):
        import torch
        from backend import Model

        torch.set_num_threads(2)
        self.torch = torch
        self.models = {kind: Model(kind, root, library) for kind in ("embed", "rerank")}
        # Publish loaded weights before either non-default stream can read them.
        torch.cuda.synchronize()
        self.streams = {kind: torch.cuda.Stream() for kind in self.models}
        self.lock, self.active = threading.Lock(), set()
        self.shared_blocks = torch.cuda.get_device_properties(0).multi_processor_count
        from backend import PREFIX, SUFFIX

        probes = {
            "embed": "task: search result | query: EverQuest",
            "rerank": PREFIX + "EverQuest\n<Document>: EverQuest is an online game." + SUFFIX,
        }
        for kind, value in probes.items():
            self.run(kind, self.encode(kind, [value]))

    def encode(self, kind, texts):
        return self.models[kind].encode(texts)

    def run(self, kind, rows):
        with self.lock:
            if kind in self.active:
                raise RuntimeError("A model stream already has an active owner")
            self.active.add(kind)

        def block_limit():
            with self.lock:
                return self.shared_blocks if len(self.active) > 1 else 0

        try:
            with self.torch.cuda.stream(self.streams[kind]):
                return unique_batch(self.models[kind], rows, block_limit=block_limit)
        finally:
            with self.lock:
                self.active.remove(kind)
