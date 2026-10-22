"""What faculty should know about an Iris conversion before approving it.

Iris is unusually candid about its own output: it says which pages it could
not extract, which pages failed its fidelity check and were not repaired,
and which accessibility issues its reviewer left open. It says all of that
in HTML comments, which no reader of a Canvas Page ever sees, so
:mod:`connector.canvas.iris_output` lifts them onto the job and this module
turns them into plain-language notes for the review screen.

Notes are ordered by how much they should change the reviewer's decision:
missing content first, then content Iris itself doubts, then open issues,
then things worth a glance.
"""

from __future__ import annotations

from typing import Any

from .iris_output import IrisReport, figures_from_manifest


def format_pages(pages: list[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``"3-5 and 9"``."""
    nums = sorted({int(p) for p in pages})
    if not nums:
        return ""
    runs: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = n
    runs.append(f"{start}-{prev}" if prev > start else str(start))
    if len(runs) == 1:
        return runs[0]
    return ", ".join(runs[:-1]) + " and " + runs[-1]


def _page_word(pages: list[int]) -> str:
    return "page" if len(set(pages)) == 1 else "pages"


def notes_for_job(job: Any) -> list[dict[str, Any]]:
    """Plain-language notes for one Iris job, most important first."""
    if not getattr(job, "is_iris", False) or not getattr(job, "completed_at", None):
        return []
    report = IrisReport.from_dict(getattr(job, "iris_report", None))
    notes: list[dict[str, Any]] = []

    if report.failed_pages:
        pages = report.failed_pages
        notes.append({
            "severity": "error",
            "code": "pages_missing",
            "pages": pages,
            "message": (
                f"Iris could not convert {_page_word(pages)} {format_pages(pages)}. "
                "That content is missing from the accessible version. Add it in the "
                "editor, or convert the document again."
            ),
        })
    if report.uncorrected_pages:
        pages = report.uncorrected_pages
        notes.append({
            "severity": "warning",
            "code": "pages_unverified",
            "pages": pages,
            "message": (
                f"{_page_word(pages).capitalize()} {format_pages(pages)} did not pass "
                "Iris's own accuracy check and could not be fixed automatically. "
                "Compare them with the PDF before approving; a table that lost rows "
                "is the usual problem."
            ),
        })
    if report.unresolved:
        notes.append({
            "severity": "warning",
            "code": "open_issues",
            "message": (
                f"Iris's reviewer left {len(report.unresolved)} accessibility "
                f"issue{'s' if len(report.unresolved) != 1 else ''} open:"
            ),
            "items": report.unresolved[:25],
        })
    if report.review_unread:
        notes.append({
            "severity": "warning",
            "code": "review_incomplete",
            "message": (
                "Part of this document was not reviewed by Iris's reader, so reading "
                "order and structure there were not checked. Read it through before "
                "approving."
            ),
        })
    if report.lint_unavailable:
        notes.append({
            "severity": "warning",
            "code": "checks_not_run",
            "message": (
                "Iris could not run its automated accessibility checks on this document."
            ),
        })
    if report.editor_truncated:
        notes.append({
            "severity": "info",
            "code": "corrections_incomplete",
            "message": (
                "Iris's copy editor could not finish a round of corrections on this "
                "document, so some listed issues may already be fixed and others may "
                "remain."
            ),
        })

    unmatched = set(getattr(job, "iris_figures_unmatched", None) or [])
    figures = [
        f for f in figures_from_manifest(getattr(job, "iris_figures", None))
        if not f.decorative and (f.ref in unmatched or not f.page)
    ]
    if figures:
        pages = sorted({int(f.page) for f in figures if f.page})
        where = f" (on {_page_word(pages)} {format_pages(pages)})" if pages else ""
        notes.append({
            "severity": "info",
            "code": "figures_as_text",
            "message": (
                f"{len(figures)} image{'s' if len(figures) != 1 else ''}{where} could "
                "not be copied out of the PDF, so the description is shown as text in "
                "place of the picture."
            ),
        })

    parts = getattr(job, "iris_parts", None) or []
    if len(parts) > 1:
        seams = [int(p["last_page"]) for p in sorted(parts, key=lambda p: int(p["first_page"]))[:-1]]
        seam_text = ", ".join(f"{n}-{n + 1}" for n in seams)
        notes.append({
            "severity": "info",
            "code": "converted_in_parts",
            "message": (
                f"This {job.source_page_count or ''}-page document was converted in "
                f"{len(parts)} parts. Check that anything crossing pages {seam_text} "
                "(a table, a sentence, a list) reads correctly."
            ).replace("This -page", "This"),
        })
    return notes
