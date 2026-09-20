from __future__ import annotations

from html import escape

from lxml import etree
from opencc import OpenCC

from .chunker import soup


def traditional(fragment: str) -> str:
    """Normalize generated prose only, never original protected code/figures."""
    converter = OpenCC("s2twp")
    doc = soup(fragment)
    for node in list(doc.find_all(string=True)):
        if not node.find_parent(["code", "pre"]):
            node.replace_with(converter.convert(str(node)))
    for tag in doc.find_all(attrs={"data-title": True}):
        tag["data-title"] = converter.convert(tag["data-title"])
    return "".join(str(x) for x in doc.root.contents)


def parse_response(response: str, existing: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    if "<!DOCTYPE" in response.upper() or "<!ENTITY" in response.upper():
        raise ValueError("DTD/entities are forbidden in model output")
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(("<root>" + response + "</root>").encode(), parser)
    except etree.XMLSyntaxError:
        raise ValueError("Return a well-formed XHTML fragment, without Markdown or preamble") from None
    if len(root) != 2 or (root.text or "").strip() or any((x.tail or "").strip() for x in root):
        raise ValueError("Expected only summary section and glossary dl")
    doc = soup(traditional(response))
    summary = doc.root.find("section", attrs={"data-role": "summary"}, recursive=False)
    glossary = doc.root.find("dl", attrs={"data-role": "glossary"}, recursive=False)
    if summary is None or glossary is None or not summary.get("data-title"):
        raise ValueError("Missing summary section, data-title or glossary dl")
    if summary.find(["script", "style", "img", "svg", "object", "iframe", "link"]):
        raise ValueError("Generated executable content/images are forbidden; use placeholders")
    for tag in summary.find_all(True):
        if any(str(k).lower().startswith("on") for k in tag.attrs):
            raise ValueError("Event handlers are forbidden")
        if str(tag.get("href", "")).lower().startswith(("javascript:", "data:")):
            raise ValueError("Unsafe href")
    terms: dict[str, str] = {}
    entries = glossary.find_all(["dt", "dd"], recursive=False)
    if len(entries) % 2:
        raise ValueError("Glossary must have dt/dd pairs")
    for a, b in zip(entries[::2], entries[1::2]):
        if a.name != "dt" or b.name != "dd":
            raise ValueError("Glossary must alternate dt and dd")
        term, rendering = a.get_text().strip(), b.get_text().strip()
        if not term or not rendering:
            raise ValueError("Empty glossary entry")
        if term in existing and existing[term] != rendering:
            raise ValueError(f"Glossary conflict: {term!r} must be {existing[term]!r}")
        terms[term] = rendering
    return "".join(str(x) for x in summary.contents), str(summary["data-title"]), terms


def glossary_xhtml(terms: dict[str, str]) -> str:
    rows = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in sorted(terms.items()))
    return "<h1>術語表</h1><dl>" + rows + "</dl>"
