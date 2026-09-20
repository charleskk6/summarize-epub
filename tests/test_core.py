from __future__ import annotations
import asyncio
import json
import zipfile
from pathlib import Path
import httpx
import pytest
from summarize_epub.cache import Cache
from summarize_epub.chunker import TOKEN_RE, protect, restore, soup, split_chapter, token_error
from summarize_epub.config import Config
from summarize_epub.epub_io import Result, read_epub, validate_navigation, write_epub, xml
from summarize_epub.glossary import glossary_xhtml, parse_response
from summarize_epub.llm_client import LLMClient
from summarize_epub.pipeline import run_book
from summarize_epub.selection import parse_indices, resolve

@pytest.mark.parametrize(("text", "expected"), [("1,4,9-12", [1,4,9,10,11,12]), ("3-", list(range(3,13))), ("all", list(range(1,13)))])
def test_selection(text: str, expected: list[int]) -> None:
    assert parse_indices(text, 12) == expected

@pytest.mark.parametrize("text", ["0", "13", "4-2", "-3", "1,,3", "a", "3-4-5"])
def test_invalid_selection(text: str) -> None:
    with pytest.raises(ValueError):
        parse_indices(text, 12)

def test_config_precedence() -> None:
    cfg = Config.load({"model": "cli", "temperature": 0.0}, env={"LLM_API_KEY": "secret", "LLM_MODEL": "env", "LLM_CONCURRENCY": "7"})
    assert cfg.model == "cli" and cfg.temperature == 0 and cfg.concurrency == 7
    assert cfg.max_tokens == 8192 and cfg.vision_model == "cli"
    assert "secret" not in repr(cfg)
    assert Config.load({}, env={"LLM_MODEL": "env"}, require_key=False).model == "env"
    assert Config.load({}, env={}, require_key=False).model == "gpt-5"
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        Config.load({}, env={})

def test_placeholder_round_trip(tiny_epub: Path) -> None:
    chapter = read_epub(tiny_epub).chapters[1]
    prepared = protect(chapter.body)
    assert "<img" not in prepared.xhtml
    assert token_error(prepared.xhtml, TOKEN_RE.findall(prepared.xhtml)) is None
    recovered, original = soup(restore(prepared.xhtml, prepared.mapping)), soup(chapter.body)
    assert [x.attrs for x in recovered.find_all("img")] == [x.attrs for x in original.find_all("img")]
    assert str(recovered.figure) == str(original.figure)
    assert recovered.pre.get_text() == original.pre.get_text()

def test_multi_image_figure() -> None:
    original = '<figure id="f"><img src="a.png"/><img src="b.png"/><figcaption>Original</figcaption></figure><img src="c.png"/>'
    prepared = protect(original)
    result = restore("<p>Explain</p>\n" + "\n".join(TOKEN_RE.findall(prepared.xhtml)), prepared.mapping)
    assert len(soup(result).find_all("img")) == 3
    assert soup(result).figure.figcaption.get_text() == "Original"
    assert len(soup(result).find_all("figure")) == 1

def test_chunker() -> None:
    body = "<section id='s'>" + "".join(f"<div><h2>Heading {n}</h2><p>{'example ' * 80}</p></div>" for n in range(12)) + "</section>"
    chunks = split_chapter(body, 600)
    assert len(chunks) > 1
    assert sum(soup(chunk).get_text().count("example") for chunk in chunks) == 960
    assert sum(len(soup(chunk).find_all(id="s")) for chunk in chunks) == 1
    for chunk in chunks:
        xml(("<root>" + chunk + "</root>").encode())
    assert split_chapter("<p>short</p>") == ["<p>short</p>"]
    with pytest.raises(ValueError, match="indivisible"):
        split_chapter("<p>" + "x " * 4000 + "</p>", 100)

def test_resolution(tiny_epub: Path) -> None:
    chapters = read_epub(tiny_epub).chapters
    assert resolve(chapters, all_chapters=True) == [2,3]
    assert resolve(chapters, titles=["queue"]) == [2]
    for pattern in ("missing", ""):
        with pytest.raises(ValueError):
            resolve(chapters, titles=[pattern])

def test_partial_navigation_assets_and_links(tiny_epub: Path, tmp_path: Path) -> None:
    source = read_epub(tiny_epub)
    chapter = source.chapters[1]
    output = tmp_path / "partial.epub"
    write_epub(source, [Result(chapter, chapter.body, "佇列")], glossary_xhtml({"queue": "佇列"}), output)
    validate_navigation(output)
    with zipfile.ZipFile(output) as archive:
        body = archive.read(chapter.output_path).decode()
        assert "three.xhtml" not in body and "Next chapter" in body
        for path, content in source.members.items():
            if path.endswith((".png", ".css", ".woff")):
                assert archive.read(path) == content
        assert any(n.endswith("toc.ncx") for n in archive.namelist())
        assert any(n.endswith("nav.xhtml") for n in archive.namelist())

