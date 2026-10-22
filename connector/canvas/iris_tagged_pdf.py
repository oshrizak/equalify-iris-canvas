"""The "Searchable PDF" format: the original document, tagged by Iris.

An Iris that runs equalify-iris-pdf hands back the PDF a session was made
from, tagged from the HTML Iris extracted (``POST /v1/sessions/{id}/pdf``):
the same pages, looking exactly as they did, with a structure tree a screen
reader can follow and a text layer search can find, scans included. That is
what a student asking for a searchable copy of the original wants, so the
overlay serves it.

A document longer than Iris's page cap was converted in parts, one session
each. Each part is tagged on its own and the parts are joined
(:mod:`connector.canvas.pdf_merge`).

Iris makes the file on request and keeps nothing, and deletes its sessions
once its retention runs out. So the connector asks once and keeps the result
on disk (``IRIS_TAGGED_PDF_DIR``) under the sessions it came from: a
document converted again gets new sessions and so a new file. Approving a
document makes its file in the background, so students do not wait for it;
purging a job's content deletes it (``state.forget_job_files``).

When Iris cannot make one (a deployment without the tagger, a part made
from images, a session Iris no longer has, a PDF the tagger refuses),
:class:`TaggedPdfUnavailable` says why, and the caller renders its own PDF.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import weakref
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from . import tagged_pdf_store as store
from .content import title_for
from .iris_client import FINISHED_STATUSES, IrisApiError, IrisClient, TaggedPdf
from .pdf_merge import PdfMergeError, merge_tagged_pdfs
from .state import CanvasJob

logger = logging.getLogger(__name__)

# How often to ask again when Iris answers "busy" (it tags two PDFs at a
# time) or asks us to slow down, and the longest single wait.
_BUSY_ATTEMPTS = 4
_BUSY_WAIT_CAP_SECONDS = 15.0

# One Iris run per document at a time: a class opening the same reading at
# once shares one run instead of queueing a dozen at Iris.
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
# Approval warms the store one document at a time, leaving Iris's other
# tagging slot to students asking for one now. One per event loop, since an
# asyncio primitive belongs to the loop it first waited on.
_warm_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_background: set[asyncio.Task[Any]] = set()


def _warm_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _warm_slots.get(loop)
    if slot is None:
        slot = _warm_slots[loop] = asyncio.Semaphore(1)
    return slot


class TaggedPdfUnavailable(Exception):  # noqa: N818 - a reason, not a failure
    """Iris cannot give this job a tagged PDF; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IrisPdf:
    pdf: bytes
    filename: str
    warnings: list[str] = field(default_factory=list)


def enabled() -> bool:
    return bool(getattr(settings, "iris_tagged_pdf", True))


def download_name(job: CanvasJob) -> str:
    """``<document>_tagged.pdf``, the name Iris itself gives the file."""
    stem = "".join(c if c.isalnum() or c in "-._ " else "_" for c in title_for(job)).strip()
    return f"{stem or 'document'}_tagged.pdf"


def _finished_parts(job: CanvasJob) -> list[dict[str, Any]]:
    if not job.is_iris:
        raise TaggedPdfUnavailable("converted by Reflow, which has no tagged PDF")
    parts = [p for p in (job.iris_parts or []) if isinstance(p, dict)]
    if not parts:
        raise TaggedPdfUnavailable("the job has no Iris sessions")
    for part in parts:
        if not part.get("session_id"):
            raise TaggedPdfUnavailable("a part was never uploaded to Iris")
        if part.get("status") not in FINISHED_STATUSES:
            raise TaggedPdfUnavailable(
                f"session {part['session_id']} is {part.get('status') or 'unknown'}, not finished"
            )
    return sorted(parts, key=lambda p: int(p.get("first_page") or 0))


