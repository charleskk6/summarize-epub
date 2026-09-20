from __future__ import annotations
import asyncio
import io
import json
import zipfile
from pathlib import Path
import httpx
from rich.console import Console
from summarize_epub.cache import Cache
from summarize_epub.cli import execute, parser
from summarize_epub.config import Config
from summarize_epub.epub_io import read_epub
from summarize_epub.llm_client import LLMClient
from summarize_epub.pipeline import describe_images
from summarize_epub.chunker import soup


def test_retry_and_parameter_negotiation(monkeypatch) -> None:
    calls: list[dict] = []
    delays: list[float] = []
    async def sleep(delay: float) -> None:
        delays.append(delay)
    monkeypatch.setattr("summarize_epub.llm_client.asyncio.sleep", sleep)
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        n = len(calls)
        if n == 1:
            return httpx.Response(400, json={"error": "Use max_completion_tokens instead of max_tokens"})
        if n == 2:
            return httpx.Response(400, json={"error": "temperature only supports default value"})
        if n == 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if n == 4:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    async def run() -> None:
        async with LLMClient(Config(api_key="secret"), httpx.MockTransport(handler)) as client:
            assert await client.complete([{"role": "user", "content": "test"}]) == "ok"
    asyncio.run(run())
    assert len(calls) == 5 and delays[0] == 2
    assert "max_completion_tokens" in calls[-1] and "temperature" not in calls[-1]


def test_vision_deduplicates_and_caches(tiny_epub: Path, tmp_path: Path) -> None:
    source = read_epub(tiny_epub)
    chapter = source.chapters[1]
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"choices": [{"message": {"content": "圖中顯示資料流向。"}, "finish_reason": "stop"}]})
    async def run() -> None:
        cache = Cache(tmp_path / "cache.sqlite")
        async with LLMClient(Config(api_key="test", vision_model="vision"), httpx.MockTransport(handler)) as client:
            body = chapter.body + '<img src="../Images/diagram.png"/>'
            first = await describe_images(body, chapter, source, client, cache)
            assert len(soup(first).find_all("figcaption", attrs={"class": "ai-caption"})) == 2
            await describe_images(body, chapter, source, client, cache)
        cache.close()
    asyncio.run(run())
    assert calls == 1


def test_unsupported_vision_keeps_images(tiny_epub: Path, tmp_path: Path) -> None:
    source = read_epub(tiny_epub)
    chapter = source.chapters[1]
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "Images unsupported"})
    async def run() -> None:
        cache = Cache(tmp_path / "cache.sqlite")
        async with LLMClient(Config(api_key="test"), httpx.MockTransport(handler)) as client:
            result = await describe_images(chapter.body, chapter, source, client, cache)
            assert soup(result).img["src"] == soup(chapter.body).img["src"]
            await describe_images(chapter.body, chapter, source, client, cache)
        cache.close()
    asyncio.run(run())
    assert calls == 1


def test_cli_offline_list_dry_run(tiny_epub: Path, tmp_path: Path, monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise AssertionError("No requests allowed")
    monkeypatch.setattr(LLMClient, "complete", fail)
    for command in (["list", str(tiny_epub)], ["run", str(tiny_epub), "-o", str(tmp_path / "out.epub"), "--all", "--dry-run", "--cache", str(tmp_path / "state.sqlite")]):
        args = parser().parse_args(command)
        output = io.StringIO()
        assert asyncio.run(execute(args, Config(), Console(file=output))) == 0
        assert "Queues" in output.getvalue()
    assert not (tmp_path / "out.epub").exists()
