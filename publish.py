#!/usr/bin/env python3
"""Build the static site in public/ from a public Google Doc, and optionally deploy it.

The doc is downloaded as HTML rather than Markdown on purpose: Google's Markdown
export drops the heading anchor ids, so the internal links (which export as
"#heading=h.xxxx") become impossible to resolve.  The HTML export keeps both
sides of that mapping, which is what lets step "fix internal links" work.

Usage:
    python3 publish.py                 # build into public/
    python3 publish.py --deploy        # build, then firebase deploy --only hosting
    python3 publish.py --cache doc.html  # reuse/save a local copy instead of refetching
"""

from __future__ import annotations

import argparse
import copy
import datetime
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOC_ID = "19rhgYc4RfUE-IcvCfwe0BVcHOl8mNF0JT4k8v-LZhE4"
DOC_VIEW_URL = f"https://docs.google.com/document/d/{DOC_ID}/"
DOC_EXPORT_URL = f"https://docs.google.com/document/d/{DOC_ID}/export?format=html"

# Markers in the document.  The header block (everything between the two header
# markers, inclusive) is dropped; the TOC marker is replaced by the front-page
# table of contents.  Matching ignores case and surrounding whitespace.
HEADER_START_MARKERS = ("<<<HEADER>>>",)
HEADER_END_MARKERS = ("<<<END HEADER>>>", "<<</HEADER>>>")
TOC_MARKERS = ("<<<TOC>>>",)

# Front page lists every other page plus their headings down to this level;
# each section page gets a TOC of its own headings in this range.
FRONT_TOC_MAX_LEVEL = 3          # h2 and h3 under each page link
SECTION_TOC_LEVELS = (2, 5)      # h2 through h5

# Hand-written pages: (output file, <h1>/<title>, fragment in site/).  Each fragment is
# run through template processing too, so it can use {{doc_url}}.
STATIC_PAGES = (
    ("about.html", "About", "about.html"),
    ("404.html", "Page not found", "404.html"),
)

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(ROOT, "site")
OUT_DIR = os.path.join(ROOT, "public")
IMAGE_SUBDIR = "images"

# Files in public/ that publish.py owns and may delete before a build.
GENERATED_SUFFIXES = (".html", ".css")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def slugify(text: str, fallback: str = "section") -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:60].strip("-") or fallback


def inline_text(el: Tag) -> str:
    """Readable text of an inline-only element.

    Google splits a heading into several <span>s and sometimes gives whitespace a
    span of its own, so neither get_text(" ") nor strip=True gets the spacing
    right: the first invents spaces inside words, the second deletes the real ones.
    """
    text = el.get_text("").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def inline_text_from_nodes(nodes: list) -> str:
    """Readable plain text for a list of detached inline nodes (footnote tooltips)."""
    parts = []
    for node in nodes:
        parts.append(node if isinstance(node, NavigableString) else node.get_text(""))
    text = "".join(str(part) for part in parts).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_marker_text(text: str) -> str:
    """Collapse whitespace and undo Google Docs' smart punctuation."""
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def matches_marker(text: str, markers: tuple[str, ...]) -> bool:
    norm = normalize_marker_text(text).upper()
    return any(norm == m.upper() for m in markers)


def contains_marker(text: str, markers: tuple[str, ...]) -> str | None:
    norm = normalize_marker_text(text).upper()
    for m in markers:
        if m.upper() in norm:
            return m
    return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def fetch_doc(cache: str | None, refetch: bool) -> str:
    if cache and os.path.exists(cache) and not refetch:
        log(f"using cached export: {cache}")
        with open(cache, encoding="utf-8") as fh:
            return fh.read()
    log(f"downloading {DOC_EXPORT_URL}")
    resp = requests.get(DOC_EXPORT_URL, timeout=60)
    resp.raise_for_status()
    if "docs.google.com/document" in resp.url and "ServiceLogin" in resp.url:
        raise SystemExit("error: the document is not publicly readable (got a login page)")
    resp.encoding = resp.encoding or "utf-8"
    html = resp.text
    if cache:
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write(html)
        log(f"saved export to {cache}")
    return html


# ---------------------------------------------------------------------------
# Cleanup / normalization of the exported HTML
# ---------------------------------------------------------------------------

