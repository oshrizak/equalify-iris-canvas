"""Cutting documents into what Iris accepts (25 pages a session today)."""

from __future__ import annotations

import fitz
import pytest
from connector.canvas import iris_documents
from connector.canvas.iris_documents import (
    DocumentPrepError,
    is_office_document,
    office_to_pdf,
    part_bytes,
    pdf_page_count,
    plan_parts,
    split_pdf,
)


def _pdf(pages: int, *, link_on: int | None = None) -> bytes:
    doc = fitz.open()
    for n in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {n}")
        if link_on == n:
            page.insert_link({
                "kind": fitz.LINK_URI, "from": fitz.Rect(72, 60, 200, 80),
                "uri": "https://example.edu/syllabus",
            })
    return doc.tobytes()


@pytest.mark.parametrize(
    ("pages", "cap", "expected"),
    [
        (0, 25, []),
        (1, 25, [(1, 1)]),
        (25, 25, [(1, 25)]),
        # Balanced, not greedy: 25 + 1 would strand one page in a session
        # with nothing around it for Iris's reviewer to read against.
        (26, 25, [(1, 13), (14, 26)]),
        (51, 25, [(1, 17), (18, 34), (35, 51)]),
        (100, 25, [(1, 25), (26, 50), (51, 75), (76, 100)]),
    ],
)
def test_plan_parts(pages, cap, expected):
    assert plan_parts(pages, cap) == expected


def test_a_document_that_fits_is_passed_through_byte_for_byte():
    raw = _pdf(10)
    parts = split_pdf(raw, 25)
    assert len(parts) == 1
    assert parts[0].pdf_bytes is raw
    assert (parts[0].first_page, parts[0].last_page) == (1, 10)


def test_a_long_document_is_cut_on_page_boundaries_in_order():
    parts = split_pdf(_pdf(30), 25)
    assert [(p.first_page, p.last_page) for p in parts] == [(1, 15), (16, 30)]
    second = fitz.open(stream=parts[1].pdf_bytes, filetype="pdf")
    assert second.page_count == 15
    assert "Page 16" in second[0].get_text()
    assert "Page 30" in second[-1].get_text()


def test_link_annotations_survive_the_cut():
    # Iris reads link targets out of the file (rasterizing loses them) and
    # treats them as ground truth, so a part must keep its page's links.
    parts = split_pdf(_pdf(30, link_on=20), 25)
    second = fitz.open(stream=parts[1].pdf_bytes, filetype="pdf")
    uris = [link.get("uri") for page in second for link in page.get_links()]
    assert "https://example.edu/syllabus" in uris


def test_recutting_a_part_is_deterministic():
    raw = _pdf(30)
    planned = split_pdf(raw, 25)[1]
    again = part_bytes(raw, planned.first_page, planned.last_page, 30)
    a = fitz.open(stream=planned.pdf_bytes, filetype="pdf")
    b = fitz.open(stream=again, filetype="pdf")
    assert [p.get_text() for p in a] == [p.get_text() for p in b]


def test_password_protected_pdf_is_refused_with_a_reason():
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    locked = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(DocumentPrepError, match="password"):
        pdf_page_count(locked)


def test_not_a_pdf_is_refused_with_a_reason():
    with pytest.raises(DocumentPrepError, match="not a readable PDF"):
        pdf_page_count(b"PK\x03\x04 this is a zip")


def test_office_detection():
    assert is_office_document("Syllabus.DOCX")
    assert is_office_document("deck.pptx")
    assert is_office_document("x", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert not is_office_document("reading.pdf", "application/pdf")


def test_office_conversion_without_libreoffice_says_what_to_do(monkeypatch):
    monkeypatch.setattr(iris_documents, "soffice_binary", lambda: None)
    with pytest.raises(DocumentPrepError, match="Upload a PDF"):
        office_to_pdf("Syllabus.docx", b"PK")
