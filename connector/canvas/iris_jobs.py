"""The life of an Iris conversion, from Canvas file to stored document.

Split out of the workers so the watcher (which starts conversions) and the
bridge (which finishes them, and retries uploads Iris could not take yet)
share one implementation of each step:

1. :func:`create_job` — prepare the file (Office -> PDF, page count, cost
   check), cut it into parts Iris accepts, write the job record, and try to
   upload every part.
2. :func:`upload_pending_parts` — upload the parts that have no Iris session
   yet. Called again by the bridge whenever Iris was busy, throttling us, or
   down.
3. :func:`poll_parts` — read each part's session status.
4. :func:`complete_job` — fetch every part's HTML, stitch it, and store the
   document, its figure manifest and Iris's report on the job.
5. :func:`requeue_job` — ask for a document again ("Try again").

Uploads honor Iris's throttling: a 429 (or Iris being unreachable) sets a
deployment-wide back-off in Redis that both workers respect, so a course
with fifty new PDFs is fed to Iris at the rate it asks for instead of being
hammered every tick.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from redis.asyncio import Redis

from ..config import settings
from .iris_client import (
    FINISHED_STATUSES,
    IN_FLIGHT_STATUSES,
    STATUS_FAILED,
    IrisApiError,
    IrisClient,
)
from .iris_documents import (
    DocumentPrepError,
    is_office_document,
    office_to_pdf,
    part_bytes,
    pdf_page_count,
    plan_parts,
)
from .iris_output import IrisPartInfo, stitch
from .sanitize import sanitize_html
from .signals import derive_signals_from_html
from .state import (
    CanvasJob,
    mark_iris_activity,
    new_job_id,
    put_iris_body,
    put_job,
)
from .tenant import tk

logger = logging.getLogger(__name__)

BACKOFF_KEY = tk("canvas:iris-upload-backoff")
UPLOAD_LOCK_KEY = tk("canvas:iris-upload-lock:{job_id}")
_DEFAULT_BACKOFF_SECONDS = 60
_MAX_BACKOFF_SECONDS = 15 * 60
# Long enough to cover every part of a large document uploading (each upload
# waits for Iris to rasterize its pages), short enough that a crashed worker
# does not strand the job for long.
_UPLOAD_LOCK_SECONDS = 30 * 60


async def acquire_upload_lock(redis: Redis, job_id: str) -> bool:
    """Only one worker uploads a job's parts at a time.

    The watcher starts uploading as soon as it creates a job, and the bridge
    retries pending parts on its own schedule; without this they could both
    upload the same part and Iris would convert (and bill) it twice.
    """
    return bool(await redis.set(
        UPLOAD_LOCK_KEY.format(job_id=job_id), "1", nx=True, ex=_UPLOAD_LOCK_SECONDS,
    ))


async def release_upload_lock(redis: Redis, job_id: str) -> None:
    await redis.delete(UPLOAD_LOCK_KEY.format(job_id=job_id))


async def upload_locked(redis: Redis, job_id: str) -> bool:
    return bool(await redis.exists(UPLOAD_LOCK_KEY.format(job_id=job_id)))


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

async def upload_backoff_active(redis: Redis) -> bool:
    return bool(await redis.exists(BACKOFF_KEY))


async def upload_throttled(redis: Redis) -> bool:
    """True when uploads are paused because Iris asked us to slow down (429),
    as opposed to Iris being unreachable or refusing our token."""
    raw = await redis.get(BACKOFF_KEY)
    if raw is None:
        return False
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    return text.startswith("429")


async def set_upload_backoff(
    redis: Redis, seconds: float | None, reason: str, *, status_code: int | None = None,
) -> None:
    wait = int(min(_MAX_BACKOFF_SECONDS, max(5, seconds or _DEFAULT_BACKOFF_SECONDS)))
    await redis.set(BACKOFF_KEY, f"{status_code or 'unreachable'}: {reason}"[:200], ex=wait)
    logger.warning("Iris uploads paused for %ss: %s", wait, reason)


# ---------------------------------------------------------------------------
# Preparing a document
# ---------------------------------------------------------------------------

@dataclass
class PreparedDocument:
    pdf_bytes: bytes
    page_count: int
    has_text_layer: bool
    sha256: str


def prepare_document(filename: str, content: bytes) -> PreparedDocument:
    """Turn a Canvas file's bytes into the PDF Iris will read.

    Raises :class:`DocumentPrepError` with a faculty-readable reason when
    that is not possible (not a PDF, password-protected, too long, ...).
    """
    pdf = office_to_pdf(filename, content) if is_office_document(filename) else content
    pages = pdf_page_count(pdf)
    if pages <= 0:
        raise DocumentPrepError("This PDF has no pages.")
    limit = int(getattr(settings, "iris_max_document_pages", 0) or 0)
    if limit and pages > limit:
        raise DocumentPrepError(
            f"This document has {pages} pages, more than the {limit}-page limit "
            "for automatic conversion. Split it into smaller files, or ask your "
            "Canvas administrator about converting it."
        )
    from .alt_formats import pdf_has_text_layer

    return PreparedDocument(
        pdf_bytes=pdf,
        page_count=pages,
        has_text_layer=pdf_has_text_layer(pdf),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def max_pages_per_session(iris: IrisClient) -> int:
    configured = int(getattr(settings, "iris_max_pages_per_session", 0) or 0)
    try:
        published = (await iris.limits()).max_pages
    except IrisApiError as exc:
        logger.warning("Could not read Iris limits (%s); assuming 25 pages per session", exc)
        published = 25
    if configured:
        return max(1, min(configured, published))
    return max(1, published)


def estimate_cost_cents(pages: int) -> int:
    per_page = int(getattr(settings, "iris_estimated_cost_per_page_cents", 11) or 0)
    return max(0, pages) * per_page


def _part_name(filename: str) -> str:
    """What Iris is told the file is called.

    Every part keeps the document's own name: Iris shows each page to its
    model as "<name>, page N of M", and a name carrying "pages 26-50" would
    invite the model to number the placeholders it writes from 26.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem or 'document'}.pdf"


