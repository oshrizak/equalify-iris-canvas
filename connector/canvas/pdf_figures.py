"""Extract figure images directly from the source PDF.

Two callers. Equalify Iris (the current converter) reads rasterized pages
and returns no image files at all: each ``<img>`` carries a description
and a placeholder naming its page. :func:`extract_iris_figures` maps each
page's placeholders to that page's embedded rasters, in reading order,
skipping scanned-page backgrounds, tiny images and running-head furniture
(a raster drawn on at least half the pages, and at least three, that always
sits in the top or bottom margin or is always small), and returns nothing
for a page whose match is
ambiguous: a wrong picture under a right description is worse than the
description alone, which is what the renderer shows instead.

The rest of this module serves documents converted by Equalify Reflow
before the switch, as described below.

Why this module exists: Reflow's figure-extraction pipeline emits PNG
crops with a vision-model tile/segmentation grid baked into the
output. Embedding those bytes makes the rendered review surface and
the published Canvas Page look visibly different from the original
document. The PDF's own embedded rasters carry the same imagery
without the overlay, so we pull them from there instead. Reflow
still owns the markdown and the alt text — only the image bytes
change source.

Matching strategy: Reflow tells us each figure's PAGE (1-indexed) but
not its bbox. For single-figure pages, page → image is unambiguous.
For multi-figure pages we group Reflow figures by page, sort by
``figure_id`` (Reflow assigns them in reading order), and the k-th
Reflow figure on page P maps to the k-th sub-region embedded raster
on page P sorted top-to-bottom, then left-to-right.

This works for typical academic layouts. It will misfire when:

  * The figure is rendered as PDF vector ops (charts, line drawings)
    — there is no embedded raster to extract. The caller catches
    ``PdfFigureNotFoundError`` and falls back to the upstream Reflow copy.
  * A page has more figure-shaped rasters than Reflow reports (e.g.,
    decorative icons that Reflow filtered out). The reading-order
    sort then includes the decorations and the index lands on the
    wrong image. The sub-region filter below drops obvious full-page
    layers (scanned-PDF backgrounds) but cannot tell decoration from
    figure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


class PdfFigureNotFoundError(Exception):
    """No PDF raster could be matched for the requested Reflow figure id."""


@dataclass(frozen=True)
class ExtractedFigure:
    """Bytes + content type for one image lifted from the source PDF."""

    image_bytes: bytes
    content_type: str  # ``image/png``, ``image/jpeg``, etc.


# Image formats browsers render directly. Anything else gets normalized
# to PNG via PyMuPDF's Pixmap so the iframe doesn't end up with a broken
# image icon for JPEG-2000 / TIFF / BMP sources.
_WEB_NATIVE_EXTS = {"png", "jpg", "jpeg", "gif"}

_EXT_TO_CONTENT_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
}


def extract_figure_for_reflow_id(
    pdf_bytes: bytes,
    reflow_figures: list[dict[str, Any]],
    requested_figure_id: str,
) -> ExtractedFigure:
    """Return the PDF embedded raster matching ``requested_figure_id``.

    Args:
        pdf_bytes: The original PDF source as bytes.
        reflow_figures: The ``status['figures']`` list from Reflow's
            ``/api/v1/documents/{id}`` response. Each entry needs at
            minimum ``figure_id`` and ``page``.
        requested_figure_id: The Reflow id (e.g., ``"figure-3"``) the
            caller wants.

    Raises:
        PdfFigureNotFoundError: The figure isn't in Reflow's list, its page
            metadata is missing, the page doesn't exist in the PDF, or
            no extractable raster was found at the expected reading-
            order position on that page.
    """
    target = next(
        (
            f for f in reflow_figures
            if str(f.get("figure_id") or "") == requested_figure_id
        ),
        None,
    )
    if target is None:
        raise PdfFigureNotFoundError(
            f"figure_id {requested_figure_id!r} not present in Reflow status"
        )
    page_num = int(target.get("page") or 0)
    if page_num < 1:
        raise PdfFigureNotFoundError(
            f"figure {requested_figure_id!r} has no page metadata"
        )

    # Reading-order rank among all Reflow figures on the same page.
    same_page = sorted(
        [f for f in reflow_figures if int(f.get("page") or 0) == page_num],
        key=lambda f: _figure_id_sort_key(str(f.get("figure_id") or "")),
    )
    try:
        index_on_page = next(
            i for i, f in enumerate(same_page)
            if str(f.get("figure_id") or "") == requested_figure_id
        )
    except StopIteration:  # pragma: no cover — by construction unreachable
        raise PdfFigureNotFoundError(
            f"{requested_figure_id!r} dropped out of its own page bucket"
        ) from None

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page_num > doc.page_count:
            raise PdfFigureNotFoundError(
                f"PDF has {doc.page_count} pages; Reflow expected page {page_num}"
            )
        page = doc[page_num - 1]
        page_w = page.rect.width
        page_h = page.rect.height

        # ``get_image_info(xrefs=True)`` returns one entry per image USE
        # on the page (an image used twice yields two entries) with the
        # placement bbox and the ``xref`` we can extract from.
        all_imgs = page.get_image_info(xrefs=True)

        # Drop full-page background layers. Scanned PDFs commonly have
        # the page itself as a giant image covering the whole page area;
        # that's never the figure the user means. Use an area-based
        # threshold (95% of the page) so we catch both
        # ``bbox == (0, 0, page_w, page_h)`` and the centered-with-margins
        # case (PyMuPDF preserves the source raster's aspect ratio when
        # the placement rect doesn't match exactly, leaving a few-pixel
        # margin that a strict bbox check would miss).
        page_area = page_w * page_h
        full_page_threshold = 0.95
        sub_region = [
            info for info in all_imgs
            if (info["bbox"][2] - info["bbox"][0])
               * (info["bbox"][3] - info["bbox"][1])
               < full_page_threshold * page_area
        ]
        # Reading order: top-to-bottom (y0), then left-to-right (x0).
        sub_region.sort(key=lambda info: (info["bbox"][1], info["bbox"][0]))

        if index_on_page >= len(sub_region):
            raise PdfFigureNotFoundError(
                f"page {page_num} has {len(sub_region)} extractable raster(s) "
                f"but Reflow expected at least {index_on_page + 1} "
                f"(for {requested_figure_id!r})"
            )
        chosen_xref = sub_region[index_on_page]["xref"]
        img = doc.extract_image(chosen_xref)
        ext = str(img.get("ext") or "png").lower()
        if ext in _WEB_NATIVE_EXTS:
            return ExtractedFigure(
                image_bytes=img["image"],
                content_type=_EXT_TO_CONTENT_TYPE[ext],
            )
        # JPEG-2000 / TIFF / BMP / etc. → normalize to PNG so the
        # downstream consumer (browser, Canvas Files) renders it.
        pix = fitz.Pixmap(img["image"])
        try:
            return ExtractedFigure(
                image_bytes=pix.tobytes("png"),
                content_type="image/png",
            )
        finally:
            pix = None  # noqa: F841 — release C-level buffer eagerly
    finally:
        doc.close()


def _image_bytes(doc: fitz.Document, page: fitz.Page, info: dict[str, Any]) -> ExtractedFigure:
    """Bytes for one image use on a page: the embedded raster when there is
    one, otherwise a render of the area it occupies."""
    xref = int(info.get("xref") or 0)
    if xref > 0:
        try:
            img = doc.extract_image(xref)
        except Exception:  # noqa: BLE001 - fall through to rendering the region
            img = None
        if img and img.get("image"):
            ext = str(img.get("ext") or "png").lower()
            if ext in _WEB_NATIVE_EXTS:
                return ExtractedFigure(image_bytes=img["image"], content_type=_EXT_TO_CONTENT_TYPE[ext])
            try:
                pix = fitz.Pixmap(img["image"])
                if pix.alpha or pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                return ExtractedFigure(image_bytes=pix.tobytes("png"), content_type="image/png")
            except Exception:  # noqa: BLE001
                pass
    clip = fitz.Rect(info["bbox"]) & page.rect
    if clip.is_empty:
        raise PdfFigureNotFoundError("image has no visible area on the page")
    pix = page.get_pixmap(clip=clip, dpi=200)
    return ExtractedFigure(image_bytes=pix.tobytes("png"), content_type="image/png")


# Below this, an image is an icon, a bullet or a rule rather than a figure a
# placeholder could be describing: half an inch on either side, or 0.5% of
# the page.
_MIN_SIDE_PT = 36.0
_MIN_AREA_FRACTION = 0.005
# An image drawn on this many pages, and on at least half of them, is page
# furniture (a logo in the running head) that a page agent describes once,
# if at all.
_FURNITURE_MIN_PAGES = 3


# A running head or foot lives in the top or bottom band of the page.
_MARGIN_BAND = 0.15
# ...or is small wherever it sits (a corner logo, a section icon).
_FURNITURE_MAX_AREA_FRACTION = 0.03


def _furniture_xrefs(doc: fitz.Document) -> set[int]:
    """Images that are page furniture: drawn on many pages, always in the
    margins or always small.

    Repetition alone is not enough. A course packet can legitimately repeat
    a diagram on half its pages, and PyMuPDF stores identical image bytes
    once, so every repeat shares an xref. What marks a running-head logo is
    repetition *and* placement.
    """
    pages_with: dict[int, int] = {}
    always_furniture_like: dict[int, bool] = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        h = page.rect.height or 1.0
        area = (page.rect.width * page.rect.height) or 1.0
        seen: set[int] = set()
        for info in page.get_image_info(xrefs=True):
            xref = int(info.get("xref") or 0)
            bbox = info.get("bbox")
            if xref <= 0 or not bbox:
                continue
            in_margin = bbox[3] <= _MARGIN_BAND * h or bbox[1] >= (1 - _MARGIN_BAND) * h
            small = _bbox_area(bbox) <= _FURNITURE_MAX_AREA_FRACTION * area
            always_furniture_like[xref] = always_furniture_like.get(xref, True) and (in_margin or small)
            seen.add(xref)
        for xref in seen:
            pages_with[xref] = pages_with.get(xref, 0) + 1
    return {
        x for x, n in pages_with.items()
        if n >= _FURNITURE_MIN_PAGES and n * 2 >= doc.page_count and always_furniture_like.get(x)
    }


def _bbox_area(bbox: Any) -> float:
    return max(0.0, (bbox[2] - bbox[0])) * max(0.0, (bbox[3] - bbox[1]))


def _figure_candidates(
    page: fitz.Page, furniture: set[int], *, include_furniture: bool = False,
) -> list[dict[str, Any]]:
    page_area = page.rect.width * page.rect.height or 1.0
    out = []
    for info in page.get_image_info(xrefs=True):
        bbox = info.get("bbox")
        if not bbox:
            continue
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        area = _bbox_area(bbox)
        if area >= 0.95 * page_area:          # scanned-page background
            continue
        if w < _MIN_SIDE_PT or h < _MIN_SIDE_PT or area < _MIN_AREA_FRACTION * page_area:
            continue
        if not include_furniture and int(info.get("xref") or 0) in furniture:
            continue
        out.append(info)
    return out


def _reading_order(infos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Rows first (10pt bands absorb small baseline differences between
    # side-by-side images), then left to right.
    return sorted(infos, key=lambda i: (round(i["bbox"][1] / 10.0), i["bbox"][0]))


# Text blocks narrower than this many characters say nothing about layout
# (page numbers, captions under a single image).
_LAYOUT_MIN_BLOCK_CHARS = 20
_LAYOUT_MIN_TEXT_CHARS = 200
_GUTTER_SLACK_PT = 6.0


def _side(info: dict[str, Any], mid: float) -> str:
    x0, _y0, x1, _y1 = info["bbox"]
    if x1 <= mid + _GUTTER_SLACK_PT:
        return "L"
    if x0 >= mid - _GUTTER_SLACK_PT:
        return "R"
    return "S"  # spans the gutter


def _column_order(page: fitz.Page, infos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The order a reader of a two-column page meets these images: down the
    left column, then the right, with full-width images separating bands."""
    mid = page.rect.width / 2

    def y(i: dict[str, Any]) -> float:
        return float(i["bbox"][1])

    def center(i: dict[str, Any]) -> float:
        return (float(i["bbox"][1]) + float(i["bbox"][3])) / 2

    spans = sorted((i for i in infos if _side(i, mid) == "S"), key=y)
    cols = [i for i in infos if _side(i, mid) != "S"]
    ordered: list[dict[str, Any]] = []
    top = float("-inf")
    for boundary in [*spans, None]:
        bottom = y(boundary) if boundary is not None else float("inf")
        band = [i for i in cols if top <= center(i) < bottom]
        ordered += sorted((i for i in band if _side(i, mid) == "L"), key=y)
        ordered += sorted((i for i in band if _side(i, mid) == "R"), key=y)
        if boundary is not None:
            ordered.append(boundary)
            top = bottom
    return ordered


