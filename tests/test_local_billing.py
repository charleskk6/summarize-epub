import asyncio
import io

import pytest
from rich.console import Console

from summarize_epub.cli import execute, parser, show_table, estimate_cost
from summarize_epub.config import Config
from summarize_epub.epub_io import Result, read_epub


@pytest.mark.parametrize('url', [
    'http://localhost:11434/v1', 'http://127.0.0.1:11434/v1/',
    'http://[::1]:11434/v1',
])
def test_local_endpoints(url):
    assert Config(base_url=url, model='epub-qwen').uses_local_ollama()


@pytest.mark.parametrize('url', [
    'https://api.openai.com/v1', 'https://ollama.com/v1',
    'http://localhost:8000/v1', 'http://localhost.example.com:11434/v1',
    'http://192.168.1.2:11434/v1', 'http://localhost:11434/proxy/v1',
    'http://localhost:bad/v1',
])
def test_other_endpoints_are_not_assumed_free(url):
    assert not Config(base_url=url, model='epub-qwen').uses_local_ollama()


def test_cloud_models_and_active_vision():
    cfg = Config(base_url='http://localhost:11434/v1', model='qwen:cloud')
    assert not cfg.uses_local_ollama()
    cfg = Config(base_url=cfg.base_url, model='epub-qwen', vision_model='vision-cloud')
    assert cfg.uses_local_ollama()
    assert not cfg.uses_local_ollama(describe_images=True)


def test_local_table_zero_overrides_cloud_prices(tiny_epub):
    cfg = Config(base_url='http://localhost:11434/v1', model='epub-qwen',
                 input_price=10, output_price=50)
    chapters = read_epub(tiny_epub).chapters
    assert estimate_cost(chapters[1], cfg) == 0
    output = io.StringIO()
    show_table(Console(file=output, width=120), chapters, cfg)
    text = output.getvalue()
    assert text.count('$0.0000') == len(chapters) + 2
    assert 'n/a' not in text and 'set input/output prices' not in text


@pytest.mark.parametrize('local', [True, False])
def test_interactive_confirmation_and_final_usage(tiny_epub, tmp_path, monkeypatch, local):
    args = parser().parse_args(['run', str(tiny_epub), '-o', str(tmp_path/'out.epub'),
                               '--all', '--cache', str(tmp_path/'cache.sqlite')])
    cfg = Config(api_key='ollama', model='epub-qwen', input_price=10, output_price=50,
                 base_url='http://localhost:11434/v1' if local else 'https://api.openai.com/v1')
    monkeypatch.setattr('summarize_epub.cli.sys.stdin.isatty', lambda: True)
    prompts = []
    def confirm(self, prompt):
        prompts.append(prompt)
        return 'yes'
    monkeypatch.setattr(Console, 'input', confirm)
    class FakeClient:
        input_tokens = 100
        output_tokens = 20
        def __init__(self, config): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def run(source, selected, *args):
        return [Result(source.chapters[i-1], source.chapters[i-1].body,
                       source.chapters[i-1].title) for i in selected], {}, []
    monkeypatch.setattr('summarize_epub.cli.LLMClient', FakeClient)
    monkeypatch.setattr('summarize_epub.cli.run_book', run)
    output = io.StringIO()
    assert asyncio.run(execute(args, cfg, Console(file=output, width=180))) == 0
    if local:
        assert prompts == []
        assert 'API cost $0.0000 (local Ollama)' in output.getvalue()
    else:
        assert len(prompts) == 1 and 'paid API calls' in prompts[0]
        assert 'estimated cost $0.0020' in output.getvalue()


def test_cloud_vision_keeps_confirmation(tiny_epub, tmp_path, monkeypatch):
    cfg = Config(base_url='http://localhost:11434/v1', model='epub-qwen', vision_model='vision:cloud')
    args = parser().parse_args(['run', str(tiny_epub), '-o', str(tmp_path/'out.epub'),
                               '--all', '--describe-images', '--cache', str(tmp_path/'cache.sqlite')])
    monkeypatch.setattr('summarize_epub.cli.sys.stdin.isatty', lambda: True)
    prompts = []
    def decline(self, prompt):
        prompts.append(prompt)
        return 'n'
    monkeypatch.setattr(Console, 'input', decline)
    output = io.StringIO()
    assert asyncio.run(execute(args, cfg, Console(file=output))) == 0
    assert len(prompts) == 1
    assert 'n/a' in output.getvalue() and 'Local Ollama:' not in output.getvalue()
    assert not args.output.exists()
