from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import discord
import pytest
from aiohttp import web

from assistant import discord_files as files
from assistant.discord_common import DiscordContext
from discord_fakes import GUILD_ID, http_error


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.1.1", "100.64.0.1",
                                  "::1", "fe80::1%eth0", "fc00::1", "0.0.0.0", "224.0.0.1", "not-an-ip"])
def test_non_public_addresses_are_refused(host: str) -> None:
    assert not files.is_public_address(host)


@pytest.mark.parametrize("host", ["1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_pass(host: str) -> None:
    assert files.is_public_address(host)


@pytest.mark.parametrize("url", ["http://example.com/a.png", "https://localhost/x", "https://127.0.0.1/x",
                                 "https://[::1]/x", "https://10.0.0.5/x", "ftp://example.com/x", "https:///nohost",
                                 "not a url"])
def test_check_url_refuses(url: str) -> None:
    assert files.check_url(url) is not None


def test_check_url_accepts_public_https() -> None:
    assert files.check_url("https://upload.wikimedia.org/cat.png") is None
    assert files.check_url("https://1.1.1.1/x") is None


class FakeResolver(files.AbstractResolver):
    def __init__(self, *hosts: str) -> None:
        self._hosts = hosts

    async def resolve(self, host: str, port: int = 0, family: Any = None) -> list[Any]:
        return [{"hostname": host, "host": h, "port": port, "family": 2, "proto": 0, "flags": 0} for h in self._hosts]

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("hosts", [("10.0.0.1",), ("1.1.1.1", "127.0.0.1"), ()])
async def test_resolver_refuses_any_private_answer(hosts: tuple[str, ...]) -> None:
    with pytest.raises(OSError):
        await files.PublicOnlyResolver(FakeResolver(*hosts)).resolve("evil.example", 443)


async def test_resolver_passes_public_answers() -> None:
    results = await files.PublicOnlyResolver(FakeResolver("1.1.1.1")).resolve("ok.example", 443)
    assert [r["host"] for r in results] == ["1.1.1.1"]


@pytest.mark.parametrize(("raw", "expected"), [("notes.md", "notes.md"), ("a b.csv", "a_b.csv"),
                                               ("../../etc/passwd", "etc_passwd"), ("", "fallback.txt"),
                                               ("...", "fallback.txt"), ("ø-ü.txt", "ø-ü.txt")])
def test_safe_filename(raw: str, expected: str) -> None:
    assert files.safe_filename(raw, "fallback.txt") == expected


@pytest.mark.parametrize("args", [{}, {"content": "a", "url": "https://x.org/a"}, {"content": ""},
                                  {"content": 5}, {"content": "x" * (files.MAX_TEXT_CHARS + 1)},
                                  {"content": "a", "message": "m" * 2001}])
def test_validate_send_rejects(args: dict[str, Any]) -> None:
    assert isinstance(files.validate_send(args), str)


@pytest.fixture
async def server() -> AsyncIterator[str]:
    async def ok(request: web.Request) -> web.Response:
        return web.Response(body=b"PNGDATA", content_type="image/png")

    async def hop(request: web.Request) -> web.Response:
        raise web.HTTPFound(f"/hop{int(request.match_info['n']) + 1}" if request.match_info["n"] != "9" else "/ok")

    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 5000)

    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404)

    async def to_forbidden(request: web.Request) -> web.Response:
        raise web.HTTPFound("/forbidden")

    app = web.Application()
    app.add_routes([web.get("/ok", ok), web.get("/hop{n}", hop), web.get("/big", big), web.get("/missing", missing),
                    web.get("/to-forbidden", to_forbidden)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    yield f"http://{host}:{port}"
    await runner.cleanup()


def _allow_but_forbidden(url: str) -> str | None:
    return "refused" if "/forbidden" in url else None


async def fetch(url: str) -> Any:
    async with aiohttp.ClientSession() as session:
        return await files.fetch_capped(session, url, check=_allow_but_forbidden)


async def test_fetch_returns_bytes(server: str) -> None:
    assert await fetch(f"{server}/ok") == (b"PNGDATA", f"{server}/ok")


async def test_fetch_follows_up_to_three_redirects(server: str) -> None:
    assert (await fetch(f"{server}/hop7"))[0] == b"PNGDATA"  # hop7 -> hop8 -> hop9 -> ok
    assert "Too many redirects" in await fetch(f"{server}/hop1")


async def test_fetch_checks_every_redirect_hop(server: str) -> None:
    assert await fetch(f"{server}/to-forbidden") == "refused"


async def test_fetch_size_cap(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "MAX_BYTES", 1000)
    assert "limit" in await fetch(f"{server}/big")


async def test_connector_really_uses_the_guard(server: str) -> None:
    """A host name that resolves to the local test server must not be reachable through the guarded connector."""
    port = server.rsplit(":", 1)[1]
    connector = aiohttp.TCPConnector(resolver=files.PublicOnlyResolver(FakeResolver("127.0.0.1")))
    async with aiohttp.ClientSession(connector=connector) as session:
        with pytest.raises((aiohttp.ClientError, OSError)):
            await files.fetch_capped(session, f"http://sneaky.example:{port}/ok", check=lambda url: None)
    # Control: the same request without the guard does reach the server, so the refusal above is the guard's.
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=FakeResolver("127.0.0.1"))) as session:
        assert (await files.fetch_capped(session, f"http://sneaky.example:{port}/ok", check=lambda url: None))[0] \
            == b"PNGDATA"


async def test_fetch_non_200(server: str) -> None:
    assert "HTTP 404" in await fetch(f"{server}/missing")


class FakeConversation:
    def __init__(self, error: discord.HTTPException | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.id, self._error = 42, error

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append({"content": content, **kwargs})


def ctx(channel: FakeConversation) -> DiscordContext:
    async def conversation() -> Any:
        return channel

    return DiscordContext(lambda: SimpleNamespace(), frozenset({GUILD_ID}), ZoneInfo("Europe/Oslo"), conversation)


async def test_send_text_file_into_the_conversation() -> None:
    channel = FakeConversation()
    result = await files.run_send_file(ctx(channel), {"filename": "q4 plan.md", "content": "# Plan", "message": "here"})
    sent = channel.sent[0]
    assert not result.get("is_error") and sent["content"] == "here"
    assert sent["file"].filename == "q4_plan.md" and sent["file"].fp.read() == b"# Plan"
    assert sent["allowed_mentions"].everyone is False


async def test_send_url_file_names_it_after_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(url: str) -> tuple[bytes, str]:
        return b"img", "https://cdn.example.org/pics/cat.png"

    monkeypatch.setattr(files, "_download", fake_download)
    channel = FakeConversation()
    await files.run_send_file(ctx(channel), {"filename": "", "url": "https://example.org/cat"})
    assert channel.sent[0]["file"].filename == "cat.png"


async def test_send_url_refusal_reaches_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FakeConversation()
    result = await files.run_send_file(ctx(channel), {"filename": "x.png", "url": "https://127.0.0.1/x.png"})
    assert result["is_error"] and "local or private" in result["content"][0]["text"] and not channel.sent


async def test_send_without_attach_files_permission() -> None:
    result = await files.run_send_file(ctx(FakeConversation(http_error())), {"filename": "a.txt", "content": "a"})
    assert result["is_error"] and "Attach Files" in result["content"][0]["text"]
