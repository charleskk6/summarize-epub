from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString, Tag


def soup(fragment: str) -> BeautifulSoup:
    return BeautifulSoup("<root>" + fragment + "</root>", "lxml-xml")


def estimate_tokens(text: str) -> int:
    """Offline, conservative multilingual estimate; no tokenizer downloads."""
    ascii_count = sum(ord(c) < 128 for c in text)
    return max(1, (ascii_count + 2) // 3 + (len(text) - ascii_count) * 2)


@dataclass(frozen=True)
class Protected:
    tag: str
    figure: str | None = None
    figure_key: str | None = None


@dataclass(frozen=True)
class Prepared:
    xhtml: str
    mapping: dict[str, Protected]


TOKEN_RE = re.compile(r"\[\[(?:IMG|CODE|MEDIA)_\d+\]\]")


def protect(fragment: str) -> Prepared:
    doc = soup(fragment)
    if TOKEN_RE.search(doc.get_text()):
        raise ValueError("Source contains reserved placeholder tokens")
    mapping: dict[str, Protected] = {}

    # A figure is one atomic protected object, even when it contains several images.
    # This prevents the model from having to preserve/deduplicate one IMG token per
    # image and makes reconstruction deterministic.
    for figure in list(doc.find_all("figure")):
        if figure.parent is None:
            continue
        kind = "IMG" if figure.find("img") is not None else "MEDIA"
        token = f"[[{kind}_{len(mapping) + 1}]]"
        mapping[token] = Protected(str(figure))
        figure.replace_with(NavigableString("\n" + token + "\n"))

    # Images outside figures are protected individually.
    for img in list(doc.find_all("img")):
        token = f"[[IMG_{len(mapping) + 1}]]"
        mapping[token] = Protected(str(img))
        img.replace_with(NavigableString("\n" + token + "\n"))

    for tag in list(doc.find_all(["pre", "svg", "object", "code"])):
        if tag.parent is None or tag.find_parent(["pre", "code", "svg", "object"]):
            continue
        kind = "CODE" if tag.name in {"pre", "code"} else "MEDIA"
        token = f"[[{kind}_{len(mapping) + 1}]]"
        mapping[token] = Protected(str(tag))
        tag.replace_with(NavigableString("\n" + token + "\n"))
    return Prepared("".join(str(x) for x in doc.root.contents), mapping)


def token_error(fragment: str, expected: list[str]) -> str | None:
    found = TOKEN_RE.findall(fragment)
    missing = sorted(set(expected) - set(found))
    invented = sorted(set(found) - set(expected))
    duplicate = sorted({t for t in found if found.count(t) > 1})
    reordered = not missing and not invented and found != expected
    if missing or invented or duplicate or reordered:
        return f"missing={missing}; invented={invented}; duplicates={duplicate}; reordered={reordered}"
    return None


def repair_tokens(fragment: str, expected: list[str]) -> tuple[str, list[str]]:
    """Remove invented/duplicate tokens; defer missing ones to chapter end."""
    seen: set[str] = set()
    def replace(match: re.Match[str]) -> str:
        token = match.group()
        if token not in expected or token in seen:
            return ""
        seen.add(token)
        return token
    return TOKEN_RE.sub(replace, fragment), [t for t in expected if t not in seen]


def canonicalize_tokens(fragment: str, expected: list[str]) -> str:
    """Make model-emitted placeholders deterministic without another API call.

    Placeholder *positions* from the model are retained where possible, but the
    identities are reassigned to source order. Extra/invented occurrences are
    removed and missing source placeholders are appended at the end. Tokens that
    the model placed in attributes are stripped from those attributes because
    protected objects may only be restored from text nodes.
    """
    doc = soup(fragment)

    for tag in doc.find_all(True):
        for key, value in list(tag.attrs.items()):
            if isinstance(value, list):
                raw = " ".join(str(x) for x in value)
            else:
                raw = str(value)
            if not TOKEN_RE.search(raw):
                continue
            cleaned = TOKEN_RE.sub("", raw).strip()
            if cleaned:
                tag.attrs[key] = cleaned
            else:
                del tag.attrs[key]

    used = 0
    for node in list(doc.find_all(string=TOKEN_RE)):
        def replace(_: re.Match[str]) -> str:
            nonlocal used
            if used >= len(expected):
                return ""
            token = expected[used]
            used += 1
            return token
        node.replace_with(NavigableString(TOKEN_RE.sub(replace, str(node))))

    if used < len(expected):
        doc.root.append(NavigableString("\n" + "\n".join(expected[used:]) + "\n"))

    return "".join(str(x) for x in doc.root.contents)


def restore(fragment: str, mapping: dict[str, Protected]) -> str:
    doc = soup(fragment)
    restored_figures: set[str] = set()

    # Restoring an outer protected block can reveal placeholders that were nested
    # inside it when protect() ran (for example an image inside a protected object).
    # Keep resolving until no protected placeholder remains.
    while True:
        nodes = list(doc.find_all(string=TOKEN_RE))
        if not nodes:
            break
        restored_any = False
        for node in nodes:
            pieces = TOKEN_RE.split(str(node))
            tokens = TOKEN_RE.findall(str(node))
            replacements: list[Tag | NavigableString] = []
            for i, piece in enumerate(pieces):
                if piece:
                    replacements.append(NavigableString(piece))
                if i == len(tokens):
                    continue
                token = tokens[i]
                if token not in mapping:
                    raise ValueError(f"Unknown protected placeholder during restore: {token}")
                value = mapping[token]
                if value.figure_key:
                    # Compatibility with caches created by the older per-image
                    # figure protection scheme.
                    parent = node.find_parent("figure")
                    if parent is not None:
                        for caption in list(parent.find_all("figcaption")):
                            caption.decompose()
                        parent.unwrap()
                    if value.figure_key in restored_figures:
                        continue
                    restored_figures.add(value.figure_key)
                original = soup(value.figure or value.tag)
                replacements.extend(list(original.root.contents))
                restored_any = True
            for replacement in replacements:
                node.insert_before(replacement)
            node.extract()
        if not restored_any:
            break

    leftovers = TOKEN_RE.findall("".join(str(x) for x in doc.root.contents))
    if leftovers:
        raise ValueError(f"Protected placeholders remained after restore: {leftovers}")

    # Block-level images/figures may have been placed inside model paragraphs.
    for block in list(doc.find_all(["figure", "pre", "div", "section"])):
        parent = block.find_parent("p")
        if parent is not None:
            parent.unwrap()
    return "".join(str(x) for x in doc.root.contents)

def split_chapter(fragment: str, limit: int = 20_000,
                  references: dict[str, str] | None = None) -> list[str]:
    """Keep XHTML subtrees intact; recursively split oversized structural wrappers.

    Atomic pre/table/paragraph blocks are never cut mid-tag. An oversized atomic
    block is a clear chapter failure rather than silent truncation/data loss.
    """
    def size(text: str) -> int:
        expanded = TOKEN_RE.sub(lambda match: (references or {}).get(match.group(), match.group()), text)
        return estimate_tokens(expanded)

    if size(fragment) <= limit:
        return [fragment]
    doc = soup(fragment)
    def units(node: Tag | NavigableString) -> list[str]:
        text = str(node)
        if size(text) <= limit:
            return [text]
        if not isinstance(node, Tag) or node.name not in {"root", "section", "div", "article", "main", "body"}:
            raise ValueError("An indivisible XHTML block exceeds the chunk limit; raise --chunk-tokens")
        children = list(node.contents)
        if not children:
            return [text]
        parts: list[str] = []
        for child in children:
            parts.extend(units(child))
        # Preserve wrapper semantics/attributes; retain its id on the first part only.
        if node.name != "root":
            wrapped: list[str] = []
            for i, part in enumerate(parts):
                shell = soup(str(node)).root.find(node.name)
                shell.clear()
                if i:
                    shell.attrs.pop("id", None)
                for item in list(soup(part).root.contents):
                    shell.append(item)
                wrapped.append(str(shell))
            return wrapped
        return parts
    blocks = units(doc.root)
    chunks: list[str] = []
    pending = ""
    for block in blocks:
        heading = bool(re.match(r"\s*<h[12](?:\s|>)", block))
        if pending and (size(pending + block) > limit or heading):
            chunks.append(pending)
            pending = ""
        pending += block
    if pending:
        chunks.append(pending)
    return chunks
