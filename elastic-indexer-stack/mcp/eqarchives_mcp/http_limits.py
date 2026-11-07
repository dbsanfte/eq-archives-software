"""Keep ASGI response admission coupled to actual transport backpressure."""


async def send_body(send, body, more_body=False):
    # Uvicorn waits for a paused transport before a send, not after that send
    # writes. A single multi-megabyte final write can therefore outlive the app
    # and its semaphore. Limit each write and drain before the final marker.
    for offset in range(0, len(body), 64 * 1024):
        await send({"type": "http.response.body", "body": bytes(body[offset:offset + 64 * 1024]),
                    "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": more_body})