def _fingerprint(job: CanvasJob, parts: list[dict[str, Any]]) -> str:
    raw = json.dumps(
        {"sessions": [str(p["session_id"]) for p in parts], "completed_at": job.completed_at},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---- asking Iris ---------------------------------------------------------

def _why(exc: IrisApiError, session_id: str) -> str:
    code = exc.code or ""
    if code == "tagged_pdf_unavailable":
        return "this Iris does not make tagged PDFs"
    if code in ("session_not_found", "session_expired") or exc.status_code == 410:
        return f"Iris no longer has session {session_id}"
    if code == "no_source_pdf":
        return f"session {session_id} was not made from one PDF"
    if code == "invalid_state":
        return f"session {session_id} has not finished"
    if exc.status_code == 504 or code == "timeout":
        return f"Iris timed out tagging session {session_id}"
    if exc.status_code in (400, 422):
        return f"Iris's tagger refused session {session_id} ({code or exc.status_code})"
    return str(exc)


async def _tag_part(iris: IrisClient, session_id: str) -> TaggedPdf:
    for attempt in range(1, _BUSY_ATTEMPTS + 1):
        try:
            return await iris.tagged_pdf(session_id)
        except IrisApiError as exc:
            if exc.status_code in (429, 503) and attempt < _BUSY_ATTEMPTS:
                await asyncio.sleep(min(exc.retry_after or 5.0, _BUSY_WAIT_CAP_SECONDS))
                continue
            raise TaggedPdfUnavailable(_why(exc, session_id)) from exc
    raise TaggedPdfUnavailable(f"Iris stayed busy tagging session {session_id}")


async def iris_tagged_pdf(job: CanvasJob, *, iris: IrisClient | None = None) -> IrisPdf:
    """The job's original PDF, tagged by Iris: from the store, else made now."""
    if not enabled():
        raise TaggedPdfUnavailable("turned off (IRIS_TAGGED_PDF=false)")
    parts = _finished_parts(job)
    key = _fingerprint(job, parts)
    job_id = job.reflow_job_id
    name = download_name(job)

    stored = await asyncio.to_thread(store.read, job_id, key)
    if stored is not None:
        return IrisPdf(stored, name)

    lock = _locks.get(job_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[job_id] = lock
    async with lock:
        # Another request may have made it while this one waited.
        stored = await asyncio.to_thread(store.read, job_id, key)
        if stored is not None:
            return IrisPdf(stored, name)

        iris = iris or IrisClient()
        try:
            offered = (await iris.limits()).tagged_pdf
        except IrisApiError as exc:
            raise TaggedPdfUnavailable(f"could not read Iris's limits: {exc}") from exc
        if not offered:
            raise TaggedPdfUnavailable("this Iris does not make tagged PDFs")

        pieces: list[bytes] = []
        warnings: list[str] = []
        for part in parts:
            tagged = await _tag_part(iris, str(part["session_id"]))
            pieces.append(tagged.pdf)
            warnings.extend(w for w in tagged.warning_codes if w not in warnings)
        if len(pieces) == 1:
            pdf = pieces[0]
        else:
            try:
                pdf = await asyncio.to_thread(merge_tagged_pdfs, pieces, title=title_for(job))
            except PdfMergeError as exc:
                raise TaggedPdfUnavailable(f"could not join the tagged parts: {exc}") from exc

        await asyncio.to_thread(store.write, job_id, key, pdf)
        logger.info(
            "Iris tagged PDF for job %s: %d part(s), %d bytes%s",
            job_id, len(pieces), len(pdf),
            f", tagger warnings: {', '.join(warnings)}" if warnings else "",
        )
        return IrisPdf(pdf, name, warnings)


def warm_tagged_pdf(job: CanvasJob) -> None:
    """Make the job's tagged PDF in the background (on approval), so the
    first student to ask gets it at once. Best-effort, and quiet when Iris
    cannot make one: the format falls back on its own."""
    if not enabled() or not job.is_iris:
        return

    async def _run() -> None:
        async with _warm_slot():
            try:
                await iris_tagged_pdf(job)
            except TaggedPdfUnavailable as exc:
                logger.info("No Iris tagged PDF for job %s: %s", job.reflow_job_id, exc.reason)
            except Exception:  # noqa: BLE001 - housekeeping must not surface
                logger.exception("Making the tagged PDF for job %s failed", job.reflow_job_id)

    task = asyncio.create_task(_run())
    _background.add(task)
    task.add_done_callback(_background.discard)