def new_job(
    *,
    course_id: str,
    file_id: str,
    filename: str,
    uploader: str,
    platform_id: str | None,
    prepared: PreparedDocument,
    max_pages: int,
    reserved_cents: int | None,
) -> CanvasJob:
    parts = [
        {
            "index": i,
            "first_page": first,
            "last_page": last,
            "session_id": None,
            "status": "pending",
            "error": None,
        }
        for i, (first, last) in enumerate(plan_parts(prepared.page_count, max_pages))
    ]
    return CanvasJob(
        reflow_job_id=new_job_id(),
        canvas_file_id=file_id,
        canvas_file_name=filename,
        canvas_course_id=course_id,
        canvas_user_id=uploader,
        status="processing",
        created_at=time.time(),
        platform_id=platform_id,
        backend="iris",
        iris_parts=parts,
        source_page_count=prepared.page_count,
        source_sha256=prepared.sha256,
        source_has_text_layer=prepared.has_text_layer,
        reserved_cost_cents=reserved_cents,
    )


def failed_job(
    *,
    course_id: str,
    file_id: str,
    filename: str,
    uploader: str,
    platform_id: str | None,
    error: str,
    sha256: str | None = None,
) -> CanvasJob:
    """A record for a file that cannot be converted, so faculty see why."""
    return CanvasJob(
        reflow_job_id=new_job_id(),
        canvas_file_id=file_id,
        canvas_file_name=filename,
        canvas_course_id=course_id,
        canvas_user_id=uploader,
        status="failed",
        created_at=time.time(),
        platform_id=platform_id,
        error=error,
        backend="iris",
        iris_parts=[],
        source_sha256=sha256,
    )


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------

