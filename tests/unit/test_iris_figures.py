"""Lifting the pictures Iris described back out of the source PDF.

Iris reads rasterized pages and returns only a placeholder per image naming
its page, never where on the page. The matcher must map placeholders to the
page's own rasters when that is unambiguous, and decline (so the renderer
shows the description) when it is not: a wrong picture under a right
description is worse than no picture.
"""

from __future__ import annotations

import fitz
from connector.canvas.pdf_figures import extract_iris_figures


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, rgb)
    return pix.tobytes("png")


LOGO = _png(120, 40, (200, 30, 30))


def _doc(pages: list[list[tuple[fitz.Rect, bytes]]], *, logo: bool = True) -> bytes:
    doc = fitz.open()
    for images in pages:
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 120), "Body text.")
        if logo:
            page.insert_image(fitz.Rect(450, 30, 570, 70), stream=LOGO)  # running head
        for rect, png in images:
            page.insert_image(rect, stream=png)
    return doc.tobytes()


MIDDLE = fitz.Rect(150, 300, 450, 500)
TOP_LEFT = fitz.Rect(72, 150, 272, 290)
TOP_RIGHT = fitz.Rect(320, 152, 540, 290)


def test_one_picture_one_placeholder():
    pdf = _doc([[(MIDDLE, _png(300, 200, (10, 120, 200)))], [], []])  # logo on 3 pages: furniture
    out = extract_iris_figures(pdf, {1: [("figures/p1-1.png", False)]})
    assert set(out) == {"figures/p1-1.png"}
    assert out["figures/p1-1.png"].content_type == "image/png"


def test_running_head_logo_is_furniture_not_a_figure():
    # Three pages, each with the same small logo in the top margin and one
    # real chart: the chart is the match, the logo never competes for it.
    pages = [[(MIDDLE, _png(300, 200, (n * 40, 90, 160)))] for n in range(3)]
    pdf = _doc(pages)
    out = extract_iris_figures(pdf, {2: [("figures/p2-1.png", False)]})
    fig = fitz.Pixmap(out["figures/p2-1.png"].image_bytes)
    assert (fig.width, fig.height) == (300, 200)


def test_a_diagram_repeated_mid_page_is_still_a_figure():
    # Course packets repeat diagrams; identical bytes share one xref. Being
    # repeated is not enough to be furniture, being in the margin is.
    same = _png(300, 200, (1, 2, 3))
    pdf = _doc([[(MIDDLE, same)] for _ in range(4)])
    out = extract_iris_figures(pdf, {3: [("figures/p3-1.png", False)]})
    assert "figures/p3-1.png" in out


def test_two_pictures_map_in_reading_order():
    left, right = _png(200, 140, (255, 0, 0)), _png(220, 138, (0, 0, 255))
    pdf = _doc([[(TOP_RIGHT, right), (TOP_LEFT, left)]], logo=False)
    out = extract_iris_figures(pdf, {1: [("figures/p1-1.png", False), ("figures/p1-2.png", False)]})
    first = fitz.Pixmap(out["figures/p1-1.png"].image_bytes)
    second = fitz.Pixmap(out["figures/p1-2.png"].image_bytes)
    assert (first.width, second.width) == (200, 220)  # left before right


def test_more_placeholders_than_pictures_declines():
    pdf = _doc([[(MIDDLE, _png(300, 200, (9, 9, 9)))]], logo=False)
    out = extract_iris_figures(pdf, {1: [("figures/p1-1.png", False), ("figures/p1-2.png", False)]})
    assert out == {}


def test_two_similar_pictures_for_one_placeholder_declines():
    a, b = _png(200, 140, (1, 1, 1)), _png(220, 138, (2, 2, 2))
    pdf = _doc([[(TOP_LEFT, a), (TOP_RIGHT, b)]], logo=False)
    assert extract_iris_figures(pdf, {1: [("figures/p1-1.png", False)]}) == {}


def test_one_dominant_picture_for_one_placeholder_matches():
    big, small = _png(400, 300, (5, 5, 5)), _png(60, 60, (6, 6, 6))
    pdf = _doc([[(fitz.Rect(100, 250, 500, 550), big), (fitz.Rect(72, 600, 140, 668), small)]], logo=False)
    out = extract_iris_figures(pdf, {1: [("figures/p1-1.png", False)]})
    assert fitz.Pixmap(out["figures/p1-1.png"].image_bytes).width == 400


