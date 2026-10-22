"""One top-level heading per document, in every format that wraps the body.

The wrappers used to put the file name above the document as an H1 of its
own, so a converted reading opened with two: "week1-water-cycle" and then
the document's real title.
"""

from __future__ import annotations

import io
import re
import zipfile

from connector.canvas.alt_formats import (
    READER_TEMPLATE,
    document_heading,
    html_full_document,
    render_epub,
    render_reader_html,
)
from connector.canvas.braille import build_blocks, build_braille_xml
from connector.canvas.markdown_to_html import RenderedPage

WITH_H1 = RenderedPage(
    title="week1-water-cycle", html="<p>Banner</p><h1>The <em>Water</em> Cycle</h1><p>Text</p>", lang="es",
)
WITHOUT_H1 = RenderedPage(title="week1-water-cycle", html="<h2>Overview</h2><p>Text</p>")


def _h1s(text: str) -> int:
    return len(re.findall(r"<h1[\s>]", text))


def test_the_document_heading_wins_over_the_file_name():
    assert document_heading(WITH_H1) == ("The Water Cycle", "")
    assert document_heading(WITHOUT_H1) == ("week1-water-cycle", "<h1>week1-water-cycle</h1>")


def test_html_and_reader():
    for page in (html_full_document(WITH_H1, mathjax=False), render_reader_html(WITH_H1)):
        assert _h1s(page) == 1 and "The Water Cycle" in page.split("</title>")[0]
        assert '<html lang="es">' in page
    for page in (html_full_document(WITHOUT_H1, mathjax=False), render_reader_html(WITHOUT_H1)):
        assert _h1s(page) == 1 and "<h1>week1-water-cycle</h1>" in page


def test_epub_chapter_is_not_empty_and_has_one_h1():
    with zipfile.ZipFile(io.BytesIO(render_epub(WITH_H1))) as book:
        chapter = book.read("EPUB/content.xhtml").decode("utf-8")
    assert "Text" in chapter and _h1s(chapter) == 1


def test_braille_title_line_only_when_the_body_has_no_heading():
    headings = [b for b in build_blocks(WITH_H1) if b.kind == "heading" and b.level == 1]
    assert len(headings) == 1 and "Water" in "".join(r.text for r in headings[0].runs)
    first = build_blocks(WITHOUT_H1)[0]
    assert first.kind == "heading" and first.runs[0].text == "week1-water-cycle"
    assert _h1s(build_braille_xml(WITH_H1)) == 1


def test_reader_dictionary_never_writes_markup():
    assert "pop.innerHTML" not in READER_TEMPLATE
