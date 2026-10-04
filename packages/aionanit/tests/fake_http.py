"""A minimal stand-in for aioresponses, served by a real aiohttp test server.

Tests register canned responses by method and URL, the same shape aioresponses
used (`m.get(url, payload=..., status=...)`, `exception=...`). Requests go
through a real ClientSession to a local aiohttp server on 127.0.0.1, so the
client under test
parses genuine aiohttp responses (status handling, `json()` content type
checks, `raise_for_status`). An `exception` is raised client side instead,
which is how timeouts and connection failures reach the caller.

Each registration answers one request, in order. A request with nothing
registered fails the test with AssertionError, and so does a registration that
was never requested. aioresponses raised ClientConnectionError instead, which
the client under test maps to NanitConnectionError, so a mistyped URL in a
test expecting that error passed without testing anything.
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientResponse, ClientSession, web
from aiohttp.test_utils import TestServer
from yarl import URL


@dataclass
class _Canned:
    status: int
    body: bytes
    content_type: str
    exception: BaseException | None


class FakeApi:
    """Registry of canned responses keyed by (method, full URL)."""

    def __init__(self) -> None:
        self._queues: dict[tuple[str, URL], deque[_Canned]] = defaultdict(deque)

    def _add(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        payload: Any = None,
        body: str | bytes | None = None,
        content_type: str | None = None,
        exception: BaseException | None = None,
    ) -> None:
        if payload is not None:
            raw = json.dumps(payload).encode()
            content_type = content_type or "application/json"
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = body or b""
        self._queues[(method, URL(url))].append(
            _Canned(status, raw, content_type or "application/json", exception)
        )

    def get(self, url: str, **kwargs: Any) -> None:
        self._add("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> None:
        self._add("POST", url, **kwargs)

    def pop(self, method: str, url: URL) -> _Canned:
        queue = self._queues.get((method, url))
        if not queue:
            raise AssertionError(f"No canned response registered for {method} {url}")
        return queue.popleft()

    def assert_consumed(self) -> None:
        left = {f"{m} {u}": len(q) for (m, u), q in self._queues.items() if q}
        assert not left, f"Registered but never requested: {left}"


_current: FakeApi | None = None


@contextmanager
def mock_api() -> Iterator[FakeApi]:
    """Register canned responses for the requests made inside the block."""
    global _current
    previous, api = _current, FakeApi()
    _current = api
    try:
        yield api
    finally:
        _current = previous
    # Only reached when the block exited cleanly, so a failing assertion
    # inside it is not masked by this one.
    api.assert_consumed()


class FakeSession:
    """Routes get/post to the test server, or raises a registered exception."""

    def __init__(self) -> None:
        self._server: TestServer | None = None
        self._session: ClientSession | None = None
        self._ids = itertools.count()
        self._pending: dict[str, _Canned] = {}

    async def _request(
        self, method: str, url: str, *, params: Any = None, **kwargs: Any
    ) -> ClientResponse:
        full = URL(url)
        if params:
            full = full.update_query(params)
        if _current is None:
            raise AssertionError(f"No mock_api() active for {method} {full}")
        canned = _current.pop(method, full)
        if canned.exception is not None:
            raise canned.exception
        key = str(next(self._ids))
        self._pending[key] = canned
        assert self._session is not None and self._server is not None
        return await self._session.request(
            method, self._server.make_url(f"/canned/{key}"), **kwargs
        )

    async def get(self, url: str, **kwargs: Any) -> ClientResponse:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> ClientResponse:
        return await self._request("POST", url, **kwargs)

    async def _handle(self, request: web.Request) -> web.Response:
        canned = self._pending.pop(request.match_info["key"], None)
        if canned is None:
            return web.Response(status=500, text="no canned response for this key")
        return web.Response(
            status=canned.status,
            body=canned.body,
            headers={"Content-Type": canned.content_type},
        )

    async def start(self) -> None:
        """Start the server and a session that talks to it."""
        app = web.Application()
        app.router.add_route("*", "/canned/{key}", self._handle)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        self._session = ClientSession()

    async def close(self) -> None:
        self._pending.clear()
        if self._session is not None:
            await self._session.close()
        if self._server is not None:
            await self._server.close()