def test_rewrite_selected_anchor(tiny_epub: Path, tmp_path: Path) -> None:
    source = read_epub(tiny_epub)
    results = [Result(c, c.body, c.title) for c in source.chapters[1:]]
    output = tmp_path / "both.epub"
    write_epub(source, results, "<h1>術語表</h1>", output)
    with zipfile.ZipFile(output) as archive:
        assert 'href="chap_003.xhtml#details"' in archive.read(source.chapters[1].output_path).decode()
        assert 'href="chap_002.xhtml#why"' in archive.read(source.chapters[2].output_path).decode()

def test_response_validation() -> None:
    raw = '<section data-role="summary" data-title="队列"><p>软件与吞吐量。</p></section><dl data-role="glossary"><dt>throughput</dt><dd>吞吐量</dd></dl>'
    body, title, _ = parse_response(raw, {})
    assert "軟體" in body and "队" not in title
    with pytest.raises(ValueError, match="conflict"):
        parse_response(raw, {"throughput": "other"})
    with pytest.raises(ValueError):
        parse_response("```" + raw, {})

def test_mocked_pipeline_resume_and_fallback(tiny_epub: Path, tmp_path: Path) -> None:
    source = read_epub(tiny_epub)
    calls: list[dict] = []
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        calls.append(data)
        context = json.loads(data["messages"][1]["content"])
        tokens = [x for x in TOKEN_RE.findall(context["source_xhtml"]) if "IMG" not in x]
        text = '<section data-role="summary" data-title="佇列"><h2>運作原因</h2><p>佇列（queue）吸收突發流量，讓接收端按其速度處理。</p>' + "\n".join(tokens) + '</section><dl data-role="glossary"><dt>queue</dt><dd>佇列</dd></dl>'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    async def run() -> None:
        cache = Cache(tmp_path / "cache.sqlite")
        async with LLMClient(Config(api_key="test"), httpx.MockTransport(handler)) as client:
            results, glossary, failed = await run_book(source, [2,3], client, cache, "test")
            assert failed == [] and glossary == {"queue": "佇列"}
            assert len(soup(results[0].body).find_all("img")) == 1
            assert soup(results[0].body).pre.get_text() == soup(source.chapters[1].body).pre.get_text()
            assert len(calls) == 5
            final_context = json.loads(calls[-1]["messages"][1]["content"])
            assert final_context["glossary"] == {"queue": "佇列"}
            assert final_context["previous_chapter_summary"]
        cache.close()
        cache = Cache(tmp_path / "cache.sqlite")
        async with LLMClient(Config(api_key="test"), httpx.MockTransport(handler)) as client:
            again, terms, failed = await run_book(source, [2,3], client, cache, "test")
            assert len(calls) == 5 and len(again) == 2 and terms == glossary
        cache.close()
    asyncio.run(run())

def test_failure_keeps_original(tiny_epub: Path, tmp_path: Path) -> None:
    source = read_epub(tiny_epub)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad"})
    async def run() -> None:
        cache = Cache(tmp_path / "cache.sqlite")
        async with LLMClient(Config(api_key="test"), httpx.MockTransport(handler)) as client:
            results, _, failed = await run_book(source, [2,3], client, cache, "scope")
            assert failed == [2,3]
            assert results[0].body == source.chapters[1].body
        cache.close()
    asyncio.run(run())


def test_completed_chunk_resume_after_later_failure(tiny_epub: Path, tmp_path: Path) -> None:
    from dataclasses import replace
    source = read_epub(tiny_epub)
    chapter = source.chapters[1]
    body = ''.join(f'<section><h2>Part {n}</h2><p>{"example " * 80}</p></section>' for n in range(3))
    source.chapters[1] = replace(chapter, body=body, images=0)
    calls: list[int] = []
    failing = True
    def handler(request: httpx.Request) -> httpx.Response:
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        part = context["part"]
        calls.append(part)
        if part == 2 and failing:
            return httpx.Response(401, json={"error": "temporary test failure"})
        text = '<section data-role="summary" data-title="範例"><p>範例說明。</p></section><dl data-role="glossary"><dt>example</dt><dd>範例</dd></dl>'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
    async def run() -> None:
        nonlocal failing
        cache = Cache(tmp_path / "partial.sqlite")
        async with LLMClient(Config(api_key="test"), httpx.MockTransport(handler)) as client:
            _, _, failures = await run_book(source, [2], client, cache, "partial", chunk_tokens=300)
            assert failures == [2] and calls == [1, 2]
            # Simulate terminology learned by a later successfully processed chapter.
            cache.complete_chapter("partial", 3, {"body":"<p>完成</p>", "title":"完成"}, {"other": "其他"})
            failing = False
            _, terms, failures = await run_book(source, [2], client, cache, "partial", chunk_tokens=300)
            assert failures == [] and calls == [1, 2, 2, 3]
            assert terms == {"example": "範例", "other": "其他"}
        cache.close()
    asyncio.run(run())
