from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from .cache import Cache
from .chunker import estimate_tokens
from .config import Config
from .epub_io import Chapter, read_epub, write_epub
from .glossary import glossary_xhtml
from .llm_client import LLMClient
from .pipeline import run_book
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .selection import resolve


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"level": record.levelname, "logger": record.name, "message": record.getMessage()}, ensure_ascii=False)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Traditional Chinese summaries with original EPUB images")
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("list", "run"):
        cmd = sub.add_parser(command)
        cmd.add_argument("input", type=Path)
        for name, kind in (("api-key", str), ("base-url", str), ("model", str), ("vision-model", str),
                           ("max-tokens", int), ("temperature", float), ("timeout", float),
                           ("concurrency", int), ("input-price", float), ("output-price", float)):
            cmd.add_argument("--" + name, type=kind, default=None)
        cmd.add_argument("--verbose", action="store_true")
        if command == "list":
            continue
        cmd.add_argument("-o", "--output", type=Path, required=True)
        cmd.add_argument("--chapters")
        cmd.add_argument("--chapter-titles", action="append", default=[])
        cmd.add_argument("--all", action="store_true")
        cmd.add_argument("--exclude")
        cmd.add_argument("--skip-front-matter", action=argparse.BooleanOptionalAction, default=True)
        cmd.add_argument("--dry-run", action="store_true")
        cmd.add_argument("--preview", action="store_true", help="Process only the first selected chapter")
        cmd.add_argument("--describe-images", action="store_true")
        cmd.add_argument("--mode", choices=("summary", "full"), default="summary")
        cmd.add_argument("--density", choices=("tight", "balanced", "full"), default="balanced")
        cmd.add_argument("--no-cache", action="store_true")
        cmd.add_argument("--cache", type=Path, default=Path(".summarize-epub.sqlite3"))
        cmd.add_argument("--chunk-tokens", type=int, default=20_000)
    return root


def estimate_cost(chapter: Chapter, cfg: Config, density: str = "balanced", mode: str = "summary") -> float:
    ratio = 1 if mode == "full" else {"tight": .25, "balanced": .5, "full": .7}[density]
    incoming = estimate_tokens(chapter.body) + estimate_tokens(SYSTEM_PROMPT)
    # Chinese output tokenization differs materially from English.
    outgoing = estimate_tokens(chapter.body) * ratio * 2
    return (incoming * cfg.input_price + outgoing * cfg.output_price) / 1_000_000


