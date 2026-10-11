# summarize-epub

Python 3.11+ command-line tool for standalone English or Traditional Chinese summaries of
selected English EPUB chapters, with original images, diagrams, code, CSS and fonts.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
# Uncomment ONE provider block and enter its API key.
summarize-epub list book.epub
summarize-epub run book.epub -o preview.epub --chapters 3-7 --preview
summarize-epub run book.epub -o summary.epub --chapters 3-7,9
summarize-epub run book.epub -o summary.en.epub --chapters 3-7,9 --language en
summarize-epub run book.epub -o summary.zh-Hant.epub --chapters 3-7,9 --language zh-Hant
```

`python -m summarize_epub` is equivalent. No selection flags on a terminal opens
interactive selection, a preview option and payment confirmation. Non-interactive
runs require selection flags. `list` and `--dry-run` are offline and need no key;
actual processing fails immediately without `LLM_API_KEY`.

```bash
summarize-epub run book.epub -o out.epub --all --exclude 1,2 --dry-run
summarize-epub run book.epub -o out.epub --chapter-titles "Transactions"
summarize-epub run book.epub -o out.epub --chapters 3- --describe-images
summarize-epub run book.epub -o out.epub --all --mode full
```

## Options

- `--language en|zh-Hant`: English or Traditional Chinese. Defaults to `zh-Hant`
  (or `LLM_LANGUAGE` in the environment). Applies to prose, chapter titles, generated
  image descriptions, glossary, navigation and EPUB language metadata. Protected
  source images, captions and code remain unchanged. Works with both `--mode` values.
- Selection: `--chapters 1,4,9-12`, `--chapter-titles TEXT` (repeatable, unique
  substring), `--all`, `--exclude 1,2`, `--preview`, `--dry-run`.
- Front matter is skipped by default. Use `--no-skip-front-matter` to include it.
- `--mode summary|full`: condense or translate completely.
- `--density tight|balanced|full`: approximately 25%, 30–50%, or 60–70%; understanding
  takes priority over length. Default: balanced.
- `--describe-images`, `--concurrency 4`, `--chunk-tokens 20000`, `--no-cache`,
  `--cache PATH`, `--verbose`.

CLI > environment > defaults. `.env.example` includes OpenAI, Anthropic, Gemini and
DeepSeek blocks. Every `LLM_*` setting has a matching flag: `--api-key`, `--base-url`,
`--model`, `--vision-model`, `--max-tokens`, `--temperature`, `--timeout`,
`--concurrency`, `--input-price`, `--output-price`, `--language`. Prices are USD per million tokens;
zero means unpriced. Estimates exclude retries, images and accumulated context.

## Resume, output and tests

Local Ollama at `http://localhost:11434/v1` (also `127.0.0.1` or `[::1]`)
automatically shows `$0.0000` API cost in the table and final usage report, and
skips the paid-API confirmation. Existing cloud price settings are ignored for
local runs. Explicit chapter selection runs immediately; interactive chapter
selection still works. Keep `LLM_API_KEY=ollama` as the placeholder key.
This detection assumes the standard local endpoint serves local models. Names
containing `cloud`, including an active vision model, retain the remote billing UI.
Other endpoints/ports retain the existing pricing and confirmation behavior.

SQLite saves completed responses, selected indices, glossary and image hashes.
English and Chinese responses, checkpoints, glossaries and generated image captions
use separate cache keys, so changing language cannot resume the other language's output.
Rerun the same command/cache to resume without another confirmation. `--no-cache`
intentionally regenerates text; glossary/image caches persist. Interrupted requests
not yet committed may have been billed remotely. Chapters/chunks run in order for
terminology consistency; image descriptions use asynchronous concurrency.

Output preserves selected source document order, stable chapter filenames, images,
code and original assets. It includes EPUB 3 navigation, NCX, h2 links, and a glossary.
Omitted-chapter links become plain text. Failed chapters keep their English originals
and are reported. Malformed XHTML/protocol responses are retried. Placeholder ordering, duplicates and
missing protected objects are repaired deterministically from source order without another paid call. The editable system prompt lives in `src/summarize_epub/prompts.py`.

```bash
pip install -e '.[test]'
python -m pytest -q
```

Offline tests cover language selection and isolation, preservation, chunking, selection, configuration, navigation,
retry/fallback, vision deduplication and resume. The generated EPUB also reopens with
EbookLib. Internal navigation validation always runs; `epubcheck` runs if on PATH.
Exit codes: 0 completed, 2 chapter failures, 1 setup/output error, 130 interrupted.

Paid provider calls and device readers were not tested. `epubcheck` was unavailable
in the build environment. Preview the language/technical accuracy: structural checks
and Traditional Chinese normalization cannot prove semantic fidelity. Long outputs
may require a higher `--max-tokens`. Oversized indivisible blocks fail safely; DRM
books are unsupported. See [IMPLEMENTATION.md](IMPLEMENTATION.md) for configuration
mapping, architecture, provider references and precise cache/preservation behaviour.