def parse_style_classes(soup: BeautifulSoup) -> dict[str, set[str]]:
    """Map Google's generated class names to the formatting they stand for.

    The export puts all character formatting in a <style> block (".c9{font-weight:700}"),
    so recovering bold/italic/etc. means reading those rules.
    """
    styles: dict[str, set[str]] = {}
    for style in soup.find_all("style"):
        css = style.get_text()
        for selector, body in re.findall(r"\.([A-Za-z0-9_-]+)\s*\{([^}]*)\}", css):
            marks = set()
            if re.search(r"font-weight:\s*(bold|[6-9]00)", body):
                marks.add("strong")
            if re.search(r"font-style:\s*italic", body):
                marks.add("em")
            if re.search(r"text-decoration[^;]*:[^;]*line-through", body):
                marks.add("s")
            if re.search(r"text-decoration[^;]*:[^;]*underline", body):
                marks.add("u")
            if re.search(r"vertical-align:\s*super", body):
                marks.add("sup")
            if re.search(r"vertical-align:\s*sub", body):
                marks.add("sub")
            if marks:
                styles.setdefault(selector, set()).update(marks)
    return styles


def strip_comments(soup: BeautifulSoup) -> int:
    """Remove Google Docs comments: the trailing blocks and the inline [a] refs.

    Comments really are present in the HTML export, and one of them has sat inside a
    heading, so this has to happen before any heading text is read.  The blocks go
    first: each one is found by its own back-link (<a href="#cmnt_ref1" id="cmnt1">),
    which the inline-ref pass below would otherwise delete out from under us, leaving
    the comment text stranded at the end of the last page.
    """
    removed = 0
    for anchor in soup.find_all("a", id=True):
        if re.match(r"^cmnt\d+$", anchor["id"]):
            block = anchor.find_parent("div") or anchor.find_parent("p") or anchor
            block.decompose()
            removed += 1
    for anchor in soup.find_all("a", href=True):
        if re.match(r"^#cmnt", anchor["href"]):
            target = anchor.find_parent("sup") or anchor
            target.decompose()
            removed += 1
    return removed


def unwrap_redirect(url: str) -> str:
    """Turn https://www.google.com/url?q=REAL&sa=D&... back into REAL."""
    if not url.startswith(("https://www.google.com/url", "http://www.google.com/url")):
        return url
    query = urllib.parse.urlparse(url).query
    target = urllib.parse.parse_qs(query).get("q", [None])[0]
    return target or url


def apply_marks(soup: BeautifulSoup, styles: dict[str, set[str]]) -> None:
    """Replace class-based formatting with real <strong>/<em>/<u>/<s> tags."""
    order = ["s", "u", "em", "strong", "sup", "sub"]
    for span in soup.find_all("span"):
        classes = span.get("class") or []
        marks = set()
        for cls in classes:
            marks |= styles.get(cls, set())
        # A link already looks like a link; don't underline it twice.
        if span.find("a") or span.find_parent("a"):
            marks.discard("u")
        # Headings are already bold/large; don't nest <strong> inside them.
        heading = span.find_parent(re.compile(r"^h[1-6]$"))
        if heading is not None:
            marks -= {"strong", "u"}
        for mark in order:
            if mark in marks:
                wrapper = soup.new_tag(mark)
                span.wrap(wrapper)
        span.unwrap()


def clean_attrs(soup: BeautifulSoup) -> None:
    keep = {
        "a": {"href", "id", "name", "title"},
        "img": {"src", "alt", "title", "width", "height"},
        "ol": {"start", "type"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan"},
    }
    for tag in soup.find_all(True):
        allowed = keep.get(tag.name, set()) | {"id"}
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]


def drop_empty_blocks(container: Tag) -> None:
    """Google pads the document with empty <p><span></span></p> spacers."""
    for tag in container.find_all(["p", "span", "div"]):
        if tag.find(["img", "br", "hr", "table"]):
            continue
        if not tag.get_text(strip=True):
            tag.decompose()


def download_images(soup: BeautifulSoup, out_dir: str) -> None:
    images = [img for img in soup.find_all("img") if img.get("src")]
    if not images:
        return
    img_dir = os.path.join(out_dir, IMAGE_SUBDIR)
    os.makedirs(img_dir, exist_ok=True)
    for index, img in enumerate(images, start=1):
        src = img["src"]
        if src.startswith("data:"):
            continue
        try:
            resp = requests.get(src, timeout=60)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a missing image shouldn't fail the build
            warn(f"could not download image {src}: {exc}")
            continue
        ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
        }.get(resp.headers.get("content-type", "").split(";")[0].strip(), ".img")
        name = f"image{index}{ext}"
        with open(os.path.join(img_dir, name), "wb") as fh:
            fh.write(resp.content)
        img["src"] = f"/{IMAGE_SUBDIR}/{name}"
        img.setdefault("alt", "")
        log(f"downloaded image {name}")


