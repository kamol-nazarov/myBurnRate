"""Read a hub response under a fixed memory budget."""
from __future__ import annotations

import httpx

MAX_HUB_RESPONSE_BYTES = 8 * 1024 * 1024


def bounded_request(client: httpx.Client, method: str, url: str, *, headers=None, **kwargs):
    headers = httpx.Headers(headers)
    headers["Accept-Encoding"] = "identity"
    with client.stream(method, url, headers=headers, follow_redirects=False, **kwargs) as response:
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        length = response.headers.get("Content-Length")
        if encoding != "identity" or (length is not None and (
            not length.isdecimal() or len(length) > 12 or int(length) > MAX_HUB_RESPONSE_BYTES
        )):
            raise ValueError("The hub response exceeded the supported format or size.")
        body = bytearray()
        # Mock transports can supply an already-materialized body. Production
        # reads raw identity bytes incrementally from the transport.
        chunks = (response.content,) if response.is_stream_consumed else response.iter_raw(64 * 1024)
        for chunk in chunks:
            if len(body) + len(chunk) > MAX_HUB_RESPONSE_BYTES:
                raise ValueError("The hub response exceeded the supported size.")
            body.extend(chunk)
        return httpx.Response(response.status_code, headers=response.headers, content=bytes(body),
                              request=response.request)