# How many times one part's upload may end without a clear answer from Iris
# (timed out, connection cut, Iris or a gateway erroring after the request
# arrived) before the job fails instead. Each such attempt may have created a
# session anyway, so retrying without a limit could convert the same pages
# over and over; three keeps a flaky moment survivable and the waste bounded.
_MAX_UNCERTAIN_UPLOADS = 3
# Uploads pause this long after an uncertain attempt (the first, then any
# later one), so a struggling Iris has time to recover before the next
# attempt counts against the part.
_UNCERTAIN_BACKOFF_SECONDS = (120, 600)
# How many times the bridge re-sends a part on its own after Iris lost it
# (session deleted or expired) while the document was still wanted.
_MAX_LOST_RESENDS = 2

RETRY_HINT = "Use “Try again” on the Accessible Documents page."


def pending_parts(job: CanvasJob) -> list[dict[str, Any]]:
    """Parts waiting to be uploaded: no session yet, and not given up on."""
    return [
        p for p in (job.iris_parts or [])
        if not p.get("session_id") and p.get("status") != STATUS_FAILED
    ]


def _where(job: CanvasJob, part: dict[str, Any]) -> str:
    if len(job.iris_parts or []) > 1:
        return f"pages {part.get('first_page')}-{part.get('last_page')} of this document"
    return "this document"


def _part_failure_message(job: CanvasJob, part: dict[str, Any], detail: str) -> str:
    return f"Iris could not convert {_where(job, part)}: {detail}"


def _failures_message(job: CanvasJob, failed: list[dict[str, Any]]) -> str:
    """One readable sentence for every part that failed, ending with what to do."""
    if len(failed) == 1:
        part = failed[0]
        text = _part_failure_message(job, part, str(part.get("error") or "conversion failed"))
    else:
        bits = [
            f"pages {p.get('first_page')}-{p.get('last_page')} "
            f"({str(p.get('error') or 'conversion failed').rstrip('.')})"
            for p in failed
        ]
        text = "Iris could not convert " + "; ".join(bits)
    text = text.rstrip()
    if not text.endswith("."):
        text += "."
    return f"{text} {RETRY_HINT}"


async def upload_pending_parts(
    redis: Redis,
    iris: IrisClient,
    job: CanvasJob,
    pdf_bytes: bytes,
) -> bool:
    """Upload every part of ``job`` that has no Iris session yet.

    Returns True when every part is now uploaded. Stops at the first failure
    and leaves the rest pending for the bridge's next tick. How a failure is
    handled depends on what it says about Iris:

    * busy or throttling us (429/503), unreachable, or refusing our token:
      uploads pause for everyone, nothing is counted against the part;
    * the request may have arrived anyway (timeout, cut connection, an Iris
      or gateway error after it was sent): retried later, at most
      ``_MAX_UNCERTAIN_UPLOADS`` times, because each attempt may have left a
      session behind in Iris;
    * Iris refused the part itself (400/413/422): the job fails with Iris's
      own explanation.

    The job is saved after every upload (a merge, see ``state.put_job``), so
    a crash between parts never forgets a session Iris is already running.
    Each attempt is counted before it is sent, so one cut short by a restart
    counts too. A job deleted meanwhile (its file removed, an erasure) stops
    the uploads.
    """
    total = int(job.source_page_count or 0) or await asyncio.to_thread(pdf_page_count, pdf_bytes)
    for part in pending_parts(job):
        if await upload_backoff_active(redis):
            return False
        attempts = int(part.get("upload_attempts") or 0)
        if attempts >= _MAX_UNCERTAIN_UPLOADS:
            await _give_up_on_part(redis, job, part, str(part.get("error") or "no answer"))
            return False
        data = await asyncio.to_thread(
            part_bytes, pdf_bytes, int(part["first_page"]), int(part["last_page"]), total,
        )
        part["upload_attempts"] = attempts + 1
        if not await put_job(redis, job):
            return False  # deleted: send nothing more
        try:
            created = await iris.create_session(_part_name(job.canvas_file_name), data)
        except IrisApiError as exc:
            await _upload_failed(redis, job, part, exc)
            return False
        part["session_id"] = str(created["session_id"])
        part["status"] = str(created.get("status") or "queued")
        part["error"] = None
        part["upload_attempts"] = 0
        if not await put_job(redis, job):
            logger.warning(
                "Job %s was deleted while uploading; Iris session %s is unused",
                job.reflow_job_id, part["session_id"],
            )
            return False
        await mark_iris_activity(redis, job.reflow_job_id, _activity_ttl())
        logger.info(
            "Iris session %s created for job %s (pages %s-%s of %s)",
            part["session_id"], job.reflow_job_id, part["first_page"],
            part["last_page"], total,
        )
    return not pending_parts(job)


