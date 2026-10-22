"""Joining the tagged PDFs of a document Iris converted in parts."""

from __future__ import annotations

import io

import fitz
import pikepdf
import pytest
from connector.canvas.pdf_merge import PdfMergeError, merge_tagged_pdfs

from tests.tagged_pdf_fixtures import check_structure, tagged_pdf

PART_1 = [[("H1", "Week 3 reading"), ("P", "Part one, page one.")], [("P", "Part one, page two.")]]
PART_2 = [[("H2", "Results"), ("P", "Part two, page one.")], [("P", "Part two, page two.")]]


def _parts(**kw):
    first = tagged_pdf(PART_1, table_on=0, link_on=1, footer_xobject=True, **kw)
    second = tagged_pdf(PART_2, table_on=0, link_on=1, footer_xobject=True, **kw)
    return first, second


@pytest.mark.unit
def test_fixture_parts_are_themselves_consistent():
    for part in _parts():
        facts = check_structure(part)
        assert facts["pages"] == 2 and facts["ids"] == [b"p1-th1"]


@pytest.mark.unit
def test_two_parts_become_one_document_with_one_tree():
    merged = merge_tagged_pdfs(list(_parts()), title="Week 3 reading")
    facts = check_structure(merged)  # every MCID, link and XObject still owned
    assert facts["pages"] == 4
    assert facts["top"] == ["/Document"]
    kids = facts["document_kids"]
    # Part two's elements follow part one's, inside the same Document.
    assert kids[0] == "/H1" and "/H2" in kids and kids.index("/H2") > kids.index("/H1")
    # Each part's table header kept its own id; part two's was renamed.
    assert facts["ids"] == [b"p1-th1", b"part2-p1-th1"]
    with fitz.open(stream=merged, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    for words in ("Part one, page one.", "Part one, page two.", "Part two, page one.", "Part two, page two."):
        assert words in text


@pytest.mark.unit
def test_keys_never_collide_and_the_next_key_is_past_them():
    merged = merge_tagged_pdfs(list(_parts()))
    with pikepdf.open(io.BytesIO(merged)) as pdf:
        page_keys = [int(p.obj.StructParents) for p in pdf.pages]
        annot_keys = [int(a.StructParent) for p in pdf.pages for a in p.obj.get("/Annots", [])]
        footers = {
            x.objgen: int(x.StructParents)
            for p in pdf.pages for x in p.obj.Resources.XObject.values()
        }
        keys = page_keys + annot_keys + list(footers.values())
        assert len(keys) == len(set(keys))
        assert int(pdf.Root.StructTreeRoot.ParentTreeNextKey) == max(keys) + 1
        # A form XObject drawn on both pages of a part is shifted once.
        assert len(footers) == 2


@pytest.mark.unit
def test_title_and_pdfua_claim():
    merged = merge_tagged_pdfs(list(_parts(title="Part")), title="Week 3 & more")
    facts = check_structure(merged)
    assert facts["title"] == "Week 3 & more"
    assert b"Week 3 &amp; more" in facts["xmp"] and b"<pdfuaid:part>1</pdfuaid:part>" in facts["xmp"]

    first = tagged_pdf(PART_1, ua=True)
    second = tagged_pdf(PART_2, ua=False)  # iris-pdf withheld the claim
    facts = check_structure(merge_tagged_pdfs([first, second], title="T"))
    assert b"pdfuaid:part" not in facts["xmp"]


@pytest.mark.unit
def test_three_parts():
    parts = [tagged_pdf([[("P", f"Part {n}")]], table_on=0) for n in range(1, 4)]
    facts = check_structure(merge_tagged_pdfs(parts))
    assert facts["pages"] == 3
    assert facts["ids"] == [b"p1-th1", b"part2-p1-th1", b"part3-p1-th1"]


@pytest.mark.unit
def test_one_part_is_returned_as_is():
    only = tagged_pdf(PART_1)
    assert merge_tagged_pdfs([only]) is only


@pytest.mark.unit
def test_an_untagged_part_is_refused():
    with pytest.raises(PdfMergeError, match="part 2 is not tagged"):
        merge_tagged_pdfs([tagged_pdf(PART_1), tagged_pdf(PART_2, tagged=False)])
    with pytest.raises(PdfMergeError):
        merge_tagged_pdfs([tagged_pdf(PART_1), b"not a pdf"])
    with pytest.raises(PdfMergeError):
        merge_tagged_pdfs([])


@pytest.mark.unit
def test_weasyprint_tagged_pdfs_join_cleanly():
    """Real-world trees: WeasyPrint's PDF/UA output, two documents."""
    pytest.importorskip("weasyprint")
    from connector.canvas.alt_formats import render_tagged_pdf
    from connector.canvas.markdown_to_html import RenderedPage

    one = render_tagged_pdf(RenderedPage(title="A", html=(
        "<h1>Chapter</h1><p>First part <a href='https://example.org'>with a link</a>.</p>"
        "<table><tr><th scope='col'>Month</th></tr><tr><td>January</td></tr></table>"
    )))
    two = render_tagged_pdf(RenderedPage(title="B", html="<h2>Later</h2><ul><li>One</li><li>Two</li></ul>"))
    facts = check_structure(merge_tagged_pdfs([one, two], title="Chapter"))
    with pikepdf.open(io.BytesIO(one)) as a, pikepdf.open(io.BytesIO(two)) as b:
        assert facts["pages"] == len(a.pages) + len(b.pages)
    assert facts["marked"] > 0
