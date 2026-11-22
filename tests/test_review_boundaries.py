import asyncio
import io
from datetime import timedelta
from pathlib import Path

import pytest

from first_print.consensus import calendar_rows, estimate_rows
from first_print.fetch import Fetcher, Response
from first_print.pipeline import Releases
from first_print.recorder import Journal, SharedPoll, Watch
from first_print.sources import RssFeed, Sighting
from first_print.transition_evidence import TransitionEvidence
from tests.test_pipeline import FakePoster, fetcher, uber


async def test_closing_fetcher_releases_queued_callers_without_granting_a_turn() -> None:
    queued: asyncio.Task[None] | None = None
    try:
        async with Fetcher("Fixture fixture@example.com") as fetcher:
            await fetcher.wait_turn("queue.example")
            queued = asyncio.create_task(fetcher.wait_turn("queue.example"))
            await asyncio.sleep(0)
            assert not queued.done()
        await asyncio.sleep(0)
        assert queued.done()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert all(q.dispatcher is None or q.dispatcher.done() for q in fetcher._queues.values())
    finally:
        if queued is not None:
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)


def test_malformed_provider_shapes_fail_as_unavailable_instead_of_escaping_validation() -> None:
    raw = {
        "symbol": "EX",
        "estimates": [
            {
                "date": "2026-10-31",
                "horizon": "fiscal quarter",
                "revenue_estimate_average": "42000000000",
                "revenue_estimate_analyst_count": "12",
            }
        ],
    }
    assert estimate_rows(raw, "EX", "USD")
    for malformed in ([], None, "unavailable", 1):
        with pytest.raises(ValueError, match="unavailable"):
            estimate_rows(malformed, "EX", "USD")
    header = "symbol,reportDate,fiscalDateEnding,currency\n"
    valid = "EX,2026-11-20,2026-10-31,USD\n"
    assert len(calendar_rows(header + valid)) == 1
    assert calendar_rows(header + "EX,2026-11-20\n") == []
    assert calendar_rows(header + "EX,not-a-date,2026-10-31,USD\n") == []


@pytest.mark.parametrize("whole_watcher", [False, True])
async def test_finishing_releases_drains_headline_read_cleanup(
    monkeypatch: pytest.MonkeyPatch, whole_watcher: bool
) -> None:
    entered, cleaning, leave = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(*args: object) -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await leave.wait()

    releases = Releases(FakePoster, fetcher(), Journal(io.StringIO()), None)
    monkeypatch.setattr(releases, "_read_headline", read)
    target = uber()
    releases.on_new(
        target,
        RssFeed("issuer", "https://issuer.example/feed"),
        Sighting("item", "Business update", "https://issuer.example/update"),
    )
    await entered.wait()
    workers = tuple(releases.checks[target.release_key])
    close = asyncio.create_task(
        releases.finish() if whole_watcher else releases.finish_target(target)
    )
    try:
        async with asyncio.timeout(1):
            await cleaning.wait()
        assert not close.done()
        leave.set()
        await close
        assert all(task.done() for task in workers)
        assert not releases.checks and not releases.checked
    finally:
        leave.set()
        for task in workers:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(close, *workers, return_exceptions=True)


async def test_last_shared_poll_member_keeps_ownership_until_request_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, cleaning, leave = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def poll(*args: object) -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await leave.wait()

    monkeypatch.setattr("first_print.recorder._poll_once", poll)
    log = Journal(io.StringIO())
    source = RssFeed("wire", "https://wire.example/feed")
    start, end = log.now(), log.now() + timedelta(minutes=1)
    shared = SharedPoll(source, fetcher(), log)
    member = asyncio.create_task(shared.join(Watch("EX", source, (start, end)), end))
    await entered.wait()
    worker = shared.task
    assert worker is not None
    member.cancel()
    try:
        await cleaning.wait()
        assert not member.done()
        leave.set()
        with pytest.raises(asyncio.CancelledError):
            await member
        assert worker.done() and shared.task is None and not shared.members
    finally:
        leave.set()
        await asyncio.gather(member, worker, return_exceptions=True)


async def test_evidence_flush_surfaces_a_failed_writer_instead_of_waiting_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = TransitionEvidence(tmp_path)

    def fail(snapshot: object) -> None:
        raise ValueError("invalid evidence metadata")

    monkeypatch.setattr(evidence, "_write", fail)
    for index in range(2):
        evidence.offer(
            str(index), {}, Response("https://issuer.example", 200, {}, b"ok", 0), positive=True
        )
    # A failed writer leaves another item queued. Flush must expose that failure.
    assert evidence._writer is not None
    await asyncio.gather(evidence._writer, return_exceptions=True)
    with pytest.raises(ValueError, match="invalid evidence"):
        async with asyncio.timeout(1):
            await evidence.flush()
