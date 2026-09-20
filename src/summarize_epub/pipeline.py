from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
from typing import Any, Callable
from urllib.parse import urlsplit

from .cache import Cache, cache_key
from .chunker import TOKEN_RE, protect, repair_tokens, restore, soup, split_chapter, token_error
from .epub_io import Chapter, Result, Source, resolve_path
from .glossary import parse_response, traditional
from .llm_client import LLMClient, LLMError
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, VISION_PROMPT


async def describe_images(body: str, chapter: Chapter, source: Source, client: LLMClient, cache: Cache) -> str:
    log = logging.getLogger(__name__)
    doc = soup(body)
    jobs: dict[str, tuple[bytes, str]] = {}
    images: list[tuple[Any, str]] = []
    for img in doc.find_all("img"):
        src = str(img.get("src", ""))
        if urlsplit(src).scheme or urlsplit(src).netloc:
            log.warning("Skipping non-embedded image in chapter %s", chapter.index)
            continue
        path = resolve_path(chapter.path, src)
        data = source.members.get(path)
        if data is None:
            log.warning("Image bytes unavailable in chapter %s", chapter.index)
            continue
        digest = hashlib.sha256(data).hexdigest()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        jobs[digest] = (data, mime)
        images.append((img, digest))

    async def describe(digest: str, data: bytes, mime: str) -> tuple[str, str]:
        hit = cache.caption(digest)
        if hit is not None:
            return digest, hit
        content: list[dict[str, Any]] = [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode()}},
        ]
        try:
            value = await client.complete([{"role": "user", "content": content}], client.config.vision_model)
            # Treat generated captions as text; never interpret returned markup.
            from opencc import OpenCC
            value = OpenCC("s2twp").convert(value.strip())
        except LLMError as error:
            log.warning("Image description skipped: %s", error)
            # Permanent unsupported-media/model responses are negative-cached.
            if error.status in {400, 404, 415, 422}:
                cache.save_caption(digest, "")
            return digest, ""
        cache.save_caption(digest, value)
        return digest, value

    descriptions = dict(await asyncio.gather(*(describe(d, *job) for d, job in jobs.items())))
    for img, digest in images:
        description = descriptions[digest]
        if not description:
            continue
        figure = img.find_parent("figure")
        if figure is None:
            figure = doc.new_tag("figure")
            img.wrap(figure)
        caption = figure.find("figcaption", recursive=False)
        if caption is None:
            caption = doc.new_tag("figcaption", attrs={"class": "ai-caption"})
            figure.append(caption)
        else:
            classes = str(caption.get("class", "")).split()
            caption["class"] = " ".join(dict.fromkeys(classes + ["ai-caption"]))
        paragraph = doc.new_tag("p", attrs={"class": "ai-caption"})
        paragraph.string = "AI 圖解：" + description
        caption.append(paragraph)
    return "".join(str(x) for x in doc.root.contents)