def _two_column_text(page: fitz.Page) -> bool | None:
    """Whether the page's text is set in two columns; ``None`` when there is
    too little text to tell (a scan, a figure page)."""
    mid = page.rect.width / 2
    left = right = spanning = 0
    for block in page.get_text("blocks"):
        x0, _y0, x1, _y1, text = block[:5]
        n = len(str(text or "").strip())
        if n < _LAYOUT_MIN_BLOCK_CHARS:
            continue
        if x1 <= mid + _GUTTER_SLACK_PT:
            left += n
        elif x0 >= mid - _GUTTER_SLACK_PT:
            right += n
        else:
            spanning += n
    total = left + right + spanning
    if total < _LAYOUT_MIN_TEXT_CHARS:
        return None
    columns = left + right
    return columns >= 0.7 * total and min(left, right) >= 0.2 * columns


def _placeholder_order(
    page: fitz.Page, chosen: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Put the chosen images in the order Iris wrote their placeholders.

    Iris writes a page in reading order, which on a two-column page runs
    down the left column first. When rows-then-columns and columns-first
    disagree, the page's text decides; with no text to go by, decline.
    """
    rows = _reading_order(chosen)
    if len(chosen) < 2:
        return rows
    columns = _column_order(page, chosen)
    if [id(i) for i in columns] == [id(i) for i in rows]:
        return rows
    two_column = _two_column_text(page)
    if two_column is None:
        return None
    return columns if two_column else rows


def _choose(cands: list[dict[str, Any]], wanted: int) -> list[dict[str, Any]] | None:
    """Pick ``wanted`` images from the page's candidates, or decline.

    Equal counts map one-to-one in reading order. When the page has more
    candidates than placeholders, the largest ``wanted`` are used only if
    they are clearly larger than the rest (twice the area of the next one) —
    otherwise there is no telling which images the placeholders meant, and
    a wrong picture under a right description is worse than no picture.
    """
    if wanted <= 0 or not cands:
        return None
    if len(cands) == wanted:
        return _reading_order(cands)
    if len(cands) < wanted:
        return None
    by_size = sorted(cands, key=lambda i: _bbox_area(i["bbox"]), reverse=True)
    if _bbox_area(by_size[wanted - 1]["bbox"]) < 2 * _bbox_area(by_size[wanted]["bbox"]):
        return None
    return _reading_order(by_size[:wanted])


def extract_iris_figures(
    pdf_bytes: bytes,
    placeholders_by_page: dict[int, list[tuple[str, bool]]],
) -> dict[str, ExtractedFigure]:
    """Lift the pictures Iris described out of the source PDF.

    Iris reads rasterized pages, so it cannot hand back image files; each
    ``<img>`` it writes names the page it is on and nothing about where on
    the page. ``placeholders_by_page`` maps a 1-based source page to that
    page's figure refs in document order, each with whether it is
    decorative. For every page whose images can be matched unambiguously
    (see :func:`_choose`) the result maps ref -> image bytes; refs on pages
    that cannot be matched are simply absent, and the renderer shows their
    descriptions instead.

    Matching is tried first with every placeholder on the page, then with
    the informative ones only (decorative images are often the rules and
    bullets the size filter already removed), and only then with page
    furniture back among the candidates (for the page agent that described
    the running-head logo too, as Iris's prompt asks it to).
    """
    out: dict[str, ExtractedFigure] = {}
    if not placeholders_by_page:
        return out
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        furniture = _furniture_xrefs(doc)
        for page_no, refs in placeholders_by_page.items():
            if not (1 <= page_no <= doc.page_count) or not refs:
                continue
            page = doc[page_no - 1]
            every = [ref for ref, _decorative in refs]
            informative = [ref for ref, decorative in refs if not decorative]
            attempts: list[tuple[bool, list[str]]] = [(False, every)]
            if informative and len(informative) != len(every):
                attempts.append((False, informative))
            attempts.append((True, every))
            if informative and len(informative) != len(every):
                attempts.append((True, informative))
            chosen: list[dict[str, Any]] | None = None
            targets: list[str] = []
            n_cands = 0
            for include_furniture, wanted in attempts:
                cands = _figure_candidates(page, furniture, include_furniture=include_furniture)
                n_cands = max(n_cands, len(cands))
                chosen = _choose(cands, len(wanted))
                if chosen is not None:
                    chosen = _placeholder_order(page, chosen)
                if chosen is not None:
                    targets = wanted
                    break
            if chosen is None:
                logger.info(
                    "Iris figures: page %d has %d placeholder(s) and up to %d candidate "
                    "image(s); not matching", page_no, len(refs), n_cands,
                )
                continue
            for ref, info in zip(targets, chosen, strict=True):
                try:
                    out[ref] = _image_bytes(doc, page, info)
                except Exception as exc:  # noqa: BLE001 - one bad image must not sink the rest
                    logger.info("Iris figures: could not lift %s from page %d: %s", ref, page_no, exc)
        return out
    finally:
        doc.close()


def _figure_id_sort_key(figure_id: str) -> tuple[int, str]:
    """Sort ``figure-2`` before ``figure-10`` and ``figure-7a`` after ``figure-7``.

    Reflow's ids look like ``figure-N`` or ``figure-Na`` (the trailing
    letter denotes sub-parts of a multi-panel figure). Splitting the
    numeric prefix from the letter suffix gives a natural reading order.
    """
    tail = figure_id.removeprefix("figure-")
    digits: list[str] = []
    rest = ""
    for i, ch in enumerate(tail):
        if ch.isdigit():
            digits.append(ch)
        else:
            rest = tail[i:]
            break
    return (int("".join(digits) or "0"), rest)
