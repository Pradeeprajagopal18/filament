"""Turn documents into sections (heading path + text) and sections into passages.

Handles HTML (including Confluence's storage format), Markdown, plain text, PDF and Word.
Passages keep their heading path ("Runbook > Rollback > Database"), which helps both keyword and
meaning search and lets answers point at the right part of a page.
"""

from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser

Section = tuple[list[str], str]  # (heading path, text)

SKIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form",
             "ac:parameter"}
BLOCK_TAGS = {"p", "div", "li", "tr", "br", "pre", "blockquote", "section", "article", "table",
              "ul", "ol", "dd", "dt", "ac:structured-macro", "ac:rich-text-body",
              "ac:plain-text-body"}
HEADINGS = {f"h{i}": i for i in range(1, 7)}


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self.path: list[tuple[int, str]] = []
        self.buf: list[str] = []
        self.heading: list[str] | None = None
        self.skip = 0
        self.title = ""
        self.in_title = False
        self.links: list[str] = []

    def _flush(self) -> None:
        text = _clean("".join(self.buf))
        if text:
            self.sections.append(([h for _, h in self.path], text))
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in HEADINGS:
            self._flush()
            self.heading = []
        elif tag in {"td", "th"}:
            self.buf.append(" | ")
        elif tag in BLOCK_TAGS:
            self.buf.append("\n")
        if tag == "li":
            self.buf.append("- ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.in_title = False
        if tag in HEADINGS and self.heading is not None:
            level, text = HEADINGS[tag], _clean(" ".join(self.heading))
            self.path = [(lv, h) for lv, h in self.path if lv < level]
            if text:
                self.path.append((level, text))
            self.heading = None
        elif tag in BLOCK_TAGS:
            self.buf.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.skip:
            return
        if self.heading is not None:
            self.heading.append(data)
        else:
            self.buf.append(data)

    def unknown_decl(self, data):  # Confluence code macros keep their body in CDATA
        if data.startswith("CDATA[") and not self.skip:
            self.buf.append("\n" + data[6:] + "\n")


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_sections(html: str) -> tuple[str, list[Section], list[str]]:
    """(page title, sections, links)."""
    p = _Html()
    p.feed(html)
    p._flush()
    return _clean(p.title), p.sections, p.links


def markdown_sections(md: str) -> list[Section]:
    out: list[Section] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []
    fence = False

    def flush():
        text = _clean("\n".join(buf))
        if text:
            out.append(([h for _, h in path], text))
        buf.clear()

    for line in md.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            level = len(m.group(1))
            path = [(lv, h) for lv, h in path if lv < level] + [(level, m.group(2))]
        else:
            buf.append(line)
    flush()
    return out


def pdf_sections(data: bytes) -> list[Section]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("Reading PDFs needs the pypdf package on the server "
                         "(pip install 'igniteai[knowledge]')") from exc
    reader = PdfReader(io.BytesIO(data))
    return [([f"Page {i}"], _clean(page.extract_text() or ""))
            for i, page in enumerate(reader.pages, 1) if (page.extract_text() or "").strip()]


def docx_sections(data: bytes) -> list[Section]:
    """Word documents: paragraphs, with Heading styles as headings (no extra package needed)."""
    try:
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf8")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Not a readable .docx file") from exc
    md = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, flags=re.S))
        text = re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", re.sub(r"&amp;", "&", text)))
        style = re.search(r'<w:pStyle w:val="(?:Heading|heading)\s*(\d)"', para)
        md.append(("#" * int(style.group(1)) + " " + text) if style and text else text)
    return markdown_sections("\n".join(md))


def file_sections(name: str, data: bytes) -> tuple[str, list[Section]]:
    lower = name.lower()
    title = re.sub(r"\.[a-z0-9]+$", "", name.rsplit("/", 1)[-1])
    if lower.endswith(".pdf"):
        return title, pdf_sections(data)
    if lower.endswith(".docx"):
        return title, docx_sections(data)
    text = data.decode("utf8", errors="replace")
    if lower.endswith((".html", ".htm")):
        t, sections, _ = html_sections(text)
        return t or title, sections
    if lower.endswith((".md", ".markdown", ".mdx", ".rst", ".adoc")):
        return title, markdown_sections(text)
    if lower.endswith((".txt", ".text", ".csv", ".log", ".yaml", ".yml", ".json")):
        return title, [([], _clean(text))] if text.strip() else []
    raise ValueError(f"Can't read {name}: use PDF, Word (.docx), Markdown, HTML or text")


# -- passages -------------------------------------------------------------------------------------
def chunk(title: str, sections: list[Section], max_words: int = 350,
          overlap_words: int = 50) -> list[tuple[str, str]]:
    """Passages of about max_words, never mixing two headings, with a little overlap so a fact
    split across a boundary is still found. Returns (heading path, text)."""
    out: list[tuple[str, str]] = []
    for path, text in sections:
        heading = " > ".join([title, *path] if title and (not path or path[0] != title)
                             else path)
        paras = [p for p in re.split(r"\n\s*\n|\n(?=- )", text) if p.strip()]
        cur: list[str] = []
        count = 0
        for para in paras:
            words = para.split()
            while len(words) > max_words:  # a single huge paragraph
                if cur:
                    out.append((heading, "\n\n".join(cur)))
                    cur, count = [], 0
                out.append((heading, " ".join(words[:max_words])))
                words = words[max_words - overlap_words:]
            if count + len(words) > max_words and cur:
                out.append((heading, "\n\n".join(cur)))
                tail = " ".join(" ".join(cur).split()[-overlap_words:])
                cur, count = [tail], len(tail.split())
            cur.append(" ".join(words) if len(words) != len(para.split()) else para.strip())
            count += len(words)
        if cur and count:
            out.append((heading, "\n\n".join(cur)))
    return out


STOP = set("""a an and are as at be by for from has have how i in is it its of on or that the
this to was what when where which who why will with you your we our can do does not no if
then than into about use used using""".split())


def terms(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for w in re.findall(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out[w] = out.get(w, 0) + 1
    return out