async def _give_up_on_part(
    redis: Redis, job: CanvasJob, part: dict[str, Any], last: str,
) -> None:
    attempts = int(part.get("upload_attempts") or 0)
    part["status"] = STATUS_FAILED
    part["error"] = (
        f"Iris did not confirm receiving these pages after {attempts} attempts (last: {last})"
    )
    job.status = "failed"
    job.error = _failures_message(job, [part])
    job.iris_stalled_at = None
    await put_job(redis, job)


async def _upload_failed(
    redis: Redis, job: CanvasJob, part: dict[str, Any], exc: IrisApiError,
) -> None:
    status = exc.status_code
    attempts = int(part.get("upload_attempts") or 0)
    if status in (401, 403):
        logger.error(
            "Iris refused our credentials (HTTP %s). Check IRIS_API_TOKEN "
            "against Iris's server.api_token.", status,
        )
        await set_upload_backoff(redis, exc.retry_after, exc.message, status_code=status)
        part["upload_attempts"] = max(0, attempts - 1)  # nothing was created
        part["error"] = exc.message
        await put_job(redis, job)
        return
    if exc.may_have_reached:
        part["error"] = exc.message
        logger.warning(
            "Upload of %s for job %s may have reached Iris anyway (%s); attempt %d of %d. "
            "A repeat can leave an unused session in Iris until its retention deletes it.",
            _where(job, part), job.reflow_job_id, exc, attempts, _MAX_UNCERTAIN_UPLOADS,
        )
        if attempts >= _MAX_UNCERTAIN_UPLOADS:
            await _give_up_on_part(redis, job, part, exc.message)
            return
        wait = _UNCERTAIN_BACKOFF_SECONDS[min(attempts, len(_UNCERTAIN_BACKOFF_SECONDS)) - 1]
        await set_upload_backoff(redis, wait, exc.message, status_code=status)
        await put_job(redis, job)
        return
    if status is None or status in (429, 502, 503, 504):
        # Throttled, busy, or never reached Iris: nothing was created.
        await set_upload_backoff(redis, exc.retry_after, exc.message, status_code=status)
        part["upload_attempts"] = max(0, attempts - 1)
        part["error"] = exc.message
        await put_job(redis, job)
        return
    # Iris refused the content itself (a page too heavy for the model, an
    # unreadable PDF): retrying the same bytes cannot help.
    part["status"] = STATUS_FAILED
    part["error"] = exc.message
    job.status = "failed"
    job.error = _failures_message(job, [part])
    job.iris_stalled_at = None
    await put_job(redis, job)
    logger.warning("Iris rejected %s: %s", job.reflow_job_id, exc)


def _activity_ttl() -> int:
    return int(getattr(settings, "canvas_stale_job_max_age_seconds", 3600) or 3600)


# ---------------------------------------------------------------------------
# Polling and completing
# ---------------------------------------------------------------------------