def test_decorative_placeholders_are_dropped_from_the_count_when_that_matches():
    # Iris wrote a chart and a decorative rule; the rule was too thin to be a
    # candidate. Matching with the informative placeholder alone succeeds.
    pdf = _doc([[(MIDDLE, _png(300, 200, (7, 7, 7)))]], logo=False)
    out = extract_iris_figures(pdf, {1: [("figures/p1-1.png", False), ("figures/p1-2.png", True)]})
    assert set(out) == {"figures/p1-1.png"}


def test_scanned_page_background_is_never_a_figure():
    scan = _png(612, 792, (240, 240, 240))
    pdf = _doc([[(fitz.Rect(0, 0, 612, 792), scan)]], logo=False)
    assert extract_iris_figures(pdf, {1: [("figures/p1-1.png", False)]}) == {}


def test_pages_outside_the_pdf_are_ignored():
    pdf = _doc([[(MIDDLE, _png(300, 200, (3, 3, 3)))]])
    assert extract_iris_figures(pdf, {9: [("figures/p9-1.png", False)]}) == {}


def test_a_described_running_head_logo_is_matched_after_all():
    # Iris's page agent is told to emit logos as images, so a letterhead
    # page may have a placeholder for the logo AND one for the chart. The
    # furniture rule first hides the logo; when that leaves too few
    # candidates the logo is allowed back, and order decides.
    pages = [[(MIDDLE, _png(300, 200, (n * 30, 9, 9)))] for n in range(3)]
    pdf = _doc(pages)
    out = extract_iris_figures(
        pdf, {2: [("figures/p2-1.png", False), ("figures/p2-2.png", False)]},
    )
    assert fitz.Pixmap(out["figures/p2-1.png"].image_bytes).width == 120  # logo, top
    assert fitz.Pixmap(out["figures/p2-2.png"].image_bytes).width == 300  # chart


def test_a_single_page_with_a_logo_and_a_chart_maps_both():
    pdf = _doc([[(MIDDLE, _png(300, 200, (4, 4, 4)))]])  # logo too, one page: no furniture
    out = extract_iris_figures(pdf, {1: [("figures/p1-1.png", False), ("figures/p1-2.png", False)]})
    assert set(out) == {"figures/p1-1.png", "figures/p1-2.png"}


LOREM = ("Students read the primary sources and compare the arguments made in each "
         "of the three essays assigned for this week. ") * 6


def _layout_page(*, text: str | None) -> bytes:
    """A lower-left and an upper-right picture, with text set in two columns,
    in one column, or not at all."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    if text == "two":
        page.insert_textbox(fitz.Rect(50, 60, 290, 380), LOREM, fontsize=9)
        page.insert_textbox(fitz.Rect(322, 400, 562, 740), LOREM, fontsize=9)
    elif text == "one":
        page.insert_textbox(fitz.Rect(50, 60, 562, 200), LOREM, fontsize=9)
    page.insert_image(fitz.Rect(60, 420, 280, 560), stream=_png(220, 140, (200, 0, 0)))  # lower left
    page.insert_image(fitz.Rect(330, 120, 552, 262), stream=_png(222, 142, (0, 0, 200)))  # upper right
    return doc.tobytes()


def _red(fig) -> bool:
    pix = fitz.Pixmap(fig.image_bytes)
    r, g, b = pix.pixel(5, 5)[:3]
    return r > 150 and b < 100


def test_two_column_pages_map_down_the_left_column_first():
    out = extract_iris_figures(
        _layout_page(text="two"), {1: [("figures/p1-1.png", False), ("figures/p1-2.png", False)]},
    )
    assert _red(out["figures/p1-1.png"]) and not _red(out["figures/p1-2.png"])


def test_one_column_pages_map_top_to_bottom():
    out = extract_iris_figures(
        _layout_page(text="one"), {1: [("figures/p1-1.png", False), ("figures/p1-2.png", False)]},
    )
    assert not _red(out["figures/p1-1.png"]) and _red(out["figures/p1-2.png"])


def test_column_order_unknown_without_text_declines():
    out = extract_iris_figures(
        _layout_page(text=None), {1: [("figures/p1-1.png", False), ("figures/p1-2.png", False)]},
    )
    assert out == {}
