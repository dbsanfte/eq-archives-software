import asyncio
import json
import httpx
import pytest

from test_archive import make_archive, VECTOR, run


def client(handler):
    return httpx.AsyncClient(base_url='http://cuda/', transport=httpx.MockTransport(handler))


def test_primary_uses_query_prefix_and_caches_the_complete_valid_vector():
    async def exercise():
        archive = make_archive(embedding_handler=lambda _: pytest.fail('Fallback used unnecessarily'))
        def primary(request):
            assert json.loads(request.content)['input'] == ['task: search result | query: cleric']
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': VECTOR}]})
        archive.query_embeddings = client(primary)
        assert await archive.embedding('cleric') == VECTOR
        assert await archive.embedding('cleric') == VECTOR
        await archive.close()
        assert archive.query_embeddings.is_closed
    run(exercise())


@pytest.mark.parametrize('failure', ['connect', 'timeout', 'auth', 'busy', 'oversized', 'bad_vector', 'wrong_model'])
def test_primary_failure_uses_vulkan_and_cools_only_the_primary(failure):
    async def exercise():
        calls = []
        async def primary(request):
            calls.append(request)
            if failure == 'connect': raise httpx.ConnectError('private diagnostic')
            if failure == 'timeout': await asyncio.sleep(1)
            if failure == 'auth': return httpx.Response(401)
            if failure == 'busy': return httpx.Response(503)
            if failure == 'oversized': return httpx.Response(200, text='x'*65537)
            return httpx.Response(200, json={'model': 'wrong' if failure == 'wrong_model' else 'embeddinggemma-2-768-v1',
                'data': [{'embedding': [float('inf')]}] if failure == 'bad_vector' else [{'embedding': VECTOR}]})
        archive = make_archive()
        archive.query_embeddings = client(primary)
        assert await archive.embedding('cleric') == VECTOR
        assert archive.query_cooldown > 0 and archive.cooldown_until == 0
        assert await archive.embedding('bard') == VECTOR
        assert len(calls) == 1
        await archive.close()
    run(exercise())


def test_bad_input_does_not_change_cooldown_or_use_fallback():
    async def exercise():
        archive = make_archive(embedding_handler=lambda _: pytest.fail('Invalid input failed over'))
        archive.query_embeddings = client(lambda _: httpx.Response(400))
        assert await archive.embedding('cleric') is None
        assert archive.query_cooldown == archive.cooldown_until == 0
        await archive.close()
    run(exercise())


def test_four_query_embeddings_overlap_while_vulkan_fallback_remains_serial():
    async def exercise():
        ready, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def primary(request):
            calls.append(request)
            if len(calls) == 4: ready.set()
            await release.wait()
            return httpx.Response(200, json={'data': [{'embedding': VECTOR}]})
        archive = make_archive()
        archive.query_embeddings = client(primary)
        archive.embedding_lock = asyncio.Semaphore(4)
        tasks = [asyncio.create_task(archive.embedding(str(n))) for n in range(4)]
        await asyncio.wait_for(ready.wait(), 1)
        assert await archive.embedding('excess') is None
        release.set()
        assert await asyncio.gather(*tasks) == [VECTOR]*4
        archive.query_cooldown = float('inf')
        async with archive.fallback_lock:
            assert await archive.embedding('fallback busy') is None
        await archive.close()
    run(exercise())