@dataclass
class PartsState:
    finished: bool = False
    failed: bool = False
    in_flight: bool = False
    # Parts Iris lost that were reset to be uploaded again.
    resent: bool = False
    error: str | None = None
    phases: list[str] = field(default_factory=list)


_LOST_DETAIL = "Iris no longer has these pages (its copy was deleted or expired)"


def _is_lost(exc: IrisApiError) -> bool:
    """Iris itself saying the session is gone. A 404 from anything else (a
    wrong base URL, a proxy route) is an error to report, not a reason to
    forget a session that may still be converting, and pay for it again."""
    return (exc.status_code == 404 and exc.code == "session_not_found") or (
        exc.status_code == 410 and exc.code == "session_expired"
    )


def _part_lost(job: CanvasJob, part: dict[str, Any]) -> bool:
    """Iris lost a part. Re-send it while the document is wanted; otherwise
    fail it. Returns True when the part was reset for upload."""
    resends = int(part.get("lost_resends") or 0)
    if job.status == "processing" and resends < _MAX_LOST_RESENDS:
        logger.warning(
            "Iris lost session %s (%s of job %s); sending those pages again",
            part.get("session_id"), _where(job, part), job.reflow_job_id,
        )
        part.update(session_id=None, status="pending", error=None, lost_resends=resends + 1)
        part["upload_attempts"] = 0
        return True
    part["status"] = STATUS_FAILED
    part["error"] = _LOST_DETAIL
    return False


async def poll_parts(redis: Redis, iris: IrisClient, job: CanvasJob) -> PartsState:
    """Read every uploaded part's session and summarize the job's state.

    Every part is read, so a job with several failed or lost parts records
    all of them at once and one "Try again" re-sends them all. A session
    Iris deleted or expired (``expired_at`` on its status, or 404/410) is
    lost: re-sent automatically while the document is wanted, failed
    otherwise.
    """
    state = PartsState()
    parts = job.iris_parts or []
    if not parts or pending_parts(job):
        state.in_flight = bool(parts)
        return state
    failed: list[dict[str, Any]] = []
    all_finished = True
    before = json.dumps(parts, sort_keys=True)
    for part in parts:
        sid = str(part.get("session_id") or "")
        if not sid:  # failed before it was ever uploaded
            failed.append(part)
            all_finished = False
            continue
        try:
            session: dict[str, Any] | None = await iris.get_session(sid)
        except IrisApiError as exc:
            if not _is_lost(exc):
                raise
            session = None
        if session is None or session.get("expired_at"):
            all_finished = False
            if _part_lost(job, part):
                state.resent = True
            else:
                failed.append(part)
            continue
        status = str(session.get("status") or "")
        part["status"] = status
        if status == STATUS_FAILED:
            part["error"] = str(session.get("error") or "conversion failed")
            failed.append(part)
            all_finished = False
        elif status in IN_FLIGHT_STATUSES:
            state.in_flight = True
            state.phases.append(f"{status}:{session.get('phase') or ''}")
            all_finished = False
        elif status not in FINISHED_STATUSES:
            all_finished = False
    if failed:
        state.failed = True
        state.error = _failures_message(job, failed)
    if json.dumps(parts, sort_keys=True) != before:
        await put_job(redis, job)
    if state.in_flight or state.resent:
        await mark_iris_activity(redis, job.reflow_job_id, _activity_ttl())
    state.finished = all_finished and not state.failed
    return state


