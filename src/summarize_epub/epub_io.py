from __future__ import annotations

import copy
import hashlib
import logging
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup
from ebooklib import epub
from lxml import etree

from .chunker import soup

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
NCX = "http://www.daisy.org/z3986/2005/ncx/"


def xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))


def serialize(node: etree._Element) -> bytes:
    return etree.tostring(node, encoding="utf-8", xml_declaration=True)


def resolve_path(base: str, reference: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(urlsplit(reference).path)))


def relative(base: str, target: str) -> str:
    return quote(posixpath.relpath(target, posixpath.dirname(base)), safe="/._-")


@dataclass(frozen=True)
class Chapter:
    index: int
    item_id: str
    path: str
    title: str
    body: str
    head: str
    words: int
    images: int
    front_matter: bool
    properties: str = ""
    body_attrs: dict[str, str] | None = None

    @property
    def output_path(self) -> str:
        return posixpath.join(posixpath.dirname(self.path), f"chap_{self.index:03d}.xhtml")


@dataclass
class Source:
    path: Path
    digest: str
    opf_path: str
    package: etree._Element
    members: dict[str, bytes]
    chapters: list[Chapter]
    document_paths: set[str]
    manifest_paths: set[str]


@dataclass(frozen=True)
class Result:
    chapter: Chapter
    body: str
    title: str
    translated: bool = True


def read_epub(path: Path) -> Source:
    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    container = xml(members["META-INF/container.xml"])
    rootfiles = container.xpath("//*[local-name()='rootfile']/@full-path")
    if not rootfiles:
        raise ValueError("EPUB has no package document")
    opf_path = rootfiles[0]
    package = xml(members[opf_path])
    manifest = package.find(f"{{{OPF}}}manifest")
    spine = package.find(f"{{{OPF}}}spine")
    if manifest is None or spine is None:
        raise ValueError("EPUB has no manifest/spine")
    items = {item.get("id"): item for item in manifest}
    documents: set[str] = set()
    manifest_paths: set[str] = set()
    labels: dict[str, str] = {}
    landmarks: dict[str, str] = {}
    for item in manifest:
        p = resolve_path(opf_path, item.get("href", ""))
        manifest_paths.add(p)
        media = item.get("media-type", "")
        if media in {"application/xhtml+xml", "text/html", "application/x-dtbncx+xml"}:
            documents.add(p)
        if "nav" in item.get("properties", "").split():
            nav = BeautifulSoup(members[p], "lxml-xml")
            for a in nav.find_all("a", href=True):
                target = resolve_path(p, a["href"])
                labels.setdefault(target, a.get_text(" ", strip=True))
                types = str(a.get("epub:type", ""))
                if types:
                    landmarks[target] = types
        if media == "application/x-dtbncx+xml":
            nav = BeautifulSoup(members[p], "lxml-xml")
            for point in nav.find_all("navPoint"):
                content, label = point.find("content"), point.find("navLabel")
                if content and label:
                    labels.setdefault(resolve_path(p, content.get("src", "")), label.get_text(" ", strip=True))
    for reference in package.xpath("//*[local-name()='guide']/*[local-name()='reference']"):
        landmarks[resolve_path(opf_path, reference.get("href", ""))] = reference.get("type", "")
    chapters: list[Chapter] = []
    for ref in spine:
        item_id = ref.get("idref")
        item = items.get(item_id)
        if item is None:
            raise ValueError("Unresolved spine item")
        if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
            continue
        source_path = resolve_path(opf_path, item.get("href", ""))
        # Read each spine document through EbookLib, operating directly on XHTML.
        ebook_item = book.get_item_with_id(item_id)
        if ebook_item is None:
            raise ValueError(f"Missing spine document {item_id}")
        doc = BeautifulSoup(ebook_item.content, "lxml-xml")
        body = doc.find("body")
        if body is None:
            raise ValueError(f"Missing XHTML body: {source_path}")
        heading = body.find(["h1", "h2", "title"])
        title = heading.get_text(" ", strip=True) if heading else labels.get(source_path, Path(source_path).stem)
        types = " ".join(str(t.get("epub:type", "")) for t in doc.find_all(True))
        markers = types + " " + landmarks.get(source_path, "")
        front = bool(set(markers.split()) & {"cover", "titlepage", "title-page", "copyright-page", "copyright", "dedication", "toc", "halftitlepage", "frontmatter", "acknowledgments", "colophon"})
        front |= bool(re.search(r"(?:^|[/_.-])(cover|titlepage|title-page|copyright|dedication|toc|contents|frontmatter|acknowledg\w*|colophon)(?:[/_.-]|$)", source_path, re.I))
        front |= "nav" in item.get("properties", "").split()
        chapters.append(Chapter(len(chapters) + 1, item_id, source_path, title,
            "".join(str(x) for x in body.contents), str(doc.head) if doc.head else "<head/>",
            len(re.findall(r"\b[\w'-]+\b", body.get_text(" "))), len(body.find_all("img")),
            front, item.get("properties", ""), dict(body.attrs)))
    if not chapters:
        raise ValueError("EPUB has no readable spine chapters")
    return Source(path, hashlib.sha256(path.read_bytes()).hexdigest(), opf_path, package, members, chapters, documents, manifest_paths)