def remove_header_block(children: list[Tag]) -> list[Tag]:
    """Drop the document's private header, delimited by the HEADER markers."""
    start = end = None
    for index, node in enumerate(children):
        text = node.get_text()
        if start is None and contains_marker(text, HEADER_START_MARKERS):
            start = index
        elif start is not None and contains_marker(text, HEADER_END_MARKERS):
            end = index
            break
    if start is None:
        warn(
            "no header block found (looked for "
            f"{HEADER_START_MARKERS[0]} … {HEADER_END_MARKERS[0]}); publishing the whole document"
        )
        return children
    if end is None:
        raise SystemExit(
            f"error: found {HEADER_START_MARKERS[0]} but no closing "
            f"{HEADER_END_MARKERS[0]} in the document"
        )
    log(f"removed header block ({end - start + 1} blocks)")
    return children[:start] + children[end + 1:]


# ---------------------------------------------------------------------------
# Footnotes
# ---------------------------------------------------------------------------

# Google exports a footnote as an inline <sup><a href="#ftnt1" id="ftnt_ref1">[1]</a></sup>
# plus a <div> at the end of the document holding the text.  Splitting the document
# would leave every footnote on the last page, so the text is pulled out here and
# re-attached to whichever page actually cites it.  Numbers are never reassigned: they
# have to keep matching the numbers an editor sees in the Doc, so the first footnote on
# a page is usually not number 1.


def extract_footnotes(body: Tag) -> dict[int, list]:
    """Detach the footnote blocks, returning {number: content nodes}."""
    notes: dict[int, list] = {}
    separator = None
    for anchor in body.find_all("a", id=True):
        match = re.match(r"^ftnt(\d+)$", anchor["id"])
        if not match:
            continue
        number = int(match.group(1))
        block = anchor.find_parent("div") or anchor.find_parent("p")
        if block is None:
            continue
        if separator is None:
            # Google puts an <hr> above the footnote list; note it before the block is
            # detached, because an extracted block has no siblings left to look at.
            previous = block.find_previous_sibling()
            if previous is not None and previous.name == "hr":
                separator = previous
        anchor.decompose()  # the "[1]" back-link; a fresh one is built per page
        block.extract()
        inner = block.find("p") or block
        contents = [c for c in inner.contents]
        # Google puts a non-breaking space between the number and the text.
        while contents and isinstance(contents[0], NavigableString) and not contents[0].strip():
            contents.pop(0)
        notes[number] = contents
    if separator is not None:
        separator.decompose()
    if notes:
        log(f"extracted {len(notes)} footnotes")
    return notes


def attach_footnotes(soup: BeautifulSoup, pages: list[Page], notes: dict[int, list]) -> None:
    """Point each footnote reference at its own page and list the notes at the bottom."""
    placed: set[int] = set()
    for page in pages:
        order: list[int] = []
        seen: dict[int, int] = {}
        for node in page.nodes:
            for anchor in node.find_all("a", href=True):
                match = re.match(r"^#ftnt(\d+)$", anchor["href"])
                if not match:
                    continue
                number = int(match.group(1))
                if number not in notes:
                    warn(f"footnote [{number}] is referenced but has no text")
                    continue
                seen[number] = seen.get(number, 0) + 1
                anchor["href"] = f"#fn{number}"
                anchor["id"] = f"fnref{number}" if seen[number] == 1 else f"fnref{number}-{seen[number]}"
                # Native tooltip, so the note is readable without leaving the paragraph.
                anchor["title"] = inline_text_from_nodes(notes[number])
                anchor.string = str(number)
                if number not in order:
                    order.append(number)
        if not order:
            continue
        section = soup.new_tag("section")
        section["class"] = "footnotes"
        heading = soup.new_tag("h2")
        heading["class"] = "footnotes-title"
        heading.string = "Notes"
        section.append(heading)
        listing = soup.new_tag("ol")
        for number in order:
            item = soup.new_tag("li")
            item["id"] = f"fn{number}"
            item["value"] = str(number)  # keeps the Doc's numbering
            # Copy: appending a node moves it, which would empty the note out of any
            # other page that cites the same footnote.
            for child in notes[number]:
                item.append(copy.copy(child))
            back = soup.new_tag("a", href=f"#fnref{number}")
            back["class"] = "footnote-back"
            back["title"] = "back to the text"
            back.string = "\u21a9"
            item.append(" ")
            item.append(back)
            listing.append(item)
            placed.add(number)
        section.append(listing)
        page.nodes.append(section)
    for number in sorted(set(notes) - placed):
        warn(f"footnote [{number}] has text but is never referenced; dropped")