async def complete_job(redis: Redis, iris: IrisClient, job: CanvasJob) -> bool:
    """Fetch, stitch and store the finished document. Idempotent.

    Returns False when a part's output turned out to be gone (Iris deleted
    it between the status check and now); that part is handled like any
    lost part and the job completes on a later tick.
    """
    parts = sorted(job.iris_parts or [], key=lambda p: int(p.get("first_page") or 0))
    outputs: list[tuple[IrisPartInfo, str]] = []
    for part in parts:
        sid = str(part["session_id"])
        try:
            html = await iris.get_output(sid)
        except IrisApiError as exc:
            if not _is_lost(exc):
                raise
            if not _part_lost(job, part):
                job.status = "failed"
                job.error = _failures_message(job, [part])
            await put_job(redis, job)
            return False
        outputs.append((
            IrisPartInfo(
                index=int(part.get("index") or 0),
                first_page=int(part["first_page"]),
                last_page=int(part["last_page"]),
                session_id=sid,
            ),
            html,
        ))
    # Parsing and sanitizing a long document is CPU work; keep it off the
    # event loop the request handlers share.
    doc = await asyncio.to_thread(stitch, outputs)
    body = await asyncio.to_thread(sanitize_html, doc.body_html)
    await put_iris_body(redis, job.reflow_job_id, body)
    job.iris_figures = [asdict(f) for f in doc.figures]
    job.iris_report = doc.report.to_dict()
    job.content_lang = doc.lang
    job.signals = await asyncio.to_thread(
        lambda: derive_signals_from_html(
            body,
            lang=doc.lang,
            has_text_layer=job.source_has_text_layer,
            report=job.iris_report,
        )
    )
    job.completed_at = time.time()
    job.iris_usage = await _usage(iris, [str(p["session_id"]) for p in parts])
    await _true_up_cost(redis, job)
    await put_job(redis, job)
    logger.info(
        "Iris job %s complete: %d part(s), %d figure(s), failed pages %s, "
        "uncorrected pages %s, %d open issue(s)",
        job.reflow_job_id, len(parts), len(doc.figures), doc.report.failed_pages,
        doc.report.uncorrected_pages, len(doc.report.unresolved),
    )
    return True


_TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")


async def _usage(iris: IrisClient, session_ids: list[str]) -> dict[str, Any] | None:
    """Sum Iris's token counts over the job's sessions. Best-effort."""
    totals: dict[str, Any] = {k: 0 for k in _TOKEN_FIELDS}
    totals["model_calls"] = 0
    models: set[str] = set()
    got_any = False
    for sid in session_ids:
        try:
            diag = await iris.get_diagnostics(sid)
        except IrisApiError as exc:
            logger.info("Iris diagnostics unavailable for %s: %s", sid, exc)
            continue
        got_any = True
        tokens = diag.get("tokens") or {}
        for k in _TOKEN_FIELDS:
            totals[k] += int(tokens.get(k) or 0)
        totals["model_calls"] += int((diag.get("model_calls") or {}).get("count") or 0)
        for agent in (diag.get("by_agent") or {}).values():
            models.update(str(m) for m in (agent.get("models") or []))
    if not got_any:
        return None
    totals["models"] = sorted(models)
    totals["sessions"] = len(session_ids)
    cost = _cost_cents(totals)
    if cost is not None:
        totals["cost_cents"] = cost
    return totals


def _cost_cents(totals: dict[str, Any]) -> int | None:
    prices = {
        "input": float(getattr(settings, "iris_price_input_per_mtok_usd", 0) or 0),
        "output": float(getattr(settings, "iris_price_output_per_mtok_usd", 0) or 0),
        "cache_read": float(getattr(settings, "iris_price_cache_read_per_mtok_usd", 0) or 0),
        "cache_write": float(getattr(settings, "iris_price_cache_write_per_mtok_usd", 0) or 0),
    }
    if not any(prices.values()):
        return None
    usd = sum(int(totals.get(k) or 0) / 1_000_000 * price for k, price in prices.items())
    return int(round(usd * 100))


async def _true_up_cost(redis: Redis, job: CanvasJob) -> None:
    actual = (job.iris_usage or {}).get("cost_cents")
    if actual is None or job.reserved_cost_cents is None:
        return
    from .spend_cap import reconcile_actual_cost

    try:
        await reconcile_actual_cost(
            redis, job.canvas_course_id, int(actual), estimate_cents=int(job.reserved_cost_cents),
        )
    except Exception:  # noqa: BLE001 - accounting must not block the document
        logger.exception("Spend true-up failed for job %s", job.reflow_job_id)