def ensure_anchors(body: str, original: str, index: int) -> str:
    doc = soup(body)
    existing: set[str] = set()
    for tag in doc.find_all(id=True):
        value = str(tag["id"])
        if value in existing:
            del tag["id"]
        else:
            existing.add(value)
    for n, heading in enumerate(doc.find_all(["h1", "h2"]), 1):
        if not heading.get("id"):
            candidate = f"c{index}-heading-{n}"
            while candidate in existing:
                candidate += "-a"
            heading["id"] = candidate
            existing.add(candidate)
    # Anchors removed by summarisation still resolve to the chapter start.
    for tag in soup(original).find_all(id=True):
        if str(tag["id"]) not in existing:
            anchor = doc.new_tag("span", id=str(tag["id"]))
            doc.root.insert(0, anchor)
            existing.add(str(tag["id"]))
    return "".join(str(x) for x in doc.root.contents)


def rewrite_links(body: str, source_path: str, output_path: str, mapping: dict[str, str],
                  removed: set[str], members: set[str], anchors: dict[str, set[str]]) -> str:
    doc = soup(body)
    for a in list(doc.find_all("a", href=True)):
        href = str(a["href"])
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            continue
        target = resolve_path(source_path, href) if parsed.path else source_path
        if target in mapping:
            mapped = mapping[target]
            fragment = unquote(parsed.fragment)
            suffix = "#" + quote(fragment, safe="-_.:") if fragment in anchors.get(mapped, set()) else ""
            a["href"] = relative(output_path, mapped) + suffix
        elif target in removed or target not in members:
            a.unwrap()
        else:
            a["href"] = relative(output_path, target) + ("#" + parsed.fragment if parsed.fragment else "")
    return "".join(str(x) for x in doc.root.contents)


def document(body: str, title: str, css_href: str, original_head: str = "<head/>",
             body_attrs: dict[str, str] | None = None) -> bytes:
    head = soup(original_head).root.find("head")
    for old in list(head.find_all("title")):
        old.decompose()
    template = soup("<title/> <link/>")
    title_tag = template.find("title")
    title_tag.string = title
    head.append(title_tag)
    link = template.find("link")
    link.attrs = {"rel": "stylesheet", "type": "text/css", "href": css_href}
    head.append(link)
    attrs = dict(body_attrs or {})
    attrs.pop("lang", None)
    attrs.pop("xml:lang", None)
    attr_text = "".join(f' {escape(str(k))}="{escape(str(v), quote=True)}"' for k, v in attrs.items())
    raw = f'<html xmlns="{XHTML}" xmlns:epub="{EPUB}" xmlns:svg="http://www.w3.org/2000/svg" xmlns:m="http://www.w3.org/1998/Math/MathML" xml:lang="zh-Hant" lang="zh-Hant">{head}<body{attr_text}>{body}</body></html>'
    return serialize(xml(raw.encode()))


