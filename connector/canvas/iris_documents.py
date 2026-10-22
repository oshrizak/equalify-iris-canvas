"""Turning a Canvas file into what Iris accepts.

Iris takes PDFs (and page images), and at most ``GET /v1/limits``'s
``pdf.max_pages`` pages per session — 25 on today's builds, a constant in
Iris rather than a setting. Course documents are routinely longer than that
(readings, slide decks exported to PDF, lab manuals), so a long PDF is cut
into consecutive parts, each part becomes its own Iris session, and the
bridge stitches the parts' HTML back into one document
(:mod:`connector.canvas.iris_output`).

What a cut costs, so nobody is surprised by it: Iris's reader/copy-editor
review loop sees one part at a time, so a table or a sentence that runs
across a part boundary is joined by nobody, and heading levels are checked
within a part rather than across the whole document. The parts are as large
as Iris allows to keep the number of seams down, and the seams fall on page
boundaries, never inside a page.

Word and PowerPoint files are converted to PDF with LibreOffice first, when
it is installed; Reflow accepted them directly (Docling), Iris does not.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from ..config import settings

logger = logging.getLogger(__name__)

PDF_EXTENSIONS = (".pdf",)
OFFICE_EXTENSIONS = (".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".rtf")
OFFICE_MIME_TYPES = frozenset({
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.presentation",
    "application/rtf",
})


class DocumentPrepError(Exception):
    """The file cannot be turned into something Iris accepts.

    The message is written for the faculty member who uploaded the file: it
    is shown on the document's row as the reason it was not converted.
    """


@dataclass(frozen=True)
class PdfPart:
    """One Iris session's worth of a source PDF (pages are 1-based, inclusive)."""

    index: int
    first_page: int
    last_page: int
    pdf_bytes: bytes

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1


def is_pdf(filename: str | None, content_type: str | None = None) -> bool:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    return name.endswith(PDF_EXTENSIONS) or ctype in ("application/pdf", "pdf")


def is_office_document(filename: str | None, content_type: str | None = None) -> bool:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    return name.endswith(OFFICE_EXTENSIONS) or ctype in OFFICE_MIME_TYPES


def soffice_binary() -> str | None:
    """Path to LibreOffice's ``soffice``, or ``None`` when it is not installed
    or Office conversion is switched off."""
    if not bool(getattr(settings, "iris_convert_office_documents", True)):
        return None
    configured = str(getattr(settings, "iris_soffice_path", "soffice") or "soffice")
    if os.path.isabs(configured):
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which(configured)


def office_to_pdf(filename: str, content: bytes) -> bytes:
    """Convert a Word/PowerPoint/OpenDocument file to PDF with LibreOffice.

    Runs ``soffice --headless --convert-to pdf`` in a throwaway directory
    with its own user profile, so concurrent conversions cannot collide on
    LibreOffice's profile lock and nothing is left behind.
    """
    binary = soffice_binary()
    if binary is None:
        raise DocumentPrepError(
            "Word and PowerPoint files have to be converted to PDF before Iris can "
            "read them, and LibreOffice is not installed where the connector runs. "
            "Upload a PDF of this file instead."
        )
    timeout = int(getattr(settings, "iris_soffice_timeout_seconds", 180) or 180)
    suffix = Path(filename).suffix.lower() or ".docx"
    with tempfile.TemporaryDirectory(prefix="iris-office-") as tmp:
        src = Path(tmp) / f"source{suffix}"
        src.write_bytes(content)
        profile = Path(tmp) / "profile"
        cmd = [
            binary,
            f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--nologo",
            "--convert-to", "pdf",
            "--outdir", tmp,
            str(src),
        ]
        # LibreOffice reads an untrusted file here; it gets no secrets from
        # the connector's environment, only what it needs to run.
        env = {
            "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
            "HOME": tmp,
            "TMPDIR": tmp,
            "LANG": "C.UTF-8",
        }
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd, capture_output=True, timeout=timeout, check=False, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentPrepError(
                f"Converting this file to PDF took longer than {timeout} seconds."
            ) from exc
        out = Path(tmp) / "source.pdf"
        if proc.returncode != 0 or not out.exists():
            logger.warning(
                "soffice failed for %s (rc=%s): %s",
                filename, proc.returncode, (proc.stderr or b"")[-500:].decode("utf-8", "replace"),
            )
            raise DocumentPrepError(
                "This file could not be converted to PDF. If it opens in Word or "
                "PowerPoint, save it as a PDF and upload that instead."
            )
        return out.read_bytes()