async def close_sessions(iris: IrisClient, job: CanvasJob) -> int:
    """Close every session of an approved job. Returns how many closed."""
    closed = 0
    for part in job.iris_parts or []:
        sid = str(part.get("session_id") or "")
        if not sid:
            continue
        try:
            await iris.close_session(sid)
            closed += 1
        except IrisApiError as exc:
            # 409: already closed or re-running; 404/410: gone. Nothing to do.
            if exc.status_code not in (404, 409, 410):
                logger.warning("Could not close Iris session %s: %s", sid, exc)
    return closed


# ---------------------------------------------------------------------------
# Asking again
# ---------------------------------------------------------------------------

@dataclass
class Requeued:
    outcome: str  # "resend" | "restart" | "rebuild" | "busy" | "over_budget"
    message: str


async def requeue_job(redis: Redis, job: CanvasJob) -> Requeued:
    """Convert a document again, as cheaply as its state allows.

    Shared by "Try again" on the review screen, the overlay's convert route
    and ``connector.tools.retry_failed_jobs``:

    * an Iris job that failed part-way re-sends only the parts that failed
      or were lost (finished parts are kept), after reserving their cost;
    * a job with nothing worth keeping (the file could not be prepared, or a
      Reflow-era conversion that never produced anything) starts over: the
      watcher converts the file with Iris on its next tick;
    * anything else is rebuilt from the content already stored.
    """
    from .spend_cap import reconcile_actual_cost, reserve_submission
    from .state import clear_processed, get_legacy_markdown, purge_job

    if job.status == "failed" and not job.completed_at:
        parts = job.iris_parts or []
        if job.is_iris and parts:
            resend = [
                p for p in parts
                if p.get("status") == STATUS_FAILED or not p.get("session_id")
            ]
            pages = sum(int(p["last_page"]) - int(p["first_page"]) + 1 for p in resend)
            estimate = estimate_cost_cents(pages) if pages else 0
            if estimate:
                allowed, _info = await reserve_submission(
                    redis, job.canvas_course_id, estimate_cents=estimate,
                )
                if not allowed:
                    return Requeued(
                        "over_budget",
                        "This course has reached its monthly conversion budget, so the "
                        "document was not sent again. Ask your Canvas administrator.",
                    )
                job.reserved_cost_cents = int(job.reserved_cost_cents or 0) + estimate
            for p in resend:
                p.update(session_id=None, status="pending", error=None,
                         upload_attempts=0, lost_resends=0)
            job.status = "processing"
            job.error = None
            job.iris_stalled_at = None
            # Only if it is still failed: a second click (or the operator tool
            # at the same moment) must not reserve, or send, twice.
            if not await put_job(redis, job, only_if={"status": "failed"}):
                if estimate:
                    await reconcile_actual_cost(
                        redis, job.canvas_course_id, 0, estimate_cents=estimate,
                    )
                return Requeued("busy", "Already sent again. The page will appear shortly.")
            return Requeued("resend", "Sending the pages that did not convert to Iris again.")
        if job.is_iris or await get_legacy_markdown(redis, job.reflow_job_id) is None:
            await clear_processed(redis, job.canvas_course_id, job.canvas_file_id)
            await purge_job(redis, job)
            return Requeued(
                "restart", "Conversion queued. The accessible page will appear in a few minutes.",
            )
    if job.status != "processing":
        job.status = "processing"
        job.error = None
        job.iris_stalled_at = None
        await put_job(redis, job)
        return Requeued("rebuild", "Building the accessible page. It will appear in under a minute.")
    return Requeued("busy", "Already converting. The page will appear shortly.")