def write_epub(source: Source, results: list[Result], glossary: str, destination: Path) -> list[str]:
    log = logging.getLogger(__name__)
    package = copy.deepcopy(source.package)
    package.set("version", "3.0")
    metadata = package.find(f"{{{OPF}}}metadata")
    manifest = package.find(f"{{{OPF}}}manifest")
    spine = package.find(f"{{{OPF}}}spine")
    assert metadata is not None and manifest is not None and spine is not None
    reserved_dir = posixpath.join(posixpath.dirname(source.opf_path), "summary-generated")
    if any(p.startswith(reserved_dir + "/") for p in source.members):
        reserved_dir += "-" + source.digest[:12]
    nav_path, ncx_path, css_path, glossary_path = [reserved_dir + "/" + n for n in ("nav.xhtml", "toc.ncx", "captions.css", "glossary.xhtml")]
    mapping = {r.chapter.path: r.chapter.output_path for r in results}
    for out_path in mapping.values():
        if out_path in source.members and out_path not in source.document_paths:
            raise ValueError(f"Generated filename conflicts with an asset: {out_path}")
    bodies: dict[str, str] = {}
    for result in results:
        c = result.chapter
        prefix = f'<h1 class="original-title">{escape(c.title)}</h1>' if result.translated else ""
        if not result.translated:
            prefix = '<p class="translation-warning">此章未能翻譯；以下保留原文。</p>'
        bodies[c.output_path] = ensure_anchors(prefix + result.body, c.body, c.index)
    anchors = {path: {str(x["id"]) for x in soup(body).find_all(id=True)} for path, body in bodies.items()}
    output: dict[str, bytes] = {}
    entries: list[tuple[str, str, list[tuple[str, str]]]] = []
    for result in results:
        c = result.chapter
        body = rewrite_links(bodies[c.output_path], c.path, c.output_path, mapping, source.document_paths, set(source.members), anchors)
        output[c.output_path] = document(body, result.title, relative(c.output_path, css_path), c.head, c.body_attrs)
        subheadings = [(c.output_path + "#" + str(h["id"]), h.get_text(" ", strip=True)) for h in soup(body).find_all("h2")]
        entries.append((c.output_path, result.title + ("（未翻譯）" if not result.translated else ""), subheadings))
    output[glossary_path] = document(glossary, "術語表", relative(glossary_path, css_path))
    entries.append((glossary_path, "術語表", []))
    for item in list(manifest):
        if resolve_path(source.opf_path, item.get("href", "")) in source.document_paths:
            manifest.remove(item)
    asset_ids = {item.get("id") for item in manifest}
    def add_item(uid: str, path: str, media: str, properties: str = "") -> str:
        while uid in asset_ids:
            uid = "summary-" + uid
        asset_ids.add(uid)
        attr = {"id": uid, "href": relative(source.opf_path, path), "media-type": media}
        if properties:
            attr["properties"] = properties
        etree.SubElement(manifest, f"{{{OPF}}}item", **attr)
        return uid
    for child in list(spine):
        spine.remove(child)
    for result in results:
        # Derive live content properties instead of copying nav/scripted flags.
        body = soup(bodies[result.chapter.output_path])
        props = []
        if body.find("svg"):
            props.append("svg")
        if body.find("math"):
            props.append("mathml")
        uid = add_item(f"chap_{result.chapter.index:03d}", result.chapter.output_path, "application/xhtml+xml", " ".join(props))
        etree.SubElement(spine, f"{{{OPF}}}itemref", idref=uid)
    gloss_id = add_item("summary-glossary", glossary_path, "application/xhtml+xml")
    etree.SubElement(spine, f"{{{OPF}}}itemref", idref=gloss_id)
    add_item("summary-nav", nav_path, "application/xhtml+xml", "nav")
    ncx_id = add_item("summary-ncx", ncx_path, "application/x-dtbncx+xml")
    spine.set("toc", ncx_id)
    add_item("summary-css", css_path, "text/css")
    for guide in package.findall(f"{{{OPF}}}guide"):
        package.remove(guide)
    for title in metadata.findall(f"{{{DC}}}title"):
        title.text = (title.text or "") + "（中文摘要）"
    if metadata.find(f"{{{DC}}}title") is None:
        etree.SubElement(metadata, f"{{{DC}}}title").text = "中文摘要"
    for language in metadata.findall(f"{{{DC}}}language"):
        metadata.remove(language)
    etree.SubElement(metadata, f"{{{DC}}}language").text = "zh-Hant"
    etree.SubElement(metadata, f"{{{DC}}}description").text = "Included source chapters: " + "; ".join(f"{r.chapter.index}: {r.chapter.title}" for r in results)
    from datetime import datetime, timezone
    for old in list(metadata):
        if old.get("property") == "dcterms:modified":
            metadata.remove(old)
        elif old.get("refines", "").startswith("#") and old.get("refines")[1:] not in {x.get("id") for x in package.iter()}:
            metadata.remove(old)
    etree.SubElement(metadata, f"{{{OPF}}}meta", property="dcterms:modified").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier_id = package.get("unique-identifier")
    identifiers = metadata.findall(f"{{{DC}}}identifier")
    identifier = next((x.text for x in identifiers if x.get("id") == identifier_id), None)
    if identifier is None:
        identifier = "urn:sha256:" + source.digest
        ident = etree.SubElement(metadata, f"{{{DC}}}identifier", id="summary-identifier")
        ident.text = identifier
        package.set("unique-identifier", "summary-identifier")
    nav = etree.Element(f"{{{XHTML}}}html", nsmap={None: XHTML, "epub": EPUB})
    nav.set("lang", "zh-Hant")
    head = etree.SubElement(nav, f"{{{XHTML}}}head")
    etree.SubElement(head, f"{{{XHTML}}}title").text = "目錄"
    nbody = etree.SubElement(nav, f"{{{XHTML}}}body")
    toc = etree.SubElement(nbody, f"{{{XHTML}}}nav", {f"{{{EPUB}}}type": "toc", "id": "toc"})
    etree.SubElement(toc, f"{{{XHTML}}}h1").text = "目錄"
    ol = etree.SubElement(toc, f"{{{XHTML}}}ol")
    ncx = etree.Element(f"{{{NCX}}}ncx", nsmap={None: NCX}, version="2005-1")
    nhead = etree.SubElement(ncx, f"{{{NCX}}}head")
    for name, content in (("dtb:uid", identifier), ("dtb:depth", "2"), ("dtb:totalPageCount", "0"), ("dtb:maxPageNumber", "0")):
        etree.SubElement(nhead, f"{{{NCX}}}meta", name=name, content=content)
    doc_title = etree.SubElement(ncx, f"{{{NCX}}}docTitle")
    etree.SubElement(doc_title, f"{{{NCX}}}text").text = metadata.find(f"{{{DC}}}title").text
    navmap = etree.SubElement(ncx, f"{{{NCX}}}navMap")
    counter = 0
    def nav_entry(parent: etree._Element, nparent: etree._Element, target: str, label: str) -> etree._Element:
        nonlocal counter
        counter += 1
        path, sep, fragment = target.partition("#")
        suffix = "#" + quote(fragment, safe="-_.:") if sep else ""
        li = etree.SubElement(parent, f"{{{XHTML}}}li")
        etree.SubElement(li, f"{{{XHTML}}}a", href=relative(nav_path, path) + suffix).text = label
        point = etree.SubElement(nparent, f"{{{NCX}}}navPoint", id=f"nav-{counter}", playOrder=str(counter))
        lab = etree.SubElement(point, f"{{{NCX}}}navLabel")
        etree.SubElement(lab, f"{{{NCX}}}text").text = label
        etree.SubElement(point, f"{{{NCX}}}content", src=relative(ncx_path, path) + suffix)
        return point
    for target, label, children in entries:
        point = nav_entry(ol, navmap, target, label)
        if children:
            sub = etree.SubElement(ol[-1], f"{{{XHTML}}}ol")
            for child_target, child_label in children:
                nav_entry(sub, point, child_target, child_label)
    output[nav_path], output[ncx_path] = serialize(nav), serialize(ncx)
    output[css_path] = b'.ai-caption {font-size:.9em;color:#555;line-height:1.5;margin:.5em 0 1em} img{max-width:100%;height:auto}.original-title{font-size:1.35em}.translation-warning{border:1px solid;padding:.5em} pre{white-space:pre-wrap} dt{font-weight:bold} dd{margin-bottom:.7em}'
    output[source.opf_path] = serialize(package)
    # Every original asset and its manifest entry stays byte-for-byte unchanged.
    output.update({p: data for p, data in source.members.items() if p not in source.document_paths and p not in output and p != "META-INF/signatures.xml"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".epub", delete=False) as handle:
        temp = Path(handle.name)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for path, data in output.items():
                if path != "mimetype":
                    archive.writestr(path, data)
        validate_navigation(temp)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    checker = shutil.which("epubcheck")
    report: list[str] = []
    if checker:
        try:
            check = subprocess.run([checker, str(destination)], capture_output=True, text=True, timeout=180)
            report.append(f"epubcheck exit={check.returncode}\n{check.stdout}\n{check.stderr}")
            if check.returncode:
                log.warning("epubcheck reported problems; see validation report")
        except subprocess.TimeoutExpired:
            report.append("epubcheck timed out after 180 seconds")
    else:
        report.append("epubcheck not installed; internal navigation validation passed")
    return report


def validate_navigation(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        container = xml(archive.read("META-INF/container.xml"))
        opf_path = container.xpath("//*[local-name()='rootfile']/@full-path")[0]
        opf = xml(archive.read(opf_path))
        items = {x.get("id"): x for x in opf.find(f"{{{OPF}}}manifest")}
        nav_item = next(x for x in items.values() if "nav" in x.get("properties", "").split())
        nav_path = resolve_path(opf_path, nav_item.get("href"))
        nav = xml(archive.read(nav_path))
        reachable: set[str] = set()
        for href in nav.xpath("//*[local-name()='nav' and @epub:type='toc']//*[local-name()='a']/@href", namespaces={"epub": EPUB}):
            target = resolve_path(nav_path, href)
            if target not in names:
                raise ValueError(f"Nav target does not exist: {target}")
            reachable.add(target)
            fragment = unquote(urlsplit(href).fragment)
            if fragment and fragment not in xml(archive.read(target)).xpath("//@id"):
                raise ValueError(f"Nav anchor does not exist: {href}")
        for ref in opf.find(f"{{{OPF}}}spine"):
            target = resolve_path(opf_path, items[ref.get("idref")].get("href"))
            if target not in reachable:
                raise ValueError(f"Spine document is not reachable from TOC: {target}")
        for item in items.values():
            if resolve_path(opf_path, item.get("href", "")) not in names:
                raise ValueError(f"Missing manifest resource: {item.get('href')}")