# ---------------------------------------------------------------------------
# Page model
# ---------------------------------------------------------------------------

@dataclass
class Heading:
    level: int
    text: str
    anchor: str


@dataclass
class Page:
    filename: str
    title: str
    nodes: list[Tag] = field(default_factory=list)
    headings: list[Heading] = field(default_factory=list)
    is_front: bool = False

    @property
    def url(self) -> str:
        return "/" if self.is_front else f"/{self.filename}"


def split_pages(children: list[Tag], site_title: str) -> list[Page]:
    pages = [Page(filename="index.html", title=site_title, is_front=True)]
    used = {"index"}
    for node in children:
        if node.name == "h1":
            title = inline_text(node)
            base = slugify(title, "section")
            slug, n = base, 2
            while slug in used:
                slug, n = f"{base}-{n}", n + 1
            used.add(slug)
            pages.append(Page(filename=f"{slug}.html", title=title or slug))
            pages[-1].nodes.append(node)
        else:
            pages[-1].nodes.append(node)
    return pages


def assign_anchors(pages: list[Page]) -> dict[str, tuple[Page, str]]:
    """Give every heading a readable id and map Google's ids onto (page, anchor)."""
    id_map: dict[str, tuple[Page, str]] = {}
    for page in pages:
        used: set[str] = set()
        for node in page.nodes:
            for element in ([node] + node.find_all(True)) if isinstance(node, Tag) else []:
                if not isinstance(element, Tag):
                    continue
                is_heading = bool(re.match(r"^h[1-6]$", element.name or ""))
                google_id = element.get("id")
                if not is_heading and not google_id:
                    continue
                # Footnote ids (fn3, fnref3) are ours already; don't reslug them.
                if google_id and re.match(r"^fn(ref)?\d", google_id):
                    continue
                # The per-page "Notes" heading is chrome, not part of the outline.
                if "footnotes-title" in (element.get("class") or []):
                    continue
                if is_heading:
                    text = inline_text(element)
                    base = slugify(text, "heading")
                else:
                    base = slugify(inline_text(element), "anchor")
                anchor, n = base, 2
                while anchor in used:
                    anchor, n = f"{base}-{n}", n + 1
                used.add(anchor)
                element["id"] = anchor
                if google_id:
                    id_map[google_id] = (page, anchor)
                if is_heading:
                    level = int(element.name[1])
                    page.headings.append(
                        Heading(level=level, text=inline_text(element), anchor=anchor)
                    )
    return id_map


def fix_internal_links(pages: list[Page], id_map: dict[str, tuple[Page, str]]) -> None:
    fixed = broken = 0
    for page in pages:
        for node in page.nodes:
            for anchor in node.find_all("a", href=True):
                href = anchor["href"]
                if not href.startswith("#"):
                    continue
                if re.match(r"^#fn(ref)?\d", href):
                    continue  # already a per-page footnote link
                # Markdown-style targets in case a link was pasted by hand.
                target = href[1:]
                if target.startswith("heading=") or target.startswith("bookmark="):
                    target = target.split("=", 1)[1]
                entry = id_map.get(target)
                if entry is None:
                    broken += 1
                    warn(f"unresolved internal link {href} on {page.filename}")
                    anchor.unwrap()
                    continue
                dest_page, dest_anchor = entry
                base = "/" if dest_page.is_front else f"/{dest_page.filename}"
                if not dest_anchor:
                    anchor["href"] = base
                elif dest_page is page:
                    anchor["href"] = f"#{dest_anchor}"
                else:
                    anchor["href"] = f"{base}#{dest_anchor}"
                fixed += 1
    log(f"internal links: {fixed} rewritten, {broken} unresolved")


# ---------------------------------------------------------------------------
# Tables of contents
# ---------------------------------------------------------------------------

