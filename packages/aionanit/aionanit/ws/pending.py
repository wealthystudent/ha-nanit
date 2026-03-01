"""Request/response correlation for outgoing protobuf requests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aionanit.proto import Response


class PendingRequests:
    """Tracks outgoing requests and correlates them with responses.

    Each request gets a unique ID and an asyncio.Future.
    When a response arrives with a matching request_id, the future is resolved.
    Timeouts are enforced via asyncio.wait_for at the call site.
    """

    def __init__(self) -> None:
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._counter: int = 0

    def next_id(self) -> int:
        """Return next unique request ID (monotonically increasing)."""
        self._counter += 1
        return self._counter

    def track(self, request_id: int) -> asyncio.Future[Response]:
        """Register a pending request. Returns a Future to await.

        Raises ValueError if request_id is already tracked.
        """
        if request_id in self._pending:
            raise ValueError(f"Request {request_id} is already tracked")
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    def resolve(self, request_id: int, response: Response) -> bool:
        """Resolve a pending request with its response.

        Returns True if a matching request was found, False otherwise.
        Removes the entry from the pending map after resolution.
        """
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(response)
        return True

    def cancel_all(self, error: Exception | None = None) -> None:
        """Cancel/fail all pending futures. Called on disconnect/close.

        If error is provided, futures are set_exception(error).
        Otherwise, futures are cancelled.
        Clears the pending map.
        """
        for future in self._pending.values():
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.cancel()
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        """Number of in-flight requests."""
        return len(self._pending)