def show_table(console: Console, chapters: list[Chapter], cfg: Config, density: str = "balanced", mode: str = "summary") -> None:
    table = Table("Index", "Chapter title", "Words", "Images", "Est. USD")
    priced = bool(cfg.input_price or cfg.output_price)
    total = 0.0
    for chapter in chapters:
        cost = estimate_cost(chapter, cfg, density, mode)
        total += cost
        table.add_row(str(chapter.index), chapter.title + (" [front matter]" if chapter.front_matter else ""),
                      str(chapter.words), str(chapter.images), f"${cost:.4f}" if priced else "n/a")
    table.add_row("TOTAL", f"{len(chapters)} documents", str(sum(c.words for c in chapters)),
                  str(sum(c.images for c in chapters)), f"${total:.4f}" if priced else "n/a", style="bold")
    console.print(table)
    console.print("Estimates exclude retries, image descriptions and accumulated context; set input/output prices for USD estimates.")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def execute(args: argparse.Namespace, cfg: Config, console: Console) -> int:
    source = read_epub(args.input)
    if args.command == "list":
        show_table(console, source.chapters, cfg)
        return 0
    if args.input.resolve() == args.output.resolve():
        raise ValueError("Output must differ from input")
    if args.chunk_tokens < 100:
        raise ValueError("--chunk-tokens must be at least 100")
    if args.output.resolve() == args.cache.resolve() or args.input.resolve() == args.cache.resolve():
        raise ValueError("Cache must differ from input/output")
    cache = Cache(args.cache, not args.no_cache)
    try:
        flags = {k: getattr(args, k) for k in ("chapters", "chapter_titles", "all", "exclude", "skip_front_matter", "preview")}
        selection_key = digest({"source": source.digest, "flags": flags})
        stored = cache.selection(selection_key)
        explicit = bool(args.chapters or args.chapter_titles or args.all)
        interactive = sys.stdin.isatty()
        preview = args.preview
        if stored is not None:
            selected = stored
            console.print("Resuming saved chapter selection.")
        elif not explicit:
            if not interactive:
                raise ValueError("Non-interactive run needs --chapters, --chapter-titles, or --all")
            show_table(console, source.chapters, cfg, args.density, args.mode)
            expression = console.input("Chapters (e.g. 3-7, 1,4,9-12, 3-, all): ")
            selected = resolve(source.chapters, expression, exclude=args.exclude, skip_front_matter=args.skip_front_matter)
            choice = console.input("Enter for all selected, or 'preview' for the first selected chapter: ").strip().lower()
            if choice not in {"", "preview"}:
                raise ValueError("Expected Enter or preview")
            preview = choice == "preview" or preview
        else:
            selected = resolve(source.chapters, args.chapters, args.chapter_titles, args.all,
                               args.exclude, args.skip_front_matter)
        if preview:
            selected = selected[:1]
        console.print("Will process source indices: " + ", ".join(map(str, selected)))
        show_table(console, [source.chapters[i - 1] for i in selected], cfg, args.density, args.mode)
        if args.dry_run:
            console.print("Dry run: no API calls made.")
            return 0
        settings = {"source": source.digest, "indices": selected, "model": cfg.model, "endpoint": cfg.base_url,
                    "vision_model": cfg.vision_model, "max_tokens": cfg.max_tokens, "temperature": cfg.temperature,
                    "mode": args.mode, "density": args.density, "describe": args.describe_images,
                    "chunk_tokens": args.chunk_tokens, "prompt": PROMPT_VERSION, "system": SYSTEM_PROMPT}
        scope = digest(settings)
        # Saved selection means this run has already been explicitly confirmed.
        confirmation_key = "confirmed:" + scope
        confirmed = cache.selection(confirmation_key) is not None and not args.no_cache
        if interactive and not confirmed:
            if console.input("Proceed with paid API calls? [y/N]: ").strip().lower() not in {"y", "yes"}:
                console.print("Cancelled; no API calls made.")
                return 0
        cache.save_selection(selection_key, selected)
        cache.save_selection(confirmation_key, selected)
        async with LLMClient(cfg) as client:
            with Progress(console=console) as progress:
                task = progress.add_task("Processing chapters", total=len(selected))
                results, terms, failures = await run_book(source, selected, client, cache, scope,
                    args.mode, args.density, args.chunk_tokens, args.describe_images,
                    lambda _: progress.advance(task))
            reports = write_epub(source, results, glossary_xhtml(terms), args.output)
            for report in reports:
                console.print(report, markup=False)
            console.print(f"Wrote {args.output}", markup=False)
            cost = (client.input_tokens * cfg.input_price + client.output_tokens * cfg.output_price) / 1_000_000
            console.print(f"This invocation: {client.input_tokens} input / {client.output_tokens} output tokens reported by API; estimated cost ${cost:.4f}" if cfg.input_price or cfg.output_price else f"This invocation: {client.input_tokens} input / {client.output_tokens} output tokens reported by API; cost unpriced.")
            if failures:
                console.print("Kept original content for failed chapters: " + ", ".join(map(str, failures)))
                return 2
            return 0
    finally:
        cache.close()


def main() -> None:
    args = parser().parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler], force=True)
    # Never enable HTTP wire logs, even with --verbose.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    console = Console(highlight=False)
    try:
        cfg = Config.load(vars(args), require_key=args.command == "run" and not args.dry_run)
        status = asyncio.run(execute(args, cfg, console))
    except KeyboardInterrupt:
        console.print("Interrupted. Completed work is saved; rerun the same command to resume.")
        status = 130
    except Exception as error:
        # Only controlled configuration/selection errors are displayed verbatim.
        console.print(f"Error: {error}" if isinstance(error, (ValueError, FileNotFoundError)) else f"Error: {type(error).__name__}; check input EPUB and output permissions", markup=False)
        status = 1
    raise SystemExit(status)
