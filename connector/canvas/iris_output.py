"""Iris's HTML -> the connector's canonical accessible document.

Iris answers ``GET /v1/sessions/{id}/output`` with a whole document::

    <!DOCTYPE html>
    <html lang="en">
    <head><meta charset="utf-8"><title>...</title></head>
    <body>
    <main>
    ...the accessible content...
    </main>
    <!-- @page-failed 7 ... -->
    <!-- @page-uncorrected 5, 31 ... -->
    <!-- @unresolved
      - an issue the review loop left open
    -->
    </body></html>

This module turns one or more of those (one per part of a long PDF, see
:mod:`connector.canvas.iris_documents`) into what the connector stores and
renders from:

* **one body** — the parts' ``<main>`` contents in page order, with the ids
  of every part after the first prefixed so footnote links from two parts
  cannot collide;
* **a figure manifest** — Iris cannot embed images (it reads a rasterized
  page and has no asset to point at), so every ``<img>`` arrives with a
  placeholder ``src`` such as ``page-3-chart.png``. Each one is renamed to a
  stable ref, ``figures/p{source page}-{n}.png``, that the bridge later
  resolves to a Canvas file, the same way it resolved Reflow's
  ``figures/<id>.png`` refs;
* **a report** — the pages Iris could not extract, the pages that failed
  its own fidelity check, and the issues its reviewer left open. Iris says
  these things in HTML comments, which every sanitizer downstream strips,
  so they are lifted out here, mapped to source-PDF page numbers, and shown
  to faculty on the review screen.

It also holds the conversions the alt-format renderers need, because the
canonical HTML now carries MathML and ``doc-pagebreak`` markers instead of
the LaTeX and bare text Reflow's markdown carried.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

MATHML_NS = "http://www.w3.org/1998/Math/MathML"

# ``page-3-chart.png``, ``page-12-cover.jpg`` — the placeholder shape Iris's
# page agent is told to write (agents/page.md: "a placeholder that names the
# page and the graphic"). The number is the page's position in the session.
_PLACEHOLDER_PAGE_RE = re.compile(r"(?:^|[/_-])page[-_ ]?(\d{1,4})(?=[-_.])", re.IGNORECASE)

_FAILED_RE = re.compile(r"^\s*@page-failed\s+([\d,\s]+)", re.IGNORECASE)
_FAILED_INLINE_RE = re.compile(r"^\s*@page-failed\s+(\d+)\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)
_UNCORRECTED_RE = re.compile(r"^\s*@page-uncorrected\s+([\d,\s]+)", re.IGNORECASE)
_UNRESOLVED_RE = re.compile(r"^\s*@unresolved\b", re.IGNORECASE)
_TRUNCATED_RE = re.compile(r"^\s*@editor-truncated\b", re.IGNORECASE)
_LINT_UNAVAILABLE_RE = re.compile(r"^\s*@lint-unavailable\b", re.IGNORECASE)
_REVIEW_UNREAD_RE = re.compile(r"^\s*@review-unread\b", re.IGNORECASE)

# Attributes whose value is one or more ids in the same document.
_IDREF_LIST_ATTRS = (
    "aria-labelledby", "aria-describedby", "aria-controls", "aria-owns",
    "aria-flowto", "aria-details", "headers",
)
_IDREF_SINGLE_ATTRS = ("aria-activedescendant", "aria-errormessage", "for", "list", "form")


@dataclass
class IrisFigure:
    """One image in the document and where its pixels should come from."""

    ref: str                 # e.g. "figures/p4-1.png" — the src in the stored body
    page: int | None         # 1-based page in the SOURCE PDF, when Iris named it
    index_on_page: int       # 1-based position among this page's images
    alt: str
    placeholder: str         # the src Iris wrote, for diagnostics

    @property
    def decorative(self) -> bool:
        return not self.alt.strip()


@dataclass
class IrisPartInfo:
    """Where one Iris session's pages sit in the source PDF."""

    index: int
    first_page: int
    last_page: int
    session_id: str = ""

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1


@dataclass
class IrisReport:
    """What Iris said about the document, in source-PDF page numbers."""

    failed_pages: list[int] = field(default_factory=list)
    failed_reasons: dict[str, str] = field(default_factory=dict)
    uncorrected_pages: list[int] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    editor_truncated: bool = False
    lint_unavailable: bool = False
    review_unread: bool = False
    parts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> IrisReport:
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class IrisDocument:
    body_html: str
    lang: str
    figures: list[IrisFigure]
    report: IrisReport


