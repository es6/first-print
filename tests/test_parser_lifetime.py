"""Parser bursts stay bounded; cancellation cannot abandon a running thread."""

import asyncio
import threading

import pytest

from first_print.fetch import Fetcher
from first_print.source_document import document_from_text
from first_print.sources import RssFeed, Sighting
from first_print.text import release_document


def parser_probe(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    one, two, third = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    document = document_from_text("Example reported its financial results. " * 100)

    def parse(html: str):
        nonlocal calls
        with lock:
            calls += 1
            count = calls
        loop.call_soon_threadsafe(one.set)
        if count == 2:
            loop.call_soon_threadsafe(two.set)
        if count == 3:
            loop.call_soon_threadsafe(third.set)
        release.wait()
        return document

    monkeypatch.setattr("first_print.text.document_from_html", parse)
    return one, two, third, release


async def read(fetcher: Fetcher, ident: int = 0):
    return await release_document(
        fetcher,
        RssFeed("issuer", "https://issuer.example/feed"),
        Sighting(str(ident), "Results", "https://issuer.example/results", body=b"<p>Results</p>"),
    )


async def test_release_reads_bound_simultaneous_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    _, two, third, release = parser_probe(monkeypatch)
    async with Fetcher("Fixture fixture@example.com") as fetcher:
        readers = [asyncio.create_task(read(fetcher, index)) for index in range(3)]
        try:
            await asyncio.wait_for(two.wait(), 3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(third.wait(), 0.2)
            release.set()
            assert all(await asyncio.gather(*readers))
            assert third.is_set()
        finally:
            release.set()
            await asyncio.gather(*readers, return_exceptions=True)


async def test_cancelled_reader_retains_its_thread_until_parsing_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, _, _, release = parser_probe(monkeypatch)
    async with Fetcher("Fixture fixture@example.com") as fetcher:
        reader = asyncio.create_task(read(fetcher))
        try:
            await asyncio.wait_for(one.wait(), 3)
            reader.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            assert not reader.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await reader
        finally:
            release.set()
            await asyncio.gather(reader, return_exceptions=True)


async def test_fetcher_close_cancels_queued_reads_and_drains_running_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, two, third, release = parser_probe(monkeypatch)
    fetcher = Fetcher("Fixture fixture@example.com")
    await fetcher.__aenter__()
    readers = [asyncio.create_task(read(fetcher, index)) for index in range(3)]
    close = None
    try:
        await asyncio.wait_for(two.wait(), 3)
        close = asyncio.create_task(fetcher.__aexit__(None, None, None))
        for _ in range(3):
            await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await close
        assert all(reader.cancelled() for reader in readers)
        assert not third.is_set()
    finally:
        release.set()
        await asyncio.gather(*readers, return_exceptions=True)
        if close is not None:
            await close
        else:
            await fetcher.__aexit__(None, None, None)