def build_toc_list(soup: BeautifulSoup, items: list[tuple[int, str, str]], base_level: int) -> Tag:
    """Turn (level, text, href) triples into nested <ul>s, tolerating skipped levels."""
    root = soup.new_tag("ul")
    stack: list[tuple[int, Tag]] = [(base_level, root)]
    for level, text, href in items:
        while len(stack) > 1 and stack[-1][0] > level:
            stack.pop()
        if stack[-1][0] < level:
            parent = stack[-1][1]
            siblings = parent.find_all("li", recursive=False)
            nested = soup.new_tag("ul")
            if siblings:
                siblings[-1].append(nested)
            else:
                parent.append(nested)
            stack.append((level, nested))
        li = soup.new_tag("li")
        link = soup.new_tag("a", href=href)
        link.string = text
        li.append(link)
        stack[-1][1].append(li)
    return root


def front_toc(soup: BeautifulSoup, pages: list[Page]) -> Tag:
    nav = soup.new_tag("nav")
    nav["class"] = "toc"
    title = soup.new_tag("p")
    title["class"] = "toc-title"
    title.string = "Contents"
    nav.append(title)
    outer = soup.new_tag("ul")
    outer["class"] = "toc-pages"
    for page in pages:
        if page.is_front:
            continue
        li = soup.new_tag("li")
        link = soup.new_tag("a", href=page.url)
        link.string = page.title
        li.append(link)
        sub_items = [
            (h.level, h.text, f"{page.url}#{h.anchor}")
            for h in page.headings
            if 2 <= h.level <= FRONT_TOC_MAX_LEVEL and h.text
        ]
        if sub_items:
            li.append(build_toc_list(soup, sub_items, base_level=2))
        outer.append(li)
    nav.append(outer)
    return nav


def section_toc(soup: BeautifulSoup, page: Page) -> Tag | None:
    low, high = SECTION_TOC_LEVELS
    items = [
        (h.level, h.text, f"#{h.anchor}")
        for h in page.headings
        if low <= h.level <= high and h.text
    ]
    if not items:
        return None
    nav = soup.new_tag("nav")
    nav["class"] = "toc"
    title = soup.new_tag("p")
    title["class"] = "toc-title"
    title.string = "On this page"
    nav.append(title)
    nav.append(build_toc_list(soup, items, base_level=low))
    return nav


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(template: str, **fields: str) -> str:
    out = template
    for key, value in fields.items():
        out = out.replace("{{%s}}" % key, value)
    leftover = re.findall(r"\{\{(\w+)\}\}", out)
    if leftover:
        warn(f"template placeholders left unfilled: {sorted(set(leftover))}")
    return out


def escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def first_paragraph_text(page: Page, limit: int = 160) -> str:
    for node in page.nodes:
        if node.name == "p":
            text = inline_text(node)
            if text:
                return text[:limit]
    return page.title


def pager(soup: BeautifulSoup, pages: list[Page], index: int) -> Tag:
    nav = soup.new_tag("nav")
    nav["class"] = "pager"
    prev_page = pages[index - 1] if index > 0 else None
    next_page = pages[index + 1] if index + 1 < len(pages) else None
    if prev_page is not None:
        span = soup.new_tag("span")
        link = soup.new_tag("a", href=prev_page.url)
        link.string = f"← {prev_page.title}"
        span.append(link)
        nav.append(span)
    if next_page is not None:
        span = soup.new_tag("span")
        link = soup.new_tag("a", href=next_page.url)
        link.string = f"{next_page.title} →"
        span.append(link)
        nav.append(span)
    return nav


def clear_output(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path) and name.endswith(GENERATED_SUFFIXES):
            os.remove(path)


