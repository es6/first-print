import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from first_print import bot as bot_module
from first_print.bot import EarningsBot
from first_print.config import Settings
from first_print.edit_priority import EditPriority
from first_print.summary import Summarizer


async def test_cancelled_card_retains_and_drains_cleanup_even_with_further_cancellation() -> None:
    priority = EditPriority()
    started, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = False

    async def request() -> None:
        nonlocal finished
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            finished = True

    caller = asyncio.create_task(priority.card(request()))
    try:
        await started.wait()
        caller.cancel()
        await cleaning.wait()
        await asyncio.sleep(0)
        assert not caller.done() and priority._cards
        caller.cancel()
        async with priority.release():
            assert priority.release_waiting  # urgent releases do not wait for card cleanup
        await asyncio.sleep(0)
        assert not finished and not caller.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished and not priority._cards
    finally:
        release_cleanup.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_summary_client_close_is_owned_and_idempotent_under_cancellation() -> None:
    writer = Summarizer("not-a-real-key")
    await writer.client.close()  # release the unused SDK transport before installing the probe
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def close() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()

    writer.client = cast(Any, SimpleNamespace(close=close))
    caller = asyncio.create_task(writer.close())
    try:
        await started.wait()
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        caller.cancel()
        other = asyncio.create_task(writer.close())
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await other
        await writer.close()
        assert calls == 1
    finally:
        finish.set()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["return", "error", "cancel", "startup"])
async def test_watcher_closes_summary_transport_after_every_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    settings = Settings.model_validate(
        {
            "discord_token": "fake",
            "discord_channel_id": 7,
            "sec_user_agent": "Fixture fixture@example.com",
        }
    )
    bot = EarningsBot(settings, tmp_path / "one.toml")
    monkeypatch.setattr(bot, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(
        bot, "fetch_channel", AsyncMock(return_value=Mock(spec=discord.TextChannel))
    )
    events: list[str] = []
    started = asyncio.Event()

    async def close() -> None:
        events.append("closed")

    writer = SimpleNamespace(close=close)
    monkeypatch.setattr(bot_module, "summarizer_from", lambda _: writer)

    def load(*args: object) -> None:
        if outcome == "startup":
            raise ValueError("startup failed")

    monkeypatch.setattr(bot_module, "load_plan", load)

    async def watch(*args: object) -> None:
        events.append("started")
        started.set()
        try:
            if outcome == "cancel":
                await asyncio.Event().wait()
            if outcome == "error":
                raise ValueError("startup failed")
        finally:
            events.append("watch ended")

    monkeypatch.setattr(bot_module, "watch_schedule", watch)
    task = asyncio.create_task(bot._watch(tmp_path / "one.toml"))
    if outcome == "cancel":
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome in ("error", "startup"):
        with pytest.raises(ValueError, match="startup failed"):
            await task
    else:
        await task
    assert events == (["closed"] if outcome == "startup" else ["started", "watch ended", "closed"])


@pytest.mark.parametrize("already_cancelling", [False, True])
async def test_bot_close_drains_watcher_before_discord_even_if_close_caller_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    already_cancelling: bool,
) -> None:
    bot = EarningsBot(Settings.model_validate({"discord_token": "fake"}))
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events: list[str] = []

    async def watching() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            events.append("watch cleaned")

    async def close_discord(self: discord.Client) -> None:
        events.append("discord closed")

    monkeypatch.setattr(discord.Client, "close", close_discord)
    bot.watcher = asyncio.create_task(watching())
    await started.wait()
    if already_cancelling:
        bot.watcher.cancel()
        await cleaning.wait()
    caller = asyncio.create_task(bot.close())
    try:
        # The old close returned immediately and never cancelled the watcher.
        for _ in range(10):
            await asyncio.sleep(0)
            if cleaning.is_set() or caller.done():
                break
        assert cleaning.is_set() and not caller.done()
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done() and events == []
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await bot.close()
        assert events == ["watch cleaned", "discord closed"]
    finally:
        finish.set()
        bot.watcher.cancel()
        await asyncio.gather(bot.watcher, caller, return_exceptions=True)


async def test_watcher_failure_starts_an_owned_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = EarningsBot(Settings.model_validate({"discord_token": "fake"}), tmp_path)
    closed = AsyncMock()
    monkeypatch.setattr(discord.Client, "close", closed)

    async def failed() -> None:
        raise ValueError("fixture watcher failure")

    bot.watcher = asyncio.create_task(failed())
    await asyncio.gather(bot.watcher, return_exceptions=True)
    bot._watch_ended(bot.watcher)
    assert bot._shutdown_task is not None
    await bot.close()
    closed.assert_awaited_once()