# ---------------------------------------------------------------------------
# Parsing and stitching
# ---------------------------------------------------------------------------

def _soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup or "", "html.parser")


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", raw or "")]


def _read_markers(soup: BeautifulSoup, report: IrisReport, part: IrisPartInfo, n_parts: int) -> None:
    """Lift Iris's comment markers into ``report`` and drop the comments."""

    def to_source(page: int) -> int | None:
        if 1 <= page <= part.page_count:
            return part.first_page + page - 1
        return None

    for comment in list(soup.find_all(string=lambda s: isinstance(s, Comment))):
        text = str(comment)
        m_inline = _FAILED_INLINE_RE.match(text)
        if m_inline:
            src = to_source(int(m_inline.group(1)))
            if src is not None:
                reason = " ".join(m_inline.group(2).split())[:300]
                report.failed_reasons.setdefault(str(src), reason)
                if src not in report.failed_pages:
                    report.failed_pages.append(src)
        elif _FAILED_RE.match(text):
            for p in _int_list(_FAILED_RE.match(text).group(1)):  # type: ignore[union-attr]
                src = to_source(p)
                if src is not None and src not in report.failed_pages:
                    report.failed_pages.append(src)
        elif _UNCORRECTED_RE.match(text):
            for p in _int_list(_UNCORRECTED_RE.match(text).group(1)):  # type: ignore[union-attr]
                src = to_source(p)
                if src is not None and src not in report.uncorrected_pages:
                    report.uncorrected_pages.append(src)
        elif _UNRESOLVED_RE.match(text):
            prefix = (
                f"Part {part.index + 1} (pages {part.first_page}-{part.last_page}): "
                if n_parts > 1 else ""
            )
            for line in text.splitlines()[1:]:
                item = line.strip()
                if item.startswith("- "):
                    report.unresolved.append(prefix + item[2:].strip())
        elif _TRUNCATED_RE.match(text):
            report.editor_truncated = True
        elif _LINT_UNAVAILABLE_RE.match(text):
            report.lint_unavailable = True
        elif _REVIEW_UNREAD_RE.match(text):
            report.review_unread = True
        comment.extract()