def build(html: str, out_dir: str) -> list[Page]:
    soup = BeautifulSoup(html, "html.parser")
    styles = parse_style_classes(soup)

    body_early = soup.body or soup
    site_title = "Untitled"
    title_google_id = None
    title_node = body_early.find(class_="title")
    if title_node is not None:
        site_title = inline_text(title_node) or site_title
        title_google_id = title_node.get("id")
        title_node.decompose()
    else:
        warn("no Title-styled paragraph found; site title will be 'Untitled'")

    removed = strip_comments(soup)
    log(f"stripped {removed} comment references/blocks")

    body = soup.body or soup
    for tag in body.find_all("style"):
        tag.decompose()

    for anchor in body.find_all("a", href=True):
        anchor["href"] = unwrap_redirect(anchor["href"])

    apply_marks(soup, styles)
    download_images(soup, out_dir)
    clean_attrs(soup)
    drop_empty_blocks(body)

    notes = extract_footnotes(body)

    children = [c for c in body.children if isinstance(c, Tag)]
    children = remove_header_block(children)

    # Find and detach the TOC marker paragraph; remember where it was.
    toc_slot = None
    for node in children:
        if matches_marker(node.get_text(), TOC_MARKERS) or contains_marker(
            node.get_text(), TOC_MARKERS
        ):
            toc_slot = node
            break
    if toc_slot is None:
        warn(f"no TOC marker found (looked for {', '.join(TOC_MARKERS)})")

    pages = split_pages(children, site_title)
    log(f"split into {len(pages)} pages (front page + {len(pages) - 1} sections)")

    attach_footnotes(soup, pages, notes)

    id_map = assign_anchors(pages)
    if title_google_id:
        id_map[title_google_id] = (pages[0], "")
    fix_internal_links(pages, id_map)

    # Insert the front-page TOC where the marker was; otherwise at the top.
    front = pages[0]
    toc = front_toc(soup, pages)
    if toc_slot is not None and toc_slot in front.nodes:
        front.nodes[front.nodes.index(toc_slot)] = toc
    elif toc_slot is not None:
        warn("TOC marker was not on the front page; putting the contents at the top")
        front.nodes.insert(0, toc)
    else:
        front.nodes.insert(0, toc)

    # Each section page gets its own TOC right after the h1.
    for page in pages[1:]:
        toc = section_toc(soup, page)
        if toc is not None:
            insert_at = 1 if page.nodes and page.nodes[0].name == "h1" else 0
            page.nodes.insert(insert_at, toc)

    with open(os.path.join(SITE_DIR, "template.html"), encoding="utf-8") as fh:
        template = fh.read()

    generated = datetime.date.today().isoformat()
    clear_output(out_dir)

    for index, page in enumerate(pages):
        content_parts = []
        if page.is_front:
            content_parts.append(f"<h1>{escape(site_title)}</h1>")
        content_parts.extend(str(node) for node in page.nodes)
        content_parts.append(str(pager(soup, pages, index)))
        breadcrumb = (
            ""
            if page.is_front
            else '<p class="breadcrumb"><a href="/">← All sections</a></p>'
        )
        page_title = site_title if page.is_front else f"{page.title} — {site_title}"
        out = render(
            template,
            page_title=escape(page_title),
            site_title=escape(site_title),
            description=escape(first_paragraph_text(page)),
            breadcrumb=breadcrumb,
            content="\n".join(content_parts),
            doc_url=DOC_VIEW_URL,
            generated=generated,
        )
        with open(os.path.join(out_dir, page.filename), "w", encoding="utf-8") as fh:
            fh.write(out)
        log(f"wrote {page.filename}")

    for filename, title, fragment_name in STATIC_PAGES:
        path = os.path.join(SITE_DIR, fragment_name)
        if not os.path.exists(path):
            warn(f"missing fragment site/{fragment_name}; skipping {filename}")
            continue
        with open(path, encoding="utf-8") as fh:
            fragment = fh.read()
        # The fragment is a template in its own right; fill it before embedding it, so
        # its placeholders don't depend on the order the page's fields are substituted.
        fragment = render(fragment, doc_url=DOC_VIEW_URL, generated=generated)
        out = render(
            template,
            page_title=escape(f"{title} \u2014 {site_title}"),
            site_title=escape(site_title),
            description=escape(title),
            breadcrumb="",
            content=f"<h1>{escape(title)}</h1>\n{fragment}",
            doc_url=DOC_VIEW_URL,
            generated=generated,
        )
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as fh:
            fh.write(out)
        log(f"wrote {filename}")

    # TODO: These should be in a list like STATIC_PAGES, or maybe just copy all
    # the files in site/ that aren't special in some way.
    shutil.copyfile(os.path.join(SITE_DIR, "style.css"), os.path.join(out_dir, "style.css"))
    shutil.copyfile(os.path.join(SITE_DIR, "bluedot.png"), os.path.join(out_dir, "bluedot.png"))
    log("wrote style.css")
    return pages


def deploy() -> None:
    log("deploying to Firebase Hosting")
    subprocess.run(["firebase", "deploy", "--only", "hosting"], cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deploy", action="store_true", help="deploy to Firebase after building")
    parser.add_argument("--cache", metavar="FILE", help="read the export from FILE if it exists, else save it there")
    parser.add_argument("--refetch", action="store_true", help="ignore --cache contents and download again")
    parser.add_argument("--out", default=OUT_DIR, help="output directory (default: public/)")
    args = parser.parse_args()

    html = fetch_doc(args.cache, args.refetch)
    pages = build(html, args.out)
    log(f"built {len(pages) + 1} files in {args.out}")
    if args.deploy:
        deploy()


if __name__ == "__main__":
    main()
