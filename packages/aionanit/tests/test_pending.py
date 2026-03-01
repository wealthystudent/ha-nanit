"""Tests for aionanit.ws.pending — PendingRequests."""

from __future__ import annotations

import asyncio

import pytest

from aionanit.proto import Response
from aionanit.ws.pending import PendingRequests


class TestNextId:
    def test_starts_at_one(self) -> None:
        p = PendingRequests()
        assert p.next_id() == 1

    def test_monotonically_increasing(self) -> None:
        p = PendingRequests()
        ids = [p.next_id() for _ in range(5)]
        assert ids == [1, 2, 3, 4, 5]


class TestTrack:
    async def test_creates_future(self) -> None:
        p = PendingRequests()
        fut = p.track(1)
        assert isinstance(fut, asyncio.Future)
        assert not fut.done()

    async def test_increments_pending_count(self) -> None:
        p = PendingRequests()
        assert p.pending_count == 0
        p.track(1)
        assert p.pending_count == 1
        p.track(2)
        assert p.pending_count == 2

    async def test_duplicate_id_raises_value_error(self) -> None:
        p = PendingRequests()
        p.track(1)
        with pytest.raises(ValueError, match="already tracked"):
            p.track(1)


class TestResolve:
    async def test_resolves_matching_future(self) -> None:
        p = PendingRequests()
        fut = p.track(1)
        resp = Response(request_id=1, status_code=200)
        assert p.resolve(1, resp) is True
        assert fut.done()
        assert fut.result() is resp

    async def test_returns_false_for_unknown_id(self) -> None:
        p = PendingRequests()
        resp = Response(request_id=99)
        assert p.resolve(99, resp) is False

    async def test_removes_entry_from_pending(self) -> None:
        p = PendingRequests()
        p.track(1)
        assert p.pending_count == 1
        p.resolve(1, Response(request_id=1))
        assert p.pending_count == 0

    async def test_returns_false_for_already_done_future(self) -> None:
        p = PendingRequests()
        fut = p.track(1)
        fut.cancel()
        # Future is done (cancelled), but still in the map — pop removes it
        # resolve should return False because the future is already done
        resp = Response(request_id=1)
        assert p.resolve(1, resp) is False


class TestCancelAll:
    async def test_cancels_all_futures(self) -> None:
        p = PendingRequests()
        f1 = p.track(1)
        f2 = p.track(2)
        p.cancel_all()
        assert f1.cancelled()
        assert f2.cancelled()

    async def test_sets_exception_when_error_provided(self) -> None:
        p = PendingRequests()
        f1 = p.track(1)
        f2 = p.track(2)
        error = RuntimeError("disconnected")
        p.cancel_all(error=error)
        assert f1.done() and not f1.cancelled()
        assert f2.done() and not f2.cancelled()
        with pytest.raises(RuntimeError, match="disconnected"):
            f1.result()
        with pytest.raises(RuntimeError, match="disconnected"):
            f2.result()

    async def test_clears_pending_map(self) -> None:
        p = PendingRequests()
        p.track(1)
        p.track(2)
        p.cancel_all()
        assert p.pending_count == 0

    async def test_track_works_after_cancel_all(self) -> None:
        p = PendingRequests()
        p.track(1)
        p.cancel_all()
        fut = p.track(2)
        assert isinstance(fut, asyncio.Future)
        assert p.pending_count == 1