async def process_chapter(chapter: Chapter, source: Source, client: LLMClient, cache: Cache,
                          terms: dict[str, str], previous: str, mode: str, density: str,
                          chunk_tokens: int, describe: bool, scope: str) -> tuple[Result, dict[str, str]]:
    log = logging.getLogger(__name__)
    prepared = protect(chapter.body)
    references = {token: value.tag for token, value in prepared.mapping.items()}
    chunks = split_chapter(prepared.xhtml, chunk_tokens, references)
    collected: list[str] = []
    missing_at_end: list[str] = []
    chapter_terms = dict(terms)
    title = chapter.title
    for number, chunk in enumerate(chunks, 1):
        checkpoint = cache.chunk(scope, chapter.index, number)
        if checkpoint is not None:
            collected.append(checkpoint["body"])
            missing_at_end.extend(checkpoint["missing"])
            chapter_terms.update(checkpoint["terms"])
            if number == 1:
                title = checkpoint["title"]
            continue
        missing: list[str] = []
        expected = TOKEN_RE.findall(chunk)
        context = {"mode": mode, "density": density, "chapter_title": chapter.title,
                   "part": number, "parts": len(chunks), "glossary": chapter_terms,
                   "previous_chapter_summary": previous[:12_000],
                   "previous_part_summary": "".join(collected)[-6000:],
                   "source_xhtml": chunk,
                   "protected_reference_xhtml_read_only": {token: references[token] for token in expected}}
        payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": payload}]
        last_valid: tuple[str, str, dict[str, str]] | None = None
        for attempt in range(4):  # Initial attempt plus up to THREE validation retries.
            envelope = json.dumps({"messages": messages, "validation_attempt": attempt, "endpoint": client.config.base_url,
                "max_tokens": client.config.max_tokens, "temperature": client.config.temperature}, ensure_ascii=False, sort_keys=True)
            key = cache_key(client.config.model, PROMPT_VERSION, envelope)
            response = cache.get_response(key)
            if response is None:
                response = await client.complete(messages)
                # Commit before parsing, so even an interrupted validation can resume.
                cache.put_response(key, response)
            try:
                body, new_title, new_terms = parse_response(response, chapter_terms)
                last_valid = body, new_title, new_terms
                error = token_error(body, expected)
                if error:
                    raise ValueError("Placeholder validation: " + error)
                break
            except ValueError as error:
                log.warning("Chapter %s part %s validation attempt %s: %s", chapter.index, number, attempt + 1, error)
                if attempt == 3:
                    if last_valid is None:
                        raise ValueError("No valid XHTML/glossary after three retries") from None
                    body, new_title, new_terms = last_valid
                    body, missing = repair_tokens(body, expected)
                    missing_at_end.extend(missing)
                    log.warning("Chapter %s: placeholder fallback; appending %s missing objects at chapter end", chapter.index, len(missing))
                    break
                messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": payload},
                            {"role": "assistant", "content": response},
                            {"role": "user", "content": f"Repair the complete response. {error}. Expected placeholders in order: {expected}. Return both XHTML siblings."}]
        if number == 1:
            title = new_title
        chapter_terms.update(new_terms)
        collected.append(body)
        cache.complete_chunk(scope, chapter.index, number,
                             {"body": body, "title": new_title, "terms": new_terms, "missing": missing}, new_terms)
    combined = "\n".join(collected + missing_at_end)
    combined = restore(combined, prepared.mapping)
    original_images = soup(chapter.body).find_all("img")
    output_images = soup(combined).find_all("img")
    if len(output_images) != len(original_images):
        raise ValueError("Restored image count differs from source; retaining original chapter")
    # Compare full attribute multisets, not just image counts.
    def attrs(images: list[Any]) -> list[str]:
        return sorted(json.dumps(img.attrs, sort_keys=True) for img in images)
    if attrs(original_images) != attrs(output_images):
        raise ValueError("Restored image attributes differ from source")
    if describe:
        combined = await describe_images(combined, chapter, source, client, cache)
    return Result(chapter, combined, title), chapter_terms


async def run_book(source: Source, selected: list[int], client: LLMClient, cache: Cache,
                   scope: str, mode: str = "summary", density: str = "balanced",
                   chunk_tokens: int = 20_000, describe: bool = False,
                   progress: Callable[[int], None] | None = None) -> tuple[list[Result], dict[str, str], list[int]]:
    log = logging.getLogger(__name__)
    terms = cache.load_glossary(scope)
    previous = ""
    results: list[Result] = []
    failed: list[int] = []
    for index in selected:
        chapter = source.chapters[index - 1]
        saved = cache.chapter(scope, index)
        if saved:
            result = Result(chapter, saved["body"], saved["title"])
            log.info("Chapter %s restored from cache", index)
        else:
            try:
                result, updated = await process_chapter(chapter, source, client, cache, terms, previous,
                                                        mode, density, chunk_tokens, describe, scope)
                cache.complete_chapter(scope, index, {"body": result.body, "title": result.title}, updated)
                terms = updated
            except (Exception,) as error:
                # KeyboardInterrupt/CancelledError inherit BaseException and must propagate.
                log.warning("Chapter %s left untranslated: %s", index, type(error).__name__)
                if isinstance(error, (ValueError, LLMError)):
                    log.warning("Chapter %s reason: %s", index, error)
                result = Result(chapter, chapter.body, chapter.title, False)
                failed.append(index)
                terms = cache.load_glossary(scope)
        results.append(result)
        if result.translated:
            previous = soup(result.body).get_text(" ", strip=True)[:12_000]
        if progress:
            progress(index)
    return results, terms, failed
