import asyncio
import json
import zipfile
from dataclasses import replace

import httpx
import pytest

from summarize_epub.cache import Cache
from summarize_epub.chunker import TOKEN_RE, soup
from summarize_epub.cli import parser
from summarize_epub.config import Config
from summarize_epub.epub_io import DC, Result, read_epub, write_epub, xml
from summarize_epub.glossary import glossary_xhtml, parse_response
from summarize_epub.llm_client import LLMClient
from summarize_epub.pipeline import describe_images, run_book
from summarize_epub.prompts import system_prompt, vision_prompt


def test_language_config_and_cli():
    args = parser().parse_args(['run', 'book.epub', '-o', 'out.epub', '--all', '--language', 'en'])
    assert Config.load(vars(args), env={'LLM_LANGUAGE': 'zh-Hant'}, require_key=False).language == 'en'
    assert Config.load({}, env={'LLM_LANGUAGE': 'en'}, require_key=False).language == 'en'
    assert Config.load({}, env={}, require_key=False).language == 'zh-Hant'
    with pytest.raises(SystemExit):
        parser().parse_args(['run', 'book.epub', '-o', 'out.epub', '--language', 'fr'])
    with pytest.raises(ValueError, match='language'):
        Config(language='fr')


def test_language_prompts_and_normalization():
    assert 'English only' in system_prompt('en')
    assert '繁體中文章節標題' not in system_prompt('en')
    assert 'Hong Kong/Taiwan' in system_prompt('zh-Hant')
    assert 'English sentences' in vision_prompt('en')
    raw = '<section data-role="summary" data-title="队列"><p>软件</p></section><dl data-role="glossary"/>'
    # English mode must not run Chinese character conversion on generated text.
    assert parse_response(raw, {}, 'en')[1] == '队列'
    assert parse_response(raw, {}, 'zh-Hant')[1] != '队列'
    assert '<h1>Glossary</h1>' in glossary_xhtml({}, 'en')


@pytest.mark.parametrize('language,title', [('en', 'English summary'), ('zh-Hant', '中文摘要')])
def test_output_language_metadata(tiny_epub, tmp_path, language, title):
    source = read_epub(tiny_epub)
    c = source.chapters[1]
    path = tmp_path / f'{language}.epub'
    write_epub(source, [Result(c, c.body, c.title, False)], glossary_xhtml({}, language), path, language)
    with zipfile.ZipFile(path) as z:
        package = xml(z.read(source.opf_path))
        assert package.find(f'.//{{{DC}}}language').text == language
        assert title in package.find(f'.//{{{DC}}}title').text
        for name in z.namelist():
            if name.endswith('.xhtml'):
                root = xml(z.read(name))
                assert root.get('lang') == language
                assert root.get('{http://www.w3.org/XML/1998/namespace}lang') == language
        if language == 'en':
            assert b'could not be processed' in z.read(c.output_path)
            nav = next(n for n in z.namelist() if n.endswith('nav.xhtml'))
            assert b'Contents' in z.read(nav) and b'Glossary' in z.read(nav)


def test_language_switch_isolates_checkpoints_and_glossary(tiny_epub, tmp_path):
    source = read_epub(tiny_epub)
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        context = json.loads(payload['messages'][1]['content'])
        language = context['language']
        calls.append(language)
        assert context['glossary'] == {}
        title = 'Queues' if language == 'en' else '佇列'
        text = f'<section data-role="summary" data-title="{title}"><p>{title}</p>'
        text += '\n'.join(TOKEN_RE.findall(context['source_xhtml']))
        text += f'</section><dl data-role="glossary"><dt>queue</dt><dd>{title}</dd></dl>'
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})
    async def run():
        cache = Cache(tmp_path / 'shared.sqlite')
        try:
            for language in ['zh-Hant', 'en', 'zh-Hant', 'en']:
                async with LLMClient(Config(api_key='test', language=language), httpx.MockTransport(handler)) as client:
                    results, terms, failed = await run_book(source, [2], client, cache, 'same-scope')
                    assert not failed
                    assert terms['queue'] == ('Queues' if language == 'en' else '佇列')
                    assert soup(results[0].body).pre.get_text() == soup(source.chapters[1].body).pre.get_text()
        finally:
            cache.close()
    asyncio.run(run())
    assert calls == ['zh-Hant', 'en']


def test_caption_cache_language_isolation(tiny_epub, tmp_path):
    source = read_epub(tiny_epub)
    chapter = source.chapters[1]
    calls = []
    def handler(request):
        prompt = json.loads(request.content)['messages'][0]['content'][0]['text']
        english = 'English' in prompt
        calls.append(english)
        text = 'Data flows right.' if english else '資料向右流動。'
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})
    async def run():
        cache = Cache(tmp_path / 'captions.sqlite')
        try:
            for language in ['en', 'zh-Hant', 'en']:
                async with LLMClient(Config(api_key='test', language=language), httpx.MockTransport(handler)) as client:
                    body = await describe_images(chapter.body, chapter, source, client, cache)
                    assert ('AI description: Data flows right.' if language == 'en' else 'AI 圖解：資料向右流動。') in body
        finally:
            cache.close()
    asyncio.run(run())
    assert calls == [True, False]