def _open_pdf(pdf_bytes: bytes) -> fitz.Document:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any parser failure means "not a PDF we can read"
        raise DocumentPrepError("This file is not a readable PDF.") from exc
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise DocumentPrepError(
            "This PDF is password-protected. Remove the password and upload it again."
        )
    return doc


def pdf_page_count(pdf_bytes: bytes) -> int:
    doc = _open_pdf(pdf_bytes)
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def plan_parts(page_count: int, max_pages: int) -> list[tuple[int, int]]:
    """Consecutive ``(first_page, last_page)`` ranges of at most ``max_pages``.

    Parts are balanced rather than greedy (26 pages at a cap of 25 becomes
    13 + 13, not 25 + 1): a one-page tail is a part with nothing around it
    for Iris's reviewer to read against, and it costs a session of its own.
    """
    if page_count <= 0:
        return []
    max_pages = max(1, int(max_pages))
    n_parts = -(-page_count // max_pages)  # ceil
    base, extra = divmod(page_count, n_parts)
    ranges: list[tuple[int, int]] = []
    start = 1
    for i in range(n_parts):
        size = base + (1 if i < extra else 0)
        ranges.append((start, start + size - 1))
        start += size
    return ranges


def split_pdf(pdf_bytes: bytes, max_pages: int) -> list[PdfPart]:
    """Cut a PDF into parts Iris accepts, keeping page order.

    A document that already fits is passed through byte for byte: Iris keeps
    the uploaded PDF of a one-file session (it is what its optional tagged-PDF
    route tags), so there is no reason to hand it a re-written copy.
    """
    doc = _open_pdf(pdf_bytes)
    try:
        total = int(doc.page_count)
        if total == 0:
            raise DocumentPrepError("This PDF has no pages.")
        ranges = plan_parts(total, max_pages)
        if len(ranges) == 1:
            return [PdfPart(index=0, first_page=1, last_page=total, pdf_bytes=pdf_bytes)]
        parts: list[PdfPart] = []
        for i, (first, last) in enumerate(ranges):
            out = fitz.open()
            try:
                # Links and annotations come along by default, which matters:
                # Iris reads link targets out of the file (they do not survive
                # rasterizing) and treats them as ground truth for the page.
                out.insert_pdf(doc, from_page=first - 1, to_page=last - 1)
                data = out.tobytes(garbage=3, deflate=True)
            except Exception as exc:  # noqa: BLE001
                raise DocumentPrepError(
                    "This PDF could not be split into parts for conversion."
                ) from exc
            finally:
                out.close()
            parts.append(PdfPart(index=i, first_page=first, last_page=last, pdf_bytes=data))
        return parts
    finally:
        doc.close()


def part_bytes(pdf_bytes: bytes, first_page: int, last_page: int, total_pages: int) -> bytes:
    """Re-cut one part of a PDF (used when a part's upload is retried later).

    The cut is deterministic, so re-cutting the same source yields the same
    pages the original plan named.
    """
    if first_page == 1 and last_page == total_pages:
        return pdf_bytes
    doc = _open_pdf(pdf_bytes)
    try:
        out = fitz.open()
        try:
            out.insert_pdf(doc, from_page=first_page - 1, to_page=last_page - 1)
            return out.tobytes(garbage=3, deflate=True)
        finally:
            out.close()
    finally:
        doc.close()
