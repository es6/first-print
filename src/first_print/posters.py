"""Discord release transport: durable messages, attachments and multi-server fanout."""

import asyncio
import hashlib
import io
import re
import secrets
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import unquote

import discord

from first_print.delivery import Delivery, DeliveryStore, retry_delivery
from first_print.release_cards import release_cards, release_content
from first_print.release_links import _line_url, _same_release_link

UNKNOWN_MESSAGE = 10008  # Discord's error code for a missing message
MAX_CONTENT = 2000  # Discord's limit for a message's text


class Poster(ABC):
    """Where a release gets posted: one message, created once and then edited. Each step returns
    the time the platform put on the change, when it gives one."""

    resumed = False
    first_seen: datetime | None = None

    @abstractmethod
    async def send(self, content: str) -> datetime | None: ...

    @abstractmethod
    async def edit(self, content: str) -> datetime | None: ...

    @abstractmethod
    async def attach(self, name: str, data: bytes) -> datetime | None: ...

    @abstractmethod
    async def set_summary(self, text: str, footer: str) -> datetime | None: ...


def discord_filename(name: str) -> str:
    """The name a file goes up under: letters, digits, "." "-" and "_" only, which Discord
    keeps as they are. Discord drops other characters, so a resumed alert couldn't recognize
    its own file and attached it again: Nebius's Q3 2026 alert had its PDF three times (read
    back from Discord on Nov 12)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", unquote(name)).strip("_") or "file"


class DiscordPoster(Poster):
    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel
        self.nonce = secrets.token_hex(12)
        self.message: discord.Message | None = None

    def _sent(self) -> discord.Message:
        if self.message is None:
            raise RuntimeError("send() must come before any edit")
        return self.message

    def _payload(self, content: str, summary: tuple[str, str] | None = None) -> dict[str, Any]:
        cards = release_cards(
            content, previous=self.message, first_seen=self.first_seen, summary=summary
        )
        if cards is None:
            return {"content": content}
        # Keep the ticker/quarter in push notifications, without duplicating all the links.
        return {"content": content.splitlines()[0], "embeds": cards}

    async def send(self, content: str) -> datetime | None:
        self.message = await self.channel.send(**self._payload(content), nonce=self.nonce)
        return self.message.created_at

    async def edit(self, content: str) -> datetime | None:
        self.message = await self._sent().edit(**self._payload(content))
        return self.message.edited_at

    async def attach(self, name: str, data: bytes) -> datetime | None:
        message = self._sent()
        file = discord.File(io.BytesIO(data), filename=discord_filename(name))
        self.message = await message.edit(attachments=[*message.attachments, file])
        return self.message.edited_at

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        payload = self._payload(release_content(self._sent()), (text, footer))
        if "embeds" in payload:
            self.message = await self._sent().edit(**payload)
            return self.message.edited_at
        # An embed fits 4,096 characters, twice a plain message
        embed = discord.Embed(description=text, color=discord.Color.dark_teal())
        embed.set_footer(text=footer)
        self.message = await self._sent().edit(embed=embed)
        return self.message.edited_at


class DurableDiscordPoster(DiscordPoster):
    def __init__(
        self,
        channel: discord.abc.Messageable,
        store: DeliveryStore,
        key: str,
        author_id: int,
    ) -> None:
        super().__init__(channel)
        self.store = store
        self.key = key
        self.author_id = author_id

    def _nonce(self, saved: Delivery) -> str:
        """The same for every try at one stored alert, across restarts, so Discord drops a
        repeat. A new alert for the key, after the old one was deleted, gets a new one."""
        intent = f"{self.store.channel_id}:{self.key}:{saved.prepared.isoformat()}"
        return hashlib.blake2s(intent.encode(), digest_size=12).hexdigest()

    async def send(self, content: str) -> datetime | None:
        async with self.store.lock(self.key):
            return await self._deliver(content)

    async def _send_prepared(self, saved: Delivery) -> datetime:
        self.message = await self.channel.send(
            **self._payload(saved.content), nonce=self._nonce(saved)
        )
        self.store.delivered(self.key, self.message.id)
        return self.message.created_at

    async def _deliver(self, content: str) -> datetime | None:
        saved = self.store.get(self.key)
        message = None
        if saved is not None and saved.message_id is not None:
            try:
                message = await self.channel.fetch_message(saved.message_id)
            except discord.NotFound as error:
                if error.code != UNKNOWN_MESSAGE:
                    raise
                # Keep the cached links when this poster watched the deleted alert.
                cached = self.message
                prior = (
                    release_content(cached)
                    if cached and cached.id == saved.message_id
                    else saved.content
                )
                content = self._merge_content(prior, content)
                self.store.forget(self.key)
                saved = None
        if saved is None:
            saved = self.store.prepare(self.key, content)
        elif message is None:
            # Discord's nonce protection is short-lived. After a crash, look for the
            # exact persisted initial message before sending again.
            after = saved.prepared - timedelta(seconds=5)
            async for candidate in self.channel.history(limit=None, after=after):
                if (
                    candidate.author.id == self.author_id
                    and release_content(candidate) == saved.content
                ):
                    message = candidate
                    break
        self.resumed = message is not None
        if message is None:
            created = await self._send_prepared(saved)
            message = self._sent()
        else:
            created = None
            self.message = message
            self.store.delivered(self.key, message.id)
        existing = release_content(message)
        if content == existing:
            return created
        merged = self._merge_content(existing, content)
        if merged != existing:
            try:
                await super().edit(merged)
            except discord.NotFound as error:
                if error.code != UNKNOWN_MESSAGE:
                    raise
                # The message was deleted between loading it and editing it.
                self.store.forget(self.key)
                self.resumed = False
                return await self._send_prepared(self.store.prepare(self.key, merged))
        return created

    def _merge_content(self, existing: str, content: str) -> str:
        lines = existing.splitlines()
        urls = [_line_url(line) for line in lines[2:]]
        for line in content.splitlines()[2:]:
            url = _line_url(line)
            if url not in urls and not any(_same_release_link(url, known) for known in urls):
                lines.append(line)
                urls.append(url)
        merged = "\n".join(lines)
        return merged if len(merged) <= MAX_CONTENT else merged[: MAX_CONTENT - 1] + "…"

    async def _update(
        self,
        action: Callable[[], Awaitable[datetime | None]],
        content: str,
        *,
        retry_after_replacement: bool = False,
    ) -> datetime | None:
        try:
            return await action()
        except discord.NotFound as error:
            if error.code != UNKNOWN_MESSAGE:
                raise
        posted = await retry_delivery(partial(self.send, content))
        return await action() if retry_after_replacement else posted

    async def edit(self, content: str) -> datetime | None:
        message = self._sent()
        existing = release_content(message)
        merged = self._merge_content(existing, content)
        if merged == existing:
            return message.edited_at
        return await self._update(partial(super().edit, merged), merged)

    async def attach(self, name: str, data: bytes) -> datetime | None:
        message = self._sent()
        # The forms Discord may have stored it under, an upload from before discord_filename
        # included: it drops "%", so "Nebius%20Q3...pdf" became "Nebius20Q3...pdf"
        stored = {name, discord_filename(name), name.replace("%", "")}
        if any(attachment.filename in stored for attachment in message.attachments):
            return message.edited_at
        return await self._update(
            partial(super().attach, name, data),
            release_content(message),
            retry_after_replacement=True,
        )

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        return await self._update(
            partial(super().set_summary, text, footer),
            release_content(self._sent()),
            retry_after_replacement=True,
        )


class FanOutPoster(Poster):
    """One release posted to every server that tracks the company: each step goes to every
    channel at once, so no server's alert waits on another's. The first poster's channel is the
    one whose times the journal records. A step one channel refuses is raised after the others
    have had theirs; trying it again is safe, since each durable poster keeps its own message."""

    def __init__(self, posters: Sequence[Poster]) -> None:
        if not posters:
            raise ValueError("A release needs somewhere to post")
        self.posters = list(posters)

    async def _each(
        self, step: Callable[[Poster], Coroutine[Any, Any, datetime | None]]
    ) -> datetime | None:
        for poster in self.posters:
            poster.first_seen = self.first_seen
        results = await asyncio.gather(
            *(step(poster) for poster in self.posters), return_exceptions=True
        )
        # Resumed only when every server's alert was already up
        self.resumed = all(poster.resumed for poster in self.posters)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        first = results[0]
        return first if isinstance(first, datetime) else None

    async def send(self, content: str) -> datetime | None:
        return await self._each(lambda poster: poster.send(content))

    async def edit(self, content: str) -> datetime | None:
        return await self._each(lambda poster: poster.edit(content))

    async def attach(self, name: str, data: bytes) -> datetime | None:
        return await self._each(lambda poster: poster.attach(name, data))

    async def set_summary(self, text: str, footer: str) -> datetime | None:
        return await self._each(lambda poster: poster.set_summary(text, footer))


def fan_out(posters: Sequence[Poster]) -> Poster:
    """One server's poster as it is; several behind a FanOutPoster."""
    return posters[0] if len(posters) == 1 else FanOutPoster(posters)
