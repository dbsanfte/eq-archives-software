import asyncio
import base64
import json
import struct
import time
import threading
import io
import tarfile
from unittest.mock import patch, Mock
import unittest

from backend import EMBED_MODEL, RERANK_MODEL, packed_layout, load_library
from benchmark_http import checked_values, rerank_batches
from benchmark_batching import paired_summary
from check_kernels import check
from scheduler import BatchBudget, Busy, Scheduler, unique_batch
from server import App, response, validate
from service import command, settings
import fetch_cutlass
import tempfile
from pathlib import Path


class ContractTests(unittest.TestCase):
    def test_dynamic_budget_matches_full_causal_plan_without_mutating_on_rejection(self):
        rows = [[1] * 64 + [i] * (200 + i) for i in range(2, 20)]
        for causal in (True, False):
            budget, accepted = BatchBudget(causal, tokens=1024), []
            for row in rows:
                before = (
                    budget.dense,
                    budget.longest,
                    budget.shortest,
                    dict(budget.blocks),
                    set(budget.rows),
                )
                if budget.add(row):
                    accepted.append(row)
                    self.assertEqual(budget.dense, len(packed_layout(accepted, share_prefix=causal).ids))
                else:
                    self.assertEqual(
                        before, (budget.dense, budget.longest, budget.shortest, budget.blocks, budget.rows)
                    )
            self.assertLess(len(accepted), len(rows))
            self.assertTrue(budget.add(accepted[0]))
            self.assertEqual(len(budget.rows), len(accepted))

    def test_dynamic_budget_bounds_attention_even_when_most_tokens_are_shared(self):
        budget = BatchBudget(True)
        rows = [[1] * 767 + [i] for i in range(2, 18)]
        self.assertEqual([budget.add(row) for row in rows], [True] * 7 + [False] * 9)
        self.assertLess(budget.dense, 4096)

    def test_benchmark_batches_preserve_unicode_inputs_within_live_byte_bound(self):
        documents = [str(i) + "é" * 4000 for i in range(20)]
        batches = rerank_batches("cleric", documents)
        self.assertGreater(len(batches), 1)
        self.assertEqual([doc for batch in batches for doc in batch], documents)
        for batch in batches:
            body = {"model": RERANK_MODEL, "query": "cleric", "documents": batch}
            self.assertLessEqual(len(json.dumps(body, ensure_ascii=False).encode()), 128 * 1024)
            self.assertLessEqual(len(batch), 20)
        with self.assertRaises(ValueError):
            rerank_batches("cleric", ["x" * (128 * 1024)])

    def test_concurrency_comparison_uses_paired_completion_times(self):
        fast = paired_summary([200, 400, 800], [100, 200, 400])
        self.assertEqual(fast["paired_speedup_median"], 2)
        self.assertTrue(fast["concurrent_faster"])
        slow = paired_summary([200, 400, 800], [220, 440, 880])
        self.assertFalse(slow["concurrent_faster"])
        self.assertFalse(paired_summary([100, 100], [100, 100])["concurrent_faster"])
        for serial, concurrent in (([1], [1, 2]), ([1, 2], [1, float("nan")]), ([1, 0], [1, 2])):
            with self.assertRaises(ValueError):
                paired_summary(serial, concurrent)

    def test_incompatible_kernel_library_fails_before_a_gpu_launch(self):
        for old in (
            Mock(spec=[]),
            Mock(layer_abi_version=Mock(return_value=1)),
            Mock(layer_abi_version=Mock(return_value=2)),
        ):
            with patch("backend.C.CDLL", return_value=old), self.assertRaisesRegex(RuntimeError, "ABI"):
                load_library("unused.so")
        lib = Mock(layer_abi_version=Mock(return_value=3))
        with patch("backend.C.CDLL", return_value=lib):
            self.assertIs(load_library("unused.so"), lib)
        lib.launch_layer.assert_not_called()

    def test_shared_blocks_preserve_query_groups_sequences_and_last_tokens(self):
        common = list(range(64))
        rows = [
            common + [100] * 32 + [101] * 9,
            common + [100] * 32 + [102] * 65,
            common + [200] * 32 + [201] * 17,
            common + [200] * 32 + [202] * 7,
        ]
        layout = packed_layout(rows, share_prefix=True)
        self.assertEqual(len(layout.ids), 320)
        separate = sum(len(packed_layout(group, share_prefix=True).ids) for group in (rows[:2], rows[2:]))
        self.assertEqual(len(layout.ids), separate - 64)
        self.assertEqual(len(layout.batch_ids), len(layout.ids))
        self.assertEqual(len(layout.positions), len(layout.ids))
        for i, row in enumerate(rows):
            recovered = [layout.ids[layout.block_offsets[i][j // 32] + j % 32] for j in range(len(row))]
            self.assertEqual(recovered, row)
            self.assertEqual(layout.ids[layout.last_indices[i]], row[-1])
        for token, (owner, pos) in enumerate(zip(layout.batch_ids, layout.positions)):
            self.assertEqual(layout.ids[token], rows[owner][pos] if pos < len(rows[owner]) else 0)

    def test_shared_blocks_never_alias_different_contexts_or_padding(self):
        rows = [[1] * 32 + [9] * 32, [2] * 32 + [9] * 32]
        self.assertEqual(len(packed_layout(rows, share_prefix=True).ids), 128)
        rows = [[3] * 33, [3] * 33 + [0] * 31]
        self.assertEqual(len(packed_layout(rows, share_prefix=True).ids), 96)
        rows = [[3] * 33, [3] * 33]
        shared = packed_layout(rows, share_prefix=True)
        self.assertEqual(len(shared.ids), 64)
        self.assertEqual(shared.last_indices, [32, 32])
        self.assertEqual(len(packed_layout(rows, share_prefix=False).ids), 128)

    def test_benchmark_rejects_nonfinite_or_incomplete_model_outputs(self):
        body = {"model": EMBED_MODEL, "data": [{"index": 0, "embedding": [0.25] * 768}]}
        self.assertEqual(checked_values("embed", body, 1), [[0.25] * 768])
        for vector in ([float("nan")] * 768, [0.0] * 768, [0.5] * 767, [True] * 768):
            with self.assertRaises(RuntimeError):
                checked_values("embed", dict(body, data=[{"index": 0, "embedding": vector}]), 1)
        for model, rows in (
            (RERANK_MODEL, [{"index": 0, "relevance_score": float("nan")}]),
            (RERANK_MODEL, [{"index": False, "relevance_score": 0.5}]),
            (RERANK_MODEL, [{"index": 1, "relevance_score": 0.5}]),
            (RERANK_MODEL, []),
            (EMBED_MODEL, [{"index": 0, "relevance_score": 0.5}]),
        ):
            with self.assertRaises(RuntimeError):
                checked_values("rerank", {"model": model, "results": rows}, 1)

    def test_pinned_headers_reject_wrong_hash_before_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "headers"
            with patch("fetch_cutlass.urllib.request.urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    fetch_cutlass.fetch(destination)
            self.assertFalse(destination.exists())

    def test_header_extraction_keeps_only_headers_and_rejects_unsafe_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, destination = Path(tmp) / "source.tar", Path(tmp) / "headers"
            with tarfile.open(archive, "w") as stream:
                for name in ("include/cutlass/example.h", "examples/do-not-extract.py", "LICENSE.txt"):
                    info = tarfile.TarInfo("cutlass-" + fetch_cutlass.COMMIT + "/" + name)
                    info.size = 4
                    stream.addfile(info, io.BytesIO(b"test"))
            fetch_cutlass.extract(archive, destination)
            self.assertEqual((destination / "include/cutlass/example.h").read_bytes(), b"test")
            self.assertFalse((destination / "examples").exists())
            for name, kind in (("include/../../escape", tarfile.REGTYPE), ("include/link", tarfile.SYMTYPE)):
                with tarfile.open(archive, "w") as stream:
                    info = tarfile.TarInfo("cutlass-" + fetch_cutlass.COMMIT + "/" + name)
                    info.type = kind
                    info.linkname = "/tmp/unrelated"
                    stream.addfile(info)
                with self.assertRaises(ValueError):
                    fetch_cutlass.extract(archive, destination)

    def test_service_is_pinned_private_and_does_not_manage_existing_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            config = {"image": "sha256:" + "a" * 64, "revision": "b" * 40, "bind_address": "192.168.68.108"}
            path.write_text(json.dumps(config))
            self.assertEqual(settings(path), config)
            args = command(config)
            self.assertIn("127.0.0.1:8084:8080", args)
            self.assertIn("192.168.68.108:8084:8080", args)
            self.assertIn("--read-only", args)
            self.assertNotIn("--privileged", args)
            for field, value in [
                ("image", "latest"),
                ("revision", "master"),
                ("bind_address", "0.0.0.0"),
                ("bind_address", "8.8.8.8"),
            ]:
                path.write_text(json.dumps(dict(config, **{field: value})))
                with self.assertRaises(ValueError):
                    settings(path)

    def test_embedding_inputs_and_base64_preserve_identity(self):
        for source, expected in [
            ("text", ["text"]),
            (["a", "b"], ["a", "b"]),
            ([2, 4], [[2, 4]]),
            ([[2], [3]], [[2], [3]]),
        ]:
            self.assertEqual(validate("embed", {"model": EMBED_MODEL, "input": source})[0], expected)
        body = response("embed", [[0.5] * 768, [0.25] * 768], [[2], [3, 4]], {"encoding_format": "base64"})
        self.assertEqual([r["index"] for r in body["data"]], [0, 1])
        self.assertEqual(
            struct.unpack("<768f", base64.b64decode(body["data"][1]["embedding"])), (0.25,) * 768
        )
        self.assertEqual(body["usage"]["total_tokens"], 3)

    def test_invalid_input_never_gets_truncated_or_coerced(self):
        for source in [
            [],
            "",
            "\ud800",
            [True],
            [[True]],
            [[-1]],
            [[262144]],
            [[2] * 513],
            ["a", [1]],
            ["x"] * 65,
        ]:
            with self.subTest(source=str(source)[:20]), self.assertRaises((ValueError, UnicodeError)):
                validate("embed", {"model": EMBED_MODEL, "input": source})
        for extra in [
            {"dimensions": True},
            {"dimensions": 512},
            {"encoding_format": "int8"},
            {"url": "http://invalid"},
        ]:
            with self.assertRaises(ValueError):
                validate("embed", dict(model=EMBED_MODEL, input="text", **extra))
        with self.assertRaises(ValueError):
            validate("embed", {"model": RERANK_MODEL, "input": "text"})

    def test_rerank_prompt_and_complete_scores(self):
        texts, raw = validate(
            "rerank", {"model": RERANK_MODEL, "query": "cleric", "documents": ["heal", "bread"]}
        )
        self.assertFalse(raw)
        self.assertIn("<Query>: cleric\n<Document>: heal", texts[0])
        for values in [[0.5], [0.5, float("nan")], [0.5, 1.1], [True, 0.5]]:
            with self.assertRaises(RuntimeError):
                response("rerank", values, [[1], [2]], {})

    def test_exact_batch_dedup_retains_every_input_slot(self):
        class Model:
            def run(self, rows):
                self.rows = rows
                return [row[0] for row in rows]

        model = Model()
        self.assertEqual(unique_batch(model, [[1, 2], [3], [1, 2]]), [1, 3, 1])
        self.assertEqual(model.rows, [[1, 2], [3]])

    def test_spill_gate_covers_every_kernel_and_disassembly(self):
        log = "ptxas info : Function properties for layer\n 0 bytes stack frame, 0 bytes spill stores, 0 bytes spill loads\nptxas info : Used 56 registers"
        resources = "Function layer:\n REG:56 STACK:0 SHARED:0 LOCAL:0"
        self.assertEqual(check(log, resources, "HMMA.16816.F32")["kernels"], 1)
        for bad in [
            log.replace("0 bytes spill loads", "8 bytes spill loads"),
            "",
            log + "\nptxas info : Function properties for other\n 4 bytes spill stores, 0 bytes spill loads",
        ]:
            with self.assertRaises(ValueError):
                check(bad, resources, "HMMA.16816.F32")
        with self.assertRaises(ValueError):
            check(log, resources, "HMMA.16816.F32; LDL R1, [R2]")
        with self.assertRaises(ValueError):
            check(log, resources.replace("LOCAL:0", "LOCAL:32"), "HMMA.16816.F32")
        with self.assertRaises(ValueError):
            check(log.replace("0 bytes stack frame", "32 bytes stack frame"), resources, "HMMA.16816.F32")
        with self.assertRaises(ValueError):
            check(log, resources.replace("layer:", "different:"), "HMMA.16816.F32")


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.fail = False

    def encode(self, kind, rows):
        return [[i + 1] for i in range(len(rows))]

    def run(self, kind, rows):
        self.calls.append((kind, rows))
        if self.fail:
            raise RuntimeError("Private CUDA detail")
        return [[row[0] / 10] * 768 for row in rows] if kind == "embed" else [0.5] * len(rows)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = FakeEngine()
        self.scheduler = Scheduler(self.engine, batch=4, capacity=8, wait=0.01)
        self.scheduler.start()

    async def asyncTearDown(self):
        await self.scheduler.close()

    async def test_concurrent_requests_share_gpu_batch_keep_original_indices(self):
        one, two = await asyncio.gather(
            self.scheduler.submit("embed", [[1], [2]], time.monotonic() + 1),
            self.scheduler.submit("embed", [[3]], time.monotonic() + 1),
        )
        self.assertEqual([r[0] for r in one], [0.1, 0.2])
        self.assertEqual(two[0][0], 0.3)
        self.assertEqual(len(self.engine.calls), 1)

    async def test_cancellation_and_expiry_do_not_poison_later_requests(self):
        task = asyncio.create_task(self.scheduler.submit("embed", [[7]], time.monotonic() + 1))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(asyncio.TimeoutError):
            await self.scheduler.submit("embed", [[8]], time.monotonic() - 1)
        value = await self.scheduler.submit("embed", [[2]], time.monotonic() + 1)
        self.assertEqual(value[0][0], 0.2)
        self.assertFalse(any([7] in rows or [8] in rows for _, rows in self.engine.calls))

    async def test_capacity_and_gpu_fault_are_visible(self):
        with self.assertRaises(Busy):
            await self.scheduler.submit("embed", [[1]] * 9, time.monotonic() + 1)
        self.engine.fail = True
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await self.scheduler.submit("embed", [[1]], time.monotonic() + 1)
        self.assertTrue(self.scheduler.failed)
        with self.assertRaises(Busy):
            await self.scheduler.submit("embed", [[2]], time.monotonic() + 1)

    async def test_length_buckets_avoid_padding_short_queries_to_long_documents(self):
        await asyncio.gather(self.scheduler.submit("embed", [[1], [2] * 480, [3]], time.monotonic() + 1))
        self.assertEqual([len(rows) for _, rows in self.engine.calls], [2, 1])
        self.scheduler.running_since["embed"] = time.monotonic() - 13
        self.assertFalse(self.scheduler.healthy())
        self.scheduler.running_since["embed"] = None
        self.assertTrue(self.scheduler.healthy())

    async def test_similar_lengths_coalesce_across_old_fixed_bucket_boundary(self):
        rows = [[1] * 127, [2] * 129, [3] * 193]
        results = await self.scheduler.submit("rerank", rows, time.monotonic() + 1)
        self.assertEqual(len(results), 3)
        self.assertEqual(self.engine.calls, [("rerank", rows)])

    async def test_each_model_progresses_while_the_other_worker_is_blocked(self):
        original = self.engine.run
        loop = asyncio.get_running_loop()
        for blocked_kind, other_kind in (("embed", "rerank"), ("rerank", "embed")):
            started, release = asyncio.Event(), threading.Event()
            threads = {}

            def run(kind, rows):
                threads[kind] = threading.get_ident()
                if kind == blocked_kind:
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(2):
                        raise RuntimeError("Test worker was not released")
                return original(kind, rows)

            self.engine.run = run
            task = asyncio.create_task(self.scheduler.submit(blocked_kind, [[1]], time.monotonic() + 2))
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.wait_for(self.scheduler.submit(other_kind, [[2]], time.monotonic() + 1), 0.5)
                self.assertFalse(task.done())
                self.assertNotEqual(threads[blocked_kind], threads[other_kind])
            finally:
                release.set()
                await task

    async def test_one_models_queue_cannot_consume_the_others_capacity(self):
        # Queue both before the workers drain either queue.
        first = asyncio.create_task(self.scheduler.submit("embed", [[1]] * 8, time.monotonic() + 1))
        second = asyncio.create_task(self.scheduler.submit("rerank", [[2]] * 8, time.monotonic() + 1))
        a, b = await asyncio.gather(first, second)
        self.assertEqual((len(a), len(b)), (8, 8))

    async def test_shared_context_failure_drains_both_queues(self):
        self.engine.fail = True
        self.scheduler.batch = 1
        values = await asyncio.gather(
            self.scheduler.submit("embed", [[1]] * 4, time.monotonic() + 1),
            self.scheduler.submit("rerank", [[2]] * 4, time.monotonic() + 1),
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(value, (RuntimeError, Busy)) for value in values))
        self.assertFalse(self.scheduler.healthy())
        await asyncio.sleep(0)
        self.assertTrue(all(not queue for queue in self.scheduler.queues.values()))

    async def test_long_inputs_have_a_bounded_gpu_scheduling_quantum(self):
        self.scheduler.batch = 16
        await self.scheduler.submit("rerank", [[i] * 768 for i in range(8)], time.monotonic() + 1)
        self.assertEqual([len(rows) for _, rows in self.engine.calls], [5, 3])

    async def test_partial_request_chunks_recombine_and_share_prefix_budget(self):
        self.scheduler.batch, self.scheduler.capacity = 16, 16
        rows = [[1] * 128 + [i] * 352 for i in range(2, 12)]
        results = await asyncio.gather(
            self.scheduler.submit("rerank", rows[:5], time.monotonic() + 1),
            self.scheduler.submit("rerank", rows[5:], time.monotonic() + 1),
        )
        self.assertEqual(list(map(len, results)), [5, 5])
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.engine.calls[0][1], rows)

    async def test_unfittable_item_does_not_prevent_filling_batch_or_get_lost(self):
        self.scheduler.batch, self.scheduler.capacity = 32, 32
        rows = [[i] * 448 for i in range(2, 9)] + [[9] * 480, [10] * 512, [11] * 257]
        result = await self.scheduler.submit("embed", rows, time.monotonic() + 1)
        self.assertEqual([r[0] for r in result], [row[0] / 10 for row in rows])
        # The 512-token row waits; the later 288-padded-token row fills the gap.
        self.assertEqual([len(batch) for _, batch in self.engine.calls], [9, 1])
        self.assertEqual(self.engine.calls[1][1], [rows[8]])


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = FakeEngine()
        self.app = App(self.engine, {"embed": b"test-only", "rerank": b"r" * 32})
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.scheduler.close()

    async def request(self, data, path="/v1/embeddings", headers=None, method="POST", send_hook=None):
        received = False
        events = []

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": data, "more_body": False}
            await asyncio.Future()

        async def send(event):
            events.append(event)
            if send_hook:
                await send_hook(event)

        scope = {
            "type": "http",
            "path": path,
            "method": method,
            "query_string": b"",
            "headers": headers if headers is not None else [(b"authorization", b"Bearer test-only")],
        }
        await self.app(scope, receive, send)
        return events[0]["status"], json.loads(b"".join(e.get("body", b"") for e in events))

    async def test_auth_closed_routes_and_invalid_requests_recover(self):
        raw = json.dumps({"model": EMBED_MODEL, "input": ["first", "second"]}).encode()
        self.assertEqual((await self.request(raw, headers=[]))[0], 401)
        self.assertEqual((await self.request(raw, path="/v1/chat/completions"))[0], 404)
        self.assertEqual((await self.request(raw, method="PUT"))[0], 404)
        self.assertEqual((await self.request(b'{"model":1,"model":2}'))[0], 400)
        self.assertEqual((await self.request(b"x" * (256 * 1024 + 1)))[0], 413)
        code, result = await self.request(raw)
        self.assertEqual(code, 200)
        self.assertEqual([r["index"] for r in result["data"]], [0, 1])
        self.assertEqual(len(self.engine.calls), 1)

    async def test_admission_remains_held_through_response_delivery(self):
        raw = json.dumps({"model": EMBED_MODEL, "input": "test"}).encode()
        blocked, release = asyncio.Event(), asyncio.Event()

        async def hook(event):
            if event["type"] == "http.response.body":
                blocked.set()
                await release.wait()

        task = asyncio.create_task(self.request(raw, send_hook=hook))
        await blocked.wait()
        self.assertEqual(self.app.active, 1)
        self.assertEqual(self.app.active_by_kind, {"embed": 1, "rerank": 0})
        release.set()
        await task
        self.assertEqual(self.app.active, 0)
        self.assertEqual(self.app.active_by_kind, {"embed": 0, "rerank": 0})

    async def test_bulk_http_clients_cannot_consume_reranking_admission(self):
        self.app.active_by_kind["embed"] = 8
        raw = json.dumps({"model": EMBED_MODEL, "input": "test"}).encode()
        self.assertEqual((await self.request(raw))[0], 503)
        raw = json.dumps({"model": RERANK_MODEL, "query": "cleric", "documents": ["heal"]}).encode()
        result = await self.request(
            raw, path="/v1/rerank", headers=[(b"authorization", b"Bearer " + b"r" * 32)]
        )
        self.assertEqual(result[0], 200)
        self.app.active_by_kind["embed"] = 0

    async def test_invalid_batch_configuration_fails_before_start(self):
        for batch in (0, 33, True):
            app = App(self.engine, self.app.keys, batch=batch)
            with self.assertRaises(ValueError):
                await app.start()

    async def test_slow_upload_times_out_and_releases_admission(self):
        sent = []

        async def receive():
            await asyncio.Future()

        async def send(event):
            sent.append(event)

        scope = {
            "type": "http",
            "path": "/v1/embeddings",
            "method": "POST",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer test-only")],
        }
        await self.app(scope, receive, send)
        self.assertEqual(sent[0]["status"], 504)
        self.assertEqual(self.app.active_by_kind["embed"], 0)
        self.assertEqual(self.engine.calls, [])
        raw = json.dumps({"model": EMBED_MODEL, "input": "valid"}).encode()
        self.assertEqual((await self.request(raw))[0], 200)

    async def test_fault_and_stalled_worker_health_are_visible(self):
        self.assertEqual((await self.request(b"", path="/health", method="GET"))[0], 200)
        self.app.scheduler.running_since["embed"] = time.monotonic() - 13
        self.assertEqual((await self.request(b"", path="/health", method="GET"))[0], 503)


if __name__ == "__main__":
    unittest.main()
