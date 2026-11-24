"""send_file: post a text file Nethor wrote, or a file from a public https URL, into the current conversation.

URL fetches run on the owner's machine, so nothing on localhost or the LAN may be reached: literal IPs are checked
per hop (aiohttp skips the resolver for them) and host names go through a resolver that drops the whole answer if
any address isn't public. Redirects are followed by hand so every hop gets both checks.
"""

from __future__ import annotations

import io
import ipaddress
import re
import socket
from collections.abc import Callable
from typing import Any

import aiohttp
import discord
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver
from yarl import URL

from assistant.discord_common import DiscordContext, error_result, log, text_result

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_MESSAGE_CHARS = 2000
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 20
CHUNK = 64 * 1024
REDIRECTS = {301, 302, 303, 307, 308}


def is_public_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def check_url(url: str) -> str | None:
    """None when url may be fetched, else why not."""
    try:
        parsed = URL(url)
    except (TypeError, ValueError):
        return f"{url!r} is not a valid URL."
    if parsed.scheme != "https":
        return "Only https:// links can be attached."
    host = parsed.host or ""
    if not host:
        return f"{url!r} has no host."
    if host.lower() == "localhost" or (_ip_literal(host) and not is_public_address(host.strip("[]"))):
        return f"{host} is a local or private address."
    return None


class PublicOnlyResolver(AbstractResolver):
    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self._inner = inner or ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET,
                      ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        if not results or not all(is_public_address(r["host"]) for r in results):
            raise OSError(f"{host} resolves to a local or private address")
        return results

    async def close(self) -> None:
        await self._inner.close()


def safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.-]", "_", name.strip())[:100].strip("._")
    return cleaned or fallback


def name_from_url(url: str) -> str:
    return safe_filename(URL(url).name, "download")


async def _read_capped(response: aiohttp.ClientResponse) -> bytes | str:
    if response.content_length is not None and response.content_length > MAX_BYTES:
        return f"The file is {response.content_length // 1_048_576} MB; the limit is {MAX_BYTES // 1_048_576} MB."
    data = bytearray()
    async for chunk in response.content.iter_chunked(CHUNK):
        data += chunk
        if len(data) > MAX_BYTES:
            return f"The file is over the {MAX_BYTES // 1_048_576} MB limit."
    return bytes(data)


async def fetch_capped(session: aiohttp.ClientSession, url: str,
                       check: Callable[[str], str | None] = check_url) -> tuple[bytes, str] | str:
    """(bytes, final url) or why not, following at most MAX_REDIRECTS redirects, each one checked."""
    for _ in range(MAX_REDIRECTS + 1):
        problem = check(url)
        if problem:
            return problem
        async with session.get(url, allow_redirects=False) as response:
            if response.status in REDIRECTS:
                location = response.headers.get("Location")
                if not location:
                    return f"{url} redirects without saying where."
                url = str(response.url.join(URL(location)))
                continue
            if response.status != 200:
                return f"{url} answered HTTP {response.status}."
            data = await _read_capped(response)
            return data if isinstance(data, str) else (data, url)
    return f"Too many redirects (more than {MAX_REDIRECTS})."


def public_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
    return aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS))


async def _download(url: str) -> tuple[bytes, str] | str:
    try:
        async with public_session() as session:
            return await fetch_capped(session, url)
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        log.warning("send_file fetch failed: url=%s error=%r", url, exc)
        return f"Couldn't download {url}: {exc}"


def validate_send(args: dict[str, Any]) -> tuple[str | None, str | None, str | None] | str:
    """(content, url, message) with exactly one of content/url, or what is wrong."""
    content, url, message = args.get("content"), args.get("url"), args.get("message")
    given = [v for v in (content, url) if v not in (None, "")]
    if len(given) != 1 or not isinstance(given[0], str):
        return "Give exactly one of content (text) or url (https link)."
    if isinstance(content, str) and len(content) > MAX_TEXT_CHARS:
        return f"content is over {MAX_TEXT_CHARS:,} characters."
    if message is not None and (not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS):
        return f"message must be text of at most {MAX_MESSAGE_CHARS} characters."
    return (content or None), (url or None), (message or None)


async def _payload(content: str | None, url: str | None, filename: object) -> tuple[bytes, str, str] | str:
    """(bytes, filename, source for the log) or why not."""
    wanted = filename if isinstance(filename, str) else ""
    if content is not None:
        return content.encode("utf-8"), safe_filename(wanted, "file.txt"), "text"
    assert url is not None
    downloaded = await _download(url)
    if isinstance(downloaded, str):
        return downloaded
    data, final_url = downloaded
    return data, safe_filename(wanted, name_from_url(final_url)), URL(final_url).host or "?"


async def run_send_file(ctx: DiscordContext, args: dict[str, Any]) -> dict[str, Any]:
    checked = validate_send(args)
    if isinstance(checked, str):
        return error_result(checked)
    content, url, message = checked
    payload = await _payload(content, url, args.get("filename"))
    if isinstance(payload, str):
        return error_result(payload)
    data, name, source = payload
    channel = await ctx.conversation_channel()
    try:
        await channel.send(content=message, file=discord.File(io.BytesIO(data), filename=name),
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        log.warning("send_file forbidden: channel=%s name=%s", getattr(channel, "id", "?"), name)
        return error_result("The bot lacks Attach Files here.")
    except discord.HTTPException as exc:
        log.exception("send_file failed: channel=%s name=%s bytes=%d", getattr(channel, "id", "?"), name, len(data))
        return error_result(f"Discord refused {name}: {exc}")
    log.info("sent file name=%s bytes=%d source=%s channel=%s", name, len(data), source, getattr(channel, "id", "?"))
    return text_result(f"✅ Sent {name} ({len(data):,} bytes).")
