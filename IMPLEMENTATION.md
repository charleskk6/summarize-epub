# summarize-epub

Python 3.11+ CLI that summarises selected English EPUB spine documents in
Traditional Chinese, preserving original images, code, CSS and fonts.

## Install and use

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
# Edit .env: uncomment one provider block and set its API key.
summarize-epub list book.epub
summarize-epub run book.epub -o preview.epub --chapters 3-7 --preview
summarize-epub run book.epub -o summary.epub --chapters 3-7,9
```

`python -m summarize_epub` also works. Run without selection flags for an
interactive range/title table, preview choice and one payment confirmation.
Rerunning a confirmed identical selection resumes without another prompt.
Explicit selection is required when stdin is not a TTY.

```bash
summarize-epub run book.epub -o result.epub --all --exclude 1,2 --dry-run
summarize-epub run book.epub -o result.epub --chapter-titles "Transactions"
summarize-epub run book.epub -o result.epub --chapters 3- --describe-images
summarize-epub run book.epub -o result.epub --all --mode full
summarize-epub run book.epub -o result.epub --all --density tight
```

Title patterns are repeatable, case-insensitive substrings; each must match exactly
one document. Index/title selections are combined. Front matter is excluded by
default using semantic types, guide/landmarks and filenames; disable this with
`--no-skip-front-matter`. Indices always match `list`, including excluded documents.
A source spine document is the unit called a chapter; pre-existing split chapters
are intentionally not merged. `list` and `--dry-run` are offline and need no key.
Actual processing fails immediately if `LLM_API_KEY` is absent.

## Options and configuration

CLI overrides environment, which overrides defaults. `.env` is loaded from the
current working directory without overwriting existing environment variables.
Every environment setting has a CLI equivalent:

| Environment | Flag | Default |
|---|---|---|
| `LLM_API_KEY` | `--api-key` | required for processing |
| `LLM_BASE_URL` | `--base-url` | `https://api.openai.com/v1` |
| `LLM_MODEL` | `--model` | `gpt-5` |
| `LLM_VISION_MODEL` | `--vision-model` | same as model |
| `LLM_MAX_TOKENS` | `--max-tokens` | 8192 |
| `LLM_TEMPERATURE` | `--temperature` | 0.3 |
| `LLM_TIMEOUT` | `--timeout` | 180 seconds |
| `LLM_CONCURRENCY` | `--concurrency` | 4 |
| `LLM_INPUT_PRICE` | `--input-price` | 0 (unpriced) |
| `LLM_OUTPUT_PRICE` | `--output-price` | 0 (unpriced) |

`--mode summary` is the default; `full` translates all prose without condensation.
`--density tight|balanced|full` targets approximately 25%, 30–50%, or 60–70% of the
source, with comprehension taking priority. Code and original figures remain
unchanged, so overall file size/word count may not shrink by these ratios.
`--chunk-tokens` defaults to 20000; `--cache` chooses the SQLite path;
`--no-cache` bypasses text/chapter reuse (image-hash cache and terminology persist);
`--verbose` adds diagnostics without HTTP bodies or API keys.

## Preservation, consistency and resume

- Parses with EbookLib and BeautifulSoup's lxml XML parser. No Markdown conversion.
- Sends one generation request per chapter unless it exceeds the approximate
  token limit; splits at headings/structural boundaries with valid XHTML fragments.
  Token counts are a conservative offline heuristic, not provider billing counts.
  Protected code is supplied as read-only reference so the model can understand it.
- Image tokens map to complete original tags and figures. Code, inline identifiers,
  SVG and object blocks are protected too. Missing, duplicate, invented or reordered
  tokens trigger up to three retries. Persistent omissions are restored at chapter
  end with warnings. Invalid XHTML or changed image attributes retain the original
  chapter. Source code text/whitespace and image attributes are preserved; XML
  serialization can change insignificant quote/attribute ordering.
- Optional base64 vision requests add labelled `ai-caption` explanations. Each
  unique image hash is cached; duplicate images are described once. Unsupported
  models/media are skipped and permanent capability failures are negative-cached.
- Chapters and their chunks run sequentially to honour glossary dependencies.
  Independent image descriptions run concurrently under `--concurrency`.
- SQLite WAL commits completed responses immediately, checkpoints each finished chunk, and commits chapter/glossary state
  atomically. Keys hash model + prompt version + serialized chunk request,
  including context/settings; different books/selections/settings get isolated
  chapter/glossary scopes. Glossary and previous summary survive resume.
- Ctrl-C exits 130. Rerun the same command with the same cache to resume. Work
  already committed does not incur another generation call. An interrupted request
  whose response never reached the local cache may still have been billed remotely;
  no generic chat/completions client can guarantee exactly-once billing in that case.
- `--no-cache` deliberately permits repayment. Change `--cache` to start with fresh
  terminology/image descriptions. Do not run simultaneous writers for one book.

The output has stable `chap_003.xhtml` names in each source directory, the original
selected spine order, translated navigation labels, original English headings,
one level of h2 navigation, EPUB 3 nav and NCX, plus a final glossary chapter.
Links to omitted chapters become plain text. Surviving source anchors are retained;
anchors removed by condensation resolve at the chapter start. Images/fonts/CSS
and their original manifest entries are copied unchanged. Original metadata is
retained with the requested title/language/description changes; obsolete document
references are removed. Source signatures are discarded because content changed.

`prompts.py` contains the editable system prompt and version. The model returns
an XHTML summary `<section>` plus an XHTML glossary `<dl>` sidecar in the same
response; the sidecar is parsed, stored and rendered only in the final glossary.
OpenCC normalises generated prose to Traditional Chinese/Taiwan terminology while
leaving code and protected source content untouched. Semantic accuracy, complete
examples, and every terminology choice still require a human preview; validation
cannot establish that an LLM understood a technical argument correctly.

## Validation and errors

```bash
pip install -e '.[test]'
python -m pytest -q
```

Tests cover synthetic EPUB parsing, image/figure/code round-trips, chunking,
selection, config precedence, partial navigation, links/assets, API retries,
vision deduplication, failure preservation and resumed requests. They use mocked
HTTP, so no API key or paid calls are needed.

Every output is checked for manifest resources, resolved TOC files/anchors and
spine reachability. If `epubcheck` is on PATH, it runs and its output is reported.
Output is written atomically. Exit 0 means processing completed, 2 means some
chapters retained their English originals, 1 is a setup/output error, 130 is Ctrl-C.
An epubcheck failure is explicitly reported, even if chapter processing succeeded.

Oversized indivisible tables/paragraphs/code fail safely instead of being cut;
raise `--chunk-tokens` if the model supports the larger input. For long translations,
raise `--max-tokens`; truncated responses are rejected. The full glossary and prior
summary add context beyond the chunk budget. DRM-encrypted books are not supported.
Original malformed EPUB structure/styles may still cause epubcheck warnings.

## Providers

The client only implements the OpenAI-compatible `/chat/completions` protocol,
including generic error-driven negotiation of `max_completion_tokens` and
unsupported temperature settings. There are no provider-name branches. Providers
still differ in model availability, context limits and vision support; set a model
available to your account. Anthropic's compatibility route needs a workspace-scoped
key; multi-workspace keys may require headers outside this CLI's basic configuration.

Endpoint references:
- https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk
- https://ai.google.dev/gemini-api/docs/openai
- https://api-docs.deepseek.com/

The source package is tested offline with mock responses; it has not been exercised
against paid live provider accounts.