def _split_idrefs(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return str(value or "").split()


def _defined_ids(root: Tag) -> list[str]:
    """Every id defined in ``root``, in document order, once each."""
    seen: dict[str, None] = {}
    for el in root.find_all(True, attrs={"id": True}):
        seen.setdefault(str(el["id"]), None)
    return list(seen)


def _plan_ids(roots: list[Tag]) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Decide each part's final ids.

    An id keeps its name unless an earlier part already used it (Iris names
    footnotes by their page within a session, so ``p1-fn-1`` turns up in
    every part); a clash is renamed ``part{n}-...``. Returns each part's
    renames, and for every id the final name at the first part defining it,
    which is where a reference from a part that does not define the id is
    pointed (a table of contents in part 1 linking to a heading in part 2).
    """
    taken: set[str] = set()
    plans: list[dict[str, str]] = []
    first: dict[str, str] = {}
    for n, root in enumerate(roots, start=1):
        plan: dict[str, str] = {}
        for ident in _defined_ids(root):
            final = ident
            if final in taken:
                final = f"part{n}-{ident}"
                k = 2
                while final in taken:
                    final = f"part{n}-{k}-{ident}"
                    k += 1
            plan[ident] = final
            taken.add(final)
            first.setdefault(ident, final)
        plans.append(plan)
    return plans, first


def _apply_ids(root: Tag, own: dict[str, str], first: dict[str, str]) -> None:
    """Rename ``root``'s ids and point every same-document reference at its
    target: this part's own element when it defines the id, otherwise the
    first part that does. A reference nothing defines is left as written."""

    def target(ref: str) -> str:
        return own.get(ref) or first.get(ref) or ref

    for el in root.find_all(True, attrs={"id": True}):
        el["id"] = own.get(str(el["id"]), str(el["id"]))
    for el in root.find_all(True, attrs={"name": True}):
        if el.name == "a" and str(el["name"]) in own:
            el["name"] = own[str(el["name"])]
    for el in root.find_all(href=True):
        href = str(el["href"])
        if href.startswith("#") and len(href) > 1:
            el["href"] = "#" + target(href[1:])
    for attr in _IDREF_LIST_ATTRS:
        for el in root.find_all(True, attrs={attr: True}):
            el[attr] = " ".join(target(r) for r in _split_idrefs(el[attr]))
    for attr in _IDREF_SINGLE_ATTRS:
        for el in root.find_all(True, attrs={attr: True}):
            el[attr] = target(str(el[attr]))


def placeholder_page(src: str | None, part: IrisPartInfo) -> int | None:
    """The SOURCE-PDF page an image placeholder names, if it names one we trust.

    Iris's page agent writes the page's position in the session ("page N of
    M"), so ``page-3-...`` in the second part of a 50-page PDF cut at 25 is
    source page 28. A number outside the part cannot be a session position
    (it is a printed folio, or a guess) and is not trusted.
    """
    m = _PLACEHOLDER_PAGE_RE.search(src or "")
    if not m:
        return None
    n = int(m.group(1))
    if 1 <= n <= part.page_count:
        return part.first_page + n - 1
    return None


def stitch(parts: list[tuple[IrisPartInfo, str]]) -> IrisDocument:
    """Combine the Iris output of each part into one :class:`IrisDocument`.

    ``parts`` must be in page order. A single part is the common case and
    goes through the same path, minus the id prefixing.
    """
    ordered = sorted(parts, key=lambda p: p[0].first_page)
    report = IrisReport(parts=len(ordered))
    bodies: list[str] = []
    figures: list[IrisFigure] = []
    per_page_counts: dict[int, int] = {}
    unplaced = 0
    langs: list[str] = []

    containers: list[tuple[IrisPartInfo, Tag]] = []
    for info, html in ordered:
        soup = _soup(html)
        root_html = soup.find("html")
        lang = str(root_html.get("lang") or "").strip() if isinstance(root_html, Tag) else ""
        if lang:
            langs.append(lang)
        _read_markers(soup, report, info, len(ordered))
        found = soup.find("main") or soup.find("body")
        containers.append((info, found if isinstance(found, Tag) else soup))

    if len(containers) > 1:
        plans, first = _plan_ids([c for _, c in containers])
        for (_, container), plan in zip(containers, plans, strict=True):
            _apply_ids(container, plan, first)

    for info, container in containers:
        for math in container.find_all("math"):
            if not math.get("xmlns"):
                math["xmlns"] = MATHML_NS

        for img in container.find_all("img"):
            placeholder = str(img.get("src") or "")
            page = placeholder_page(placeholder, info)
            if page is not None:
                per_page_counts[page] = per_page_counts.get(page, 0) + 1
                k = per_page_counts[page]
                ref = f"figures/p{page}-{k}.png"
            else:
                unplaced += 1
                k = unplaced
                ref = f"figures/x{unplaced}.png"
            alt = str(img.get("alt") or "")
            img["src"] = ref
            if not img.has_attr("alt"):
                img["alt"] = ""
            figures.append(IrisFigure(
                ref=ref, page=page, index_on_page=k, alt=alt, placeholder=placeholder,
            ))

        bodies.append(container.decode_contents().strip())

    report.failed_pages.sort()
    report.uncorrected_pages.sort()
    lang = langs[0] if langs else "en"
    return IrisDocument(
        body_html="\n".join(b for b in bodies if b),
        lang=lang,
        figures=figures,
        report=report,
    )


# ---------------------------------------------------------------------------
# Rendering the stored body
# ---------------------------------------------------------------------------

def figures_from_manifest(manifest: list[dict[str, Any]] | None) -> list[IrisFigure]:
    out: list[IrisFigure] = []
    for item in manifest or []:
        try:
            out.append(IrisFigure(
                ref=str(item["ref"]),
                page=item.get("page"),
                index_on_page=int(item.get("index_on_page") or 1),
                alt=str(item.get("alt") or ""),
                placeholder=str(item.get("placeholder") or ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def apply_figures(
    body_html: str,
    figure_urls: dict[str, str] | None,
    *,
    manifest: list[IrisFigure] | None = None,
) -> str:
    """Point each figure ref at its Canvas file, or say what it showed.

    An image whose pixels could not be lifted out of the PDF is not left as
    a broken ``<img>``: a decorative one (``alt=""``) is dropped, and an
    informative one is replaced by its description as visible text, naming
    the page of the original PDF where the picture is. A screen-reader user
    gets the same words either way; a sighted reader gets the description
    instead of a broken-image icon, and knows where to look for the picture.
    """
    urls = figure_urls or {}
    pages = {f.ref: f.page for f in (manifest or [])}
    soup = _soup(body_html)
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if not src.startswith("figures/"):
            continue
        if src in urls:
            img["src"] = urls[src]
            continue
        alt = str(img.get("alt") or "").strip()
        parent = img.parent
        if not alt:
            img.decompose()
        else:
            page = pages.get(src)
            label = (
                f"Image (page {page} of the original PDF): " if page else "Image: "
            )
            span = soup.new_tag("span", attrs={"class": "iris-image-description"})
            span.string = label + alt
            img.replace_with(span)
        # A <figure> left holding nothing is noise for every reader.
        if isinstance(parent, Tag) and parent.name == "figure" and not parent.get_text(strip=True) \
                and not parent.find(["img", "table", "math", "svg"]):
            parent.decompose()
    return str(soup)


def unresolved_figure_refs(
    figures: list[IrisFigure], figure_urls: dict[str, str] | None,
) -> list[IrisFigure]:
    urls = figure_urls or {}
    return [f for f in figures if f.ref not in urls and not f.decorative]


# ---------------------------------------------------------------------------
# For the alt formats that cannot use MathML or doc-pagebreak markers
# ---------------------------------------------------------------------------

def _math_latex(math: Tag) -> tuple[str, bool]:
    """(LaTeX source, is_block) for one ``<math>`` element.

    Iris's math specialist puts the expression's LaTeX in ``alttext``
    (agents/mathematicalEquationAgent.md). Without it the best available
    linear form is the element's text, which loses structure but keeps
    every symbol.
    """
    latex = str(math.get("alttext") or "").strip()
    block = str(math.get("display") or "").lower() == "block"
    if not latex:
        latex = " ".join(math.get_text(" ", strip=True).split())
    return latex, block


def mathml_to_latex(html: str) -> str:
    """Replace each ``<math>`` with its LaTeX between ``\\(...\\)`` / ``\\[...\\]``.

    The braille transcriber, the tagged-PDF renderer (matplotlib mathtext),
    the plain-text and audio outputs and the translation prompt were all
    built around LaTeX in the text, which is what Reflow's markdown carried.
    """
    if "<math" not in (html or "").lower():
        return html
    soup = _soup(html)
    for math in soup.find_all("math"):
        latex, block = _math_latex(math)
        if not latex:
            math.decompose()
            continue
        math.replace_with(NavigableString(f"\\[{latex}\\]" if block else f"\\({latex}\\)"))
    return str(soup)


def _page_label(hr: Tag) -> str:
    label = str(hr.get("aria-label") or "").strip()
    m = re.match(r"(?i)^page\s+(.+)$", label)
    if m:
        return m.group(1).strip()
    ident = str(hr.get("id") or "")
    m = re.search(r"page-([0-9a-z]+)$", ident, re.IGNORECASE)
    return m.group(1) if m else ""


def pagebreaks_to_markers(html: str) -> str:
    """``<hr role="doc-pagebreak" aria-label="Page 214">`` -> ``<p>[Page 214]</p>``.

    ``[Page N]`` is the print-page convention the braille module already
    understands (a braille reader in class needs to find "page 214" in the
    book everyone else is holding).
    """
    if "doc-pagebreak" not in (html or ""):
        return html
    soup = _soup(html)
    for hr in soup.find_all(True, attrs={"role": "doc-pagebreak"}):
        label = _page_label(hr)
        if label:
            p = soup.new_tag("p")
            p.string = f"[Page {label}]"
            hr.replace_with(p)
        else:
            hr.decompose()
    return str(soup)


def strip_pagebreaks(html: str) -> str:
    if "doc-pagebreak" not in (html or ""):
        return html
    soup = _soup(html)
    for hr in soup.find_all(True, attrs={"role": "doc-pagebreak"}):
        hr.decompose()
    return str(soup)


def html_to_markdown(body_html: str) -> str:
    """Markdown for the "Markdown" alt format, from the canonical HTML.

    Math comes out as ``$...$`` / ``$$...$$`` LaTeX and print pages as
    ``[Page N]``. Anything Markdown cannot express (a table with merged
    cells, say) is approximated by markdownify, which is why HTML stays the
    canonical form and Markdown is a convenience export.
    """
    from markdownify import markdownify as _md

    soup = _soup(pagebreaks_to_markers(body_html or ""))
    stash: dict[str, str] = {}
    for i, math in enumerate(soup.find_all("math")):
        latex, block = _math_latex(math)
        token = f"IRISMATHTOKEN{i}X"
        stash[token] = f"\n\n$${latex}$$\n\n" if block else f"${latex}$"
        math.replace_with(NavigableString(token))
    text = _md(str(soup), heading_style="ATX", bullets="-", strip=["span"])
    for token, value in stash.items():
        text = text.replace(token, value)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def escape_text(text: str) -> str:
    return _html.escape(text or "", quote=True)
