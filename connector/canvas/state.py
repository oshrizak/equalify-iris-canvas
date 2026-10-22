"""Redis-backed mapping between Canvas files and conversion jobs.

Keys:
  eq-pdf:canvas:job:{job_id}                   String  job ↔ canvas mapping (JSON)
  eq-pdf:canvas:course:{course_id}:pending     Set     awaiting-review job ids
  eq-pdf:canvas:course:{course_id}:processed   Set     canvas file ids already
                                                       submitted (idempotency)
  eq-pdf:canvas:iris-body:{job_id}             String  the converted document
                                                       (Iris jobs), stitched
  eq-pdf:canvas:legacy-markdown:{job_id}       String  snapshot of a Reflow
                                                       job's markdown

The job id is the record's primary key. The field is still called
``reflow_job_id`` because every stored record, URL and Redis key already
uses it; for an Iris job it is a connector-generated UUID, and the Iris
session ids live in ``iris_parts``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from .tenant import tk

logger = logging.getLogger(__name__)

JOB_KEY = tk("canvas:job:{job_id}")
PENDING_KEY = tk("canvas:course:{course_id}:pending")
PROCESSED_KEY = tk("canvas:course:{course_id}:processed")
# Stable map of a Canvas file -> the wiki page slug the bridge created for it.
# Survives across re-conversions (each re-conversion is a new reflow_job_id),
# so the bridge can UPDATE the same page instead of creating a duplicate.
FILE_PAGE_KEY = tk("canvas:course:{course_id}:file-page:{file_id}")

JobStatus = Literal[
    "processing",
    # Reflow paused the conversion for a PII/privacy decision. Distinct from
    # "processing" so the overlay can show an Approve/Deny prompt and the
    # timeout watchdog doesn't mislabel it "stuck in processing".
    "awaiting_approval",
    "awaiting_review",
    "published",
    "rejected",
    "failed",
    # Conversion succeeded but the Canvas Page write was rejected (almost
    # always the course-owner OAuth token missing the Pages write scope).
    # Distinct from "failed" (which means the conversion itself failed) so the
    # overlay can tell faculty the real reason, and from "awaiting_review"
    # (which falsely implied a page existed). The bridge keeps polling these
    # and rebuilds the page automatically once the scope is granted.
    "page_failed",
]


@dataclass
class CanvasJob:
    reflow_job_id: str
    canvas_file_id: str
    canvas_file_name: str
    canvas_course_id: str
    canvas_user_id: str
    status: JobStatus
    created_at: float
    canvas_page_id: str | None = None
    canvas_page_url: str | None = None
    error: str | None = None
    # LTI platform that owns this job. Set by the multi-tenant watcher
    # (Phase 5+); legacy single-tenant submissions leave it None and the
    # bridge worker falls back to the env-token CanvasClient for them.
    platform_id: str | None = None
    # Per-document conversion-quality signals derived from the pipeline
    # output (heading structure, image+alt counts, table semantics, etc).
    # ``None`` for jobs that haven't reached completion yet. Populated
    # by the bridge worker when Reflow returns the converted markdown.
    # NOTE: these are conversion-quality heuristics, not WCAG conformance
    # proofs. The publication gate (Phase 7) runs separate WCAG checks.
    signals: dict | None = None
    # Map of markdown figure reference (e.g. "figures/figure-1.png") to the
    # Canvas course-file URL the bridge uploaded it to. Populated by the
    # bridge worker after it uploads figures into the course. Used to embed
    # Canvas-hosted images in both the Canvas Page and the overlay's
    # accessible-HTML views, so pages stay self-contained (no dependency on
    # the tunnel or the Reflow backend staying up).
    figure_canvas_urls: dict | None = None
    # Real PDF/UA-1 audit result for the SOURCE PDF, produced by the
    # watcher via canvas.verapdf_audit at submission time. Replaces the
    # heuristic source_score that came out of canvas.signals. ``None``
    # when the audit couldn't run (verapdf binary missing, audit
    # timeout, non-PDF source format like docx).
    #
    # verapdf_score: 0..100, computed as passed_rules / applicable_rules.
    #   The dial uses this as the "before" number.
    # verapdf_is_compliant: True only when zero rules failed (PDF/UA
    #   conformance is binary; the percentage is a relative measure).
    # verapdf_violations: list of {rule_id, clause, description,
    #   occurrence_count}. The alt-formats modal renders these so faculty
    #   see WHY the score is what it is, not just a number.
    # verapdf_audited_at: epoch seconds the audit finished. Lets the UI
    #   tell faculty when the audit was last run.
    verapdf_score: int | None = None
    verapdf_is_compliant: bool | None = None
    verapdf_violations: list | None = None
    verapdf_audited_at: float | None = None
    # Which service converted this document: "iris", or "reflow" for every
    # record written before the switch (they have no such field, so they
    # load with this default).
    backend: str = "reflow"
    # Iris only. One entry per part of the source PDF, in page order:
    #   {"index", "first_page", "last_page", "session_id", "status", "error"}
    # ``session_id`` is None until that part has been uploaded; a part that
    # could not be uploaded yet (Iris busy, rate-limited, down) is retried
    # by the bridge on its next tick.
    iris_parts: list | None = None
    source_page_count: int | None = None
    source_sha256: str | None = None
    source_has_text_layer: bool | None = None
    # Iris only, set when the conversion completes: the figure manifest
    # (see iris_output.IrisFigure), what Iris reported about the document
    # (failed / uncorrected pages, open issues), and its token usage.
    iris_figures: list | None = None
    iris_report: dict | None = None
    iris_usage: dict | None = None
    # BCP 47 language of the converted document, as Iris reported it.
    content_lang: str | None = None
    # Figure refs whose pixels could not be matched in the source PDF; they
    # render as their descriptions and are not looked for again.
    iris_figures_unmatched: list | None = None
    # Set when the stale-job sweeper failed this job because Iris showed no
    # sign of working on it (unreachable, most likely). Unlike a failure Iris
    # reported, that one can heal: the bridge keeps checking the sessions and
    # resumes the job if Iris comes back with them.
    iris_stalled_at: float | None = None
    # What the spend cap charged at submission, so completion can true it up.
    reserved_cost_cents: int | None = None
    completed_at: float | None = None

    @property
    def is_iris(self) -> bool:
        return self.backend == "iris"


_JOB_FIELDS = frozenset(f.name for f in dataclasses.fields(CanvasJob))


def new_job_id() -> str:
    """A fresh job id. Iris jobs can span several Iris sessions, so the id is
    the connector's own rather than any one session's."""
    return str(uuid.uuid4())


def job_from_dict(data: dict[str, Any]) -> CanvasJob:
    """Build a CanvasJob, ignoring fields this version does not know.

    Lets a record written by a newer connector be read by this one instead
    of failing the whole request on an unexpected keyword. The job remembers
    what it looked like when read, so :func:`put_job` can write back only
    what its holder changed (see there).
    """
    job = CanvasJob(**{k: v for k, v in data.items() if k in _JOB_FIELDS})
    _remember(job, asdict(job))
    return job


# ---------------------------------------------------------------------------
# Writing a job without clobbering anyone else's changes
#
# Several workers hold copies of the same job at once: the watcher (which
# uploads parts and records the source audit), the bridge (which uploads the
# parts Iris could not take, polls them and builds the page), the stale-job
# sweeper, and the API. Writing a whole stale copy back would undo whatever
# the others saved in between, and for Iris that can mean forgetting a
# session id and uploading (and paying for) that part again. So a job read
# from Redis remembers its state at read time, and put_job applies only the
# fields its holder changed onto the record as it is now, atomically.
# ---------------------------------------------------------------------------

_LOADED_ATTR = "_state_when_read"
_MISSING = object()


def _plain(value: Any) -> Any:
    """A deep copy in exactly the shape JSON gives back (lists, not tuples)."""
    return json.loads(json.dumps(value))


def _remember(job: CanvasJob, state: dict[str, Any]) -> None:
    object.__setattr__(job, _LOADED_ATTR, _plain(state))


def _parts_shape(parts: Any) -> list[tuple[Any, Any, Any]] | None:
    if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
        return None
    return [(p.get("index"), p.get("first_page"), p.get("last_page")) for p in parts]


def _merge_parts(stored: Any, loaded: Any, mine: Any) -> Any:
    """Per part, per key: this writer's changes on top of the stored parts."""
    mine_shape = _parts_shape(mine)
    if mine_shape is None or mine_shape != _parts_shape(loaded):
        return mine  # this writer re-planned the parts: its list is the plan
    if _parts_shape(stored) != mine_shape:
        return stored  # re-planned by someone else since; this writer's edits are moot
    merged = []
    for s_part, l_part, m_part in zip(stored, loaded, mine, strict=True):
        part = dict(s_part)
        for key, value in m_part.items():
            if l_part.get(key, _MISSING) != value:
                part[key] = value
        merged.append(part)
    return merged


def _merge_job(stored: dict[str, Any], loaded: dict[str, Any], mine: dict[str, Any]) -> dict[str, Any]:
    out = dict(stored)
    for name, value in mine.items():
        before = loaded.get(name, _MISSING)
        if before is not _MISSING and value == before:
            continue  # not changed by this writer: keep what is stored now
        if name == "iris_parts" and before is not _MISSING:
            out[name] = _merge_parts(stored.get(name), before, value)
        else:
            out[name] = value
    return out


def _adopt(job: CanvasJob, payload: dict[str, Any]) -> None:
    """Bring the in-memory job up to what was written.

    Part dicts are updated in place, so code iterating over a job's parts
    while saving after each one keeps working on the live objects.
    """
    for name in _JOB_FIELDS:
        if name not in payload:
            continue
        value = _plain(payload[name])
        current = getattr(job, name)
        if (
            name == "iris_parts"
            and isinstance(current, list)
            and isinstance(value, list)
            and len(current) == len(value)
            and all(isinstance(c, dict) for c in current)
        ):
            for cur, new in zip(current, value, strict=True):
                cur.clear()
                cur.update(new)
            continue
        setattr(job, name, value)


class _ConditionFailedError(Exception):
    """The stored record no longer matches what the writer required."""


async def _write_merged(
    redis: Redis,
    key: str,
    loaded: dict[str, Any],
    mine: dict[str, Any],
    only_if: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Merge-and-write under WATCH; ``None`` when the record is gone.

    Raises :class:`_ConditionFailedError` when ``only_if`` names a field whose
    stored value is no longer the one given.
    """
    from redis.exceptions import WatchError

    for _ in range(25):
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                if raw is None:
                    return None
                stored = json.loads(raw)
                for name, expected in (only_if or {}).items():
                    if stored.get(name) != expected:
                        raise _ConditionFailedError(name)
                payload = _merge_job(stored, loaded, mine)
                pipe.multi()
                pipe.set(key, json.dumps(payload))
                await pipe.execute()
                return payload
            except WatchError:
                continue
    raise RuntimeError(f"job record {key} kept changing while being written")


async def already_processed(
    redis: Redis, course_id: str, canvas_file_id: str
) -> bool:
    return bool(
        await redis.sismember(
            PROCESSED_KEY.format(course_id=course_id), canvas_file_id
        )
    )


async def mark_processed(redis: Redis, course_id: str, canvas_file_id: str) -> None:
    await redis.sadd(PROCESSED_KEY.format(course_id=course_id), canvas_file_id)


async def clear_processed(redis: Redis, course_id: str, canvas_file_id: str) -> None:
    """Drop a file's 'already processed' marker so the watcher re-converts it
    on its next tick. Used when a document is asked for again ("Try again",
    ``iris_jobs.requeue_job``) and when its file changed mid-conversion.
    Idempotent — a no-op if the marker isn't set."""
    await redis.srem(PROCESSED_KEY.format(course_id=course_id), canvas_file_id)


# ----------------------------------------------------------------------
# Per-course opt-in
#
# Reflow is OFF for a course until a human turns it on. Until then the
# watcher only looks at the drop folder, so a faculty member can try one
# file without authorising a sweep of everything they've ever uploaded.
# ----------------------------------------------------------------------

OPTIN_KEY = tk("canvas:course:{course_id}:optin")


async def is_course_enabled(redis: Redis, course_id: str) -> bool:
    """True when a human has explicitly enabled full-course scanning."""
    return bool(await redis.exists(OPTIN_KEY.format(course_id=course_id)))


async def set_course_enabled(
    redis: Redis, course_id: str, *, enabled: bool, actor: str = "",
) -> None:
    """Turn full-course scanning on or off.

    Stores who flipped it and when — an ISO asking "who authorised
    processing for this course" needs an answer that isn't "the software".
    """
    key = OPTIN_KEY.format(course_id=course_id)
    if enabled:
        await redis.set(key, json.dumps({"actor": actor, "at": time.time()}))
    else:
        await redis.delete(key)


async def get_course_optin(redis: Redis, course_id: str) -> dict[str, Any] | None:
    """Return ``{"actor", "at"}`` for an enabled course, else ``None``."""
    raw = await redis.get(OPTIN_KEY.format(course_id=course_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------
# Content-hash index
#
# Faculty drop a *copy* of a PDF into the Reflow folder, so the converted
# file has a different Canvas id from the one students actually click in
# Modules or Pages. We hash content to recognise "same document, different
# file id" and attach the accessible page to every copy.
# ----------------------------------------------------------------------

CONTENT_HASH_KEY = tk("canvas:course:{course_id}:hash:{sha256}")


async def get_job_for_hash(
    redis: Redis, course_id: str, sha256: str,
) -> str | None:
    """Return the Reflow job id already produced for this exact content."""
    raw = await redis.get(
        CONTENT_HASH_KEY.format(course_id=course_id, sha256=sha256)
    )
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def put_job_for_hash(
    redis: Redis, course_id: str, sha256: str, reflow_job_id: str,
) -> None:
    """Remember which job converted this content, so a re-drop of the same
    file is recognised instead of reconverted (which would burn API spend)."""
    await redis.set(
        CONTENT_HASH_KEY.format(course_id=course_id, sha256=sha256),
        reflow_job_id,
    )


# A file id that shares content with a converted file, pointing at that
# file's job. The overlay consults this so the accessible-version badge
# appears on the copy students actually click — typically the one linked
# from a Module — and not only on the copy sitting in the drop folder.
FILE_ALIAS_KEY = tk("canvas:course:{course_id}:file-alias:{file_id}")


async def get_file_alias(
    redis: Redis, course_id: str, file_id: str,
) -> str | None:
    """Return the Reflow job id this file id is aliased to, if any."""
    raw = await redis.get(
        FILE_ALIAS_KEY.format(course_id=course_id, file_id=file_id)
    )
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def put_file_alias(
    redis: Redis, course_id: str, file_id: str, reflow_job_id: str,
) -> None:
    """Point a file id at an existing job without converting it again."""
    await redis.set(
        FILE_ALIAS_KEY.format(course_id=course_id, file_id=file_id),
        reflow_job_id,
    )


async def put_job(
    redis: Redis, job: CanvasJob, *, only_if: dict[str, Any] | None = None,
) -> bool:
    """Save a job. Returns False when nothing was written.

    A job built in code (a new one) is written as is. A job read from Redis
    writes back only the fields its holder changed since reading it, merged
    onto the record as stored now (see the section above), and is then
    brought up to date with what other writers saved meanwhile. A job that
    was deleted after it was read (the file was removed, retention, erasure)
    is not recreated.

    ``only_if`` makes the write conditional on stored values, for writers
    acting on a judgment that is only valid while those values hold (the
    stale-job sweeper failing a job that is still ``processing``).
    """
    key = JOB_KEY.format(job_id=job.reflow_job_id)
    mine = _plain(asdict(job))
    loaded = getattr(job, _LOADED_ATTR, None)
    if loaded is None:
        if only_if:
            raise ValueError("only_if needs a job that was read from Redis")
        payload: dict[str, Any] | None = mine
        await redis.set(key, json.dumps(mine))
    else:
        try:
            payload = await _write_merged(redis, key, loaded, mine, only_if)
        except _ConditionFailedError:
            return False
        if payload is None:
            logger.info(
                "Job %s was deleted while being updated; not recreating it",
                job.reflow_job_id,
            )
            return False
        _adopt(job, payload)
    _remember(job, payload)
    # Drop the panorama score cache (same key the overlay's /score endpoint
    # uses) so a status change shows up promptly. Its TTL is 24h, so without
    # this the overlay can keep showing a stale state — e.g. a PII gate for an
    # already-completed job — long after the bridge moved the job on. put_job
    # is only called on actual changes, so this clears exactly when we want a
    # recompute. Best-effort: never let a cache delete break a status write.
    try:
        await redis.delete(tk("canvas:score:{job_id}").format(job_id=job.reflow_job_id))
    except Exception:  # noqa: BLE001
        pass
    # Both statuses warrant faculty action in the LTI tool's queue:
    # ``awaiting_review`` is the accessibility-approval gate; ``awaiting_approval``
    # is the upstream PII gate. The tool index renders them with distinct
    # badges so faculty know which decision they're being asked to make.
    if job.status in ("awaiting_review", "awaiting_approval"):
        await redis.sadd(
            PENDING_KEY.format(course_id=job.canvas_course_id), job.reflow_job_id
        )
    else:
        await redis.srem(
            PENDING_KEY.format(course_id=job.canvas_course_id), job.reflow_job_id
        )
    return True


async def get_job(redis: Redis, reflow_job_id: str) -> CanvasJob | None:
    raw: Any = await redis.get(JOB_KEY.format(job_id=reflow_job_id))
    if raw is None:
        return None
    return job_from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Converted content the connector keeps itself
#
# Reflow kept its markdown in S3 and the connector fetched it on every view.
# Iris keeps sessions on one box with an optional retention window, and a
# document cut into parts has no single session to fetch from anyway, so
# the stitched body lives here, next to the job it belongs to.
# ---------------------------------------------------------------------------

IRIS_BODY_KEY = tk("canvas:iris-body:{job_id}")
LEGACY_MARKDOWN_KEY = tk("canvas:legacy-markdown:{job_id}")
# Set (with a TTL) whenever the bridge sees a part queued or running in
# Iris, so the stale-job sweeper can tell "Iris is busy" from "nothing is
# happening". Long documents legitimately take more than an hour.
IRIS_ACTIVITY_KEY = tk("canvas:iris-activity:{job_id}")
# Figure lifting for Iris jobs: a short "wait before trying again" marker
# after a failed attempt, and how many attempts were made, so a document
# whose figures cannot be uploaded is not re-downloaded (and, for Office
# files, re-converted) on every tick.
IRIS_FIGURES_RETRY_KEY = tk("canvas:iris-figures-retry:{job_id}")
IRIS_FIGURES_ATTEMPTS_KEY = tk("canvas:iris-figures-attempts:{job_id}")


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


async def get_iris_body(redis: Redis, job_id: str) -> str | None:
    return _text(await redis.get(IRIS_BODY_KEY.format(job_id=job_id)))


async def put_iris_body(redis: Redis, job_id: str, body_html: str) -> None:
    await redis.set(IRIS_BODY_KEY.format(job_id=job_id), body_html)


async def get_legacy_markdown(redis: Redis, job_id: str) -> str | None:
    return _text(await redis.get(LEGACY_MARKDOWN_KEY.format(job_id=job_id)))


async def put_legacy_markdown(redis: Redis, job_id: str, markdown: str) -> None:
    await redis.set(LEGACY_MARKDOWN_KEY.format(job_id=job_id), markdown)


async def mark_iris_activity(redis: Redis, job_id: str, ttl_seconds: int) -> None:
    await redis.set(
        IRIS_ACTIVITY_KEY.format(job_id=job_id), str(time.time()), ex=max(60, int(ttl_seconds)),
    )


async def has_recent_iris_activity(redis: Redis, job_id: str) -> bool:
    return bool(await redis.exists(IRIS_ACTIVITY_KEY.format(job_id=job_id)))


def content_keys(job_id: str) -> list[str]:
    """Every per-job key holding converted content or derived state.

    One list so the purge paths (deleted file, retention, hard delete,
    user erasure) cannot drift apart as content keys are added.
    """
    return [
        IRIS_BODY_KEY.format(job_id=job_id),
        LEGACY_MARKDOWN_KEY.format(job_id=job_id),
        IRIS_ACTIVITY_KEY.format(job_id=job_id),
        IRIS_FIGURES_RETRY_KEY.format(job_id=job_id),
        IRIS_FIGURES_ATTEMPTS_KEY.format(job_id=job_id),
        EDITED_HTML_KEY.format(job_id=job_id),
        tk("canvas:score:{job_id}").format(job_id=job_id),
    ]


async def get_file_page(redis: Redis, course_id: str, file_id: str) -> str | None:
    """Return the wiki page slug previously created for this Canvas file."""
    raw: Any = await redis.get(
        FILE_PAGE_KEY.format(course_id=course_id, file_id=file_id)
    )
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


async def put_file_page(
    redis: Redis, course_id: str, file_id: str, page_ref: str
) -> None:
    """Remember the wiki page slug for this Canvas file so re-conversions
    update the same page instead of creating duplicates."""
    if not page_ref:
        return
    await redis.set(
        FILE_PAGE_KEY.format(course_id=course_id, file_id=file_id), page_ref
    )


async def list_pending(redis: Redis, course_id: str) -> list[CanvasJob]:
    job_ids: set[Any] = await redis.smembers(PENDING_KEY.format(course_id=course_id))
    jobs: list[CanvasJob] = []
    for jid in job_ids:
        decoded = jid.decode() if isinstance(jid, bytes) else str(jid)
        job = await get_job(redis, decoded)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.created_at)
    return jobs


async def purge_job(redis: Redis, job: CanvasJob) -> None:
    """Forget a job and every trace that would resurrect or display it.

    Used when the Canvas file behind a job is gone. Faculty who delete a
    PDF expect it to disappear, so the record goes with it rather than
    lingering as a row nobody can act on — and, more practically, the
    bridge would otherwise retry a document that no longer exists until
    someone noticed.

    Four keys have to go together. Leaving the processed marker behind
    would stop a re-upload of the same file from ever being picked up;
    leaving the page mapping behind would aim a future conversion at a
    page belonging to a deleted document.
    """
    course = job.canvas_course_id
    await redis.delete(JOB_KEY.format(job_id=job.reflow_job_id))
    await redis.srem(PENDING_KEY.format(course_id=course), job.reflow_job_id)
    for key in content_keys(job.reflow_job_id):
        await redis.delete(key)
    # A re-upload of the same bytes must convert again rather than alias
    # onto the record that was just removed.
    await forget_hash_pointer(redis, job)
    if job.canvas_file_id:
        await redis.srem(PROCESSED_KEY.format(course_id=course), job.canvas_file_id)
        await redis.delete(
            FILE_PAGE_KEY.format(course_id=course, file_id=job.canvas_file_id)
        )
        await redis.delete(
            FILE_ALIAS_KEY.format(course_id=course, file_id=job.canvas_file_id)
        )


async def forget_hash_pointer(redis: Redis, job: CanvasJob) -> None:
    """Drop the content-hash pointer to ``job`` (if it still points there),
    so the same bytes uploaded again are converted rather than aliased onto
    a job that no longer exists."""
    sha = getattr(job, "source_sha256", None)
    if not sha or not job.canvas_course_id:
        return
    hash_key = CONTENT_HASH_KEY.format(course_id=job.canvas_course_id, sha256=sha)
    if _text(await redis.get(hash_key)) == job.reflow_job_id:
        await redis.delete(hash_key)


async def forget_job_pointers(redis: Redis, job: CanvasJob) -> None:
    """Drop every pointer that leads to ``job``: its content hash and the
    file aliases of byte-identical copies. For targeted deletes (erasure,
    hard delete); it scans the course's aliases."""
    await forget_hash_pointer(redis, job)
    if not job.canvas_course_id:
        return
    pattern = FILE_ALIAS_KEY.format(course_id=job.canvas_course_id, file_id="*")
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=200)
        for raw in keys:
            if _text(await redis.get(raw)) == job.reflow_job_id:
                await redis.delete(raw)
        if cursor == 0:
            break


async def list_course_jobs(redis: Redis, course_id: str) -> list[CanvasJob]:
    """Every bridge record belonging to one course, oldest first.

    ``list_pending`` only sees the pending set, so a job that has moved on
    — published, failed, still converting — is invisible to it. The review
    queue used that as its only source and therefore could not answer the
    question faculty actually ask, which is "where is everything?". This
    walks the job keyspace instead and lets the caller decide what to show.

    Scanning is fine at course scale: one course holds tens of records, not
    thousands, and the review page is opened by hand rather than polled.
    """
    out: list[CanvasJob] = []
    cursor = 0
    pattern = JOB_KEY.format(job_id="*")
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=200)
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            raw: Any = await redis.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if str(data.get("canvas_course_id") or "") != str(course_id):
                continue
            try:
                out.append(job_from_dict(data))
            except TypeError:
                # Record written by an older schema. Skip rather than break
                # the whole listing for one stale key.
                continue
        if cursor == 0:
            break
    out.sort(key=lambda j: j.created_at)
    return out


EDITED_HTML_KEY = tk("canvas:edited:{job_id}")


async def get_edited_html(redis: Redis, job_id: str) -> str | None:
    """Return the faculty-edited HTML for a job if one exists."""
    raw: Any = await redis.get(EDITED_HTML_KEY.format(job_id=job_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


async def put_edited_html(redis: Redis, job_id: str, html: str) -> None:
    """Persist a faculty-edited HTML body. Source-of-truth for downstream formats."""
    await redis.set(EDITED_HTML_KEY.format(job_id=job_id), html)


async def clear_edited_html(redis: Redis, job_id: str) -> None:
    """Revert to the auto-generated HTML by deleting any edit."""
    await redis.delete(EDITED_HTML_KEY.format(job_id=job_id))


# ---------------------------------------------------------------------------
# Faculty consent / disclaimer acknowledgment
# ---------------------------------------------------------------------------
# Bump this when the disclaimer language changes — users must re-acknowledge
# any version they have not already agreed to.
#
# 2.0: conversion moved from Equalify Reflow to Equalify Iris. Iris runs no
# PII scan, so the 1.0 statements that PII is detected and redacted before
# anything reaches the AI provider stopped being true, and everyone who
# agreed to them is asked again.
CURRENT_CONSENT_VERSION = "2.0"

CONSENT_KEY = tk("canvas:consent:{user_id}")
CONSENT_AUDIT_KEY = tk("canvas:consent:audit")  # list, append-only


@dataclass
class ConsentRecord:
    user_id: str
    user_name: str | None
    user_email: str | None
    course_id: str | None
    version: str
    agreed_at: float  # unix seconds
    user_agent: str | None = None
    ip: str | None = None


async def get_consent(redis: Redis, user_id: str) -> ConsentRecord | None:
    """Return the latest consent record for a user, or None if never given."""
    raw: Any = await redis.get(CONSENT_KEY.format(user_id=user_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return ConsentRecord(**data)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Corrupt consent record for %s: %s", user_id, exc)
        return None


async def put_consent(redis: Redis, record: ConsentRecord) -> None:
    """Persist consent + append to the immutable audit log."""
    payload = json.dumps(asdict(record))
    await redis.set(CONSENT_KEY.format(user_id=record.user_id), payload)
    # The audit log is append-only — never trimmed, never updated.
    # If an admin needs to revoke, write a new record; the audit log keeps the
    # full history (who/when/what-version/from-what-IP).
    await redis.rpush(CONSENT_AUDIT_KEY, payload)


# ---------------------------------------------------------------------------
# Approval audit log — every approve/reject/request-edit is recorded
# ---------------------------------------------------------------------------
APPROVAL_AUDIT_KEY = tk("canvas:approval:audit")


async def append_approval_event(
    redis: Redis,
    *,
    job_id: str,
    action: str,                  # "approve" | "reject" | "request_edits"
    actor_user_id: str,
    actor_name: str | None,
    comment: str | None = None,
    actor_ip: str | None = None,
    course_id: str | None = None,
) -> None:
    """Append an immutable approval event to the audit log.

    ``actor_ip`` and ``course_id`` are optional because some test paths
    and historical callers don't carry them, but ISO compliance asks for
    both on every event so production callers should always pass them.
    """
    import time
    payload = json.dumps({
        "job_id": job_id,
        "action": action,
        "actor_user_id": actor_user_id,
        "actor_name": actor_name,
        "actor_ip": actor_ip,
        "course_id": course_id,
        "comment": comment,
        "at": time.time(),
    })
    await redis.rpush(APPROVAL_AUDIT_KEY, payload)


async def list_approval_events(
    redis: Redis,
    *,
    since: float | None = None,
    until: float | None = None,
    course_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return every approval event matching the filters, oldest first.

    Designed for the audit-log CSV export. Filters are inclusive:
    ``since <= at <= until``. ``course_id`` matches the event's stored
    course (jobs created before the field was added will have None and
    are skipped when this filter is set).
    """
    raw = await redis.lrange(APPROVAL_AUDIT_KEY, 0, -1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            decoded = item.decode() if isinstance(item, bytes) else str(item)
            data = json.loads(decoded)
        except (json.JSONDecodeError, AttributeError):
            continue
        at = data.get("at")
        if since is not None and (at is None or at < since):
            continue
        if until is not None and (at is None or at > until):
            continue
        if course_id is not None and str(data.get("course_id") or "") != str(course_id):
            continue
        out.append(data)
    return out


async def get_approval_history(redis: Redis, job_id: str) -> list[dict[str, Any]]:
    """Return all approval events for a specific job, oldest first."""
    raw = await redis.lrange(APPROVAL_AUDIT_KEY, 0, -1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            decoded = item.decode() if isinstance(item, bytes) else str(item)
            data = json.loads(decoded)
            if data.get("job_id") == job_id:
                out.append(data)
        except (json.JSONDecodeError, AttributeError):
            continue
    return out


async def revoke_consent(redis: Redis, user_id: str) -> None:
    """Admin operation: delete the active consent record. The audit log entry
    showing the original consent remains; the user will be re-prompted next
    launch."""
    await redis.delete(CONSENT_KEY.format(user_id=user_id))
    revoke_marker = json.dumps({
        "event": "revoke",
        "user_id": user_id,
        "at": __import__("time").time(),
    })
    await redis.rpush(CONSENT_AUDIT_KEY, revoke_marker)


def needs_consent(record: ConsentRecord | None) -> bool:
    """True if the user must be shown the disclaimer before proceeding."""
    if record is None:
        return True
    return record.version != CURRENT_CONSENT_VERSION



# ---------------------------------------------------------------------------
# Data-retention sweeper — bounded, predictable purge of old records
# ---------------------------------------------------------------------------
# ISO compliance asks us to retain audit events for a defined window and to
# delete operational data we don't need anymore. The bridge worker calls
# ``purge_old_canvas_records`` on a slow timer; both windows are independent
# so an operator can keep audit history while still rolling jobs off.
#
# Why purge by AGE rather than COUNT: storage cost in Redis is proportional
# to lifetime, and a steady cap on count would conflict with the audit
# log's append-only contract (we never want to evict an event because a
# newer one arrived).
async def purge_old_canvas_records(
    redis: Redis,
    *,
    job_retention_seconds: int,
    audit_retention_seconds: int,
) -> dict[str, int]:
    """Delete Canvas state older than the configured retention windows.

    ``job_retention_seconds <= 0`` disables job purging; the same goes for
    ``audit_retention_seconds`` and the audit log. Returns a dict of
    {bucket: count_removed} so the caller can log how much got swept.

    Jobs deleted here are the *records* in Redis - the underlying S3
    artifacts (markdown, figures) are owned by Reflow and have their own
    lifecycle. The faculty member can always re-upload to regenerate.
    """
    import time

    now = time.time()
    out = {"jobs_removed": 0, "audit_events_removed": 0}

    # ---- Jobs --------------------------------------------------------------
    if job_retention_seconds > 0:
        cutoff = now - job_retention_seconds
        cursor = 0
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match=tk("canvas:job:*"), count=200
            )
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                job_id = key.rsplit(":", 1)[-1]
                job = await get_job(redis, job_id)
                if job is None:
                    continue
                if (job.created_at or 0) > cutoff:
                    continue
                # Also remove from per-course pending set (no-op if absent).
                if job.canvas_course_id:
                    await redis.srem(
                        PENDING_KEY.format(course_id=job.canvas_course_id), job_id
                    )
                await redis.delete(key)
                # Edited HTML, the converted body and the score cache live
                # at their own keys; drop them too.
                for content_key in content_keys(job_id):
                    await redis.delete(content_key)
                # Without this, the same file uploaded again would alias
                # onto the deleted job and never be converted.
                await forget_hash_pointer(redis, job)
                out["jobs_removed"] += 1
            if cursor == 0:
                break

    # ---- Audit events ------------------------------------------------------
    # The audit log is a Redis list, so we can't SCAN it. Read once, filter
    # out the keepers, and rewrite the list atomically via a transaction so
    # we don't lose events that arrive mid-purge.
    if audit_retention_seconds > 0:
        cutoff = now - audit_retention_seconds
        raw_events = await redis.lrange(APPROVAL_AUDIT_KEY, 0, -1)
        keepers: list[str] = []
        removed = 0
        for item in raw_events:
            decoded = item.decode() if isinstance(item, bytes) else str(item)
            try:
                at = json.loads(decoded).get("at")
            except (json.JSONDecodeError, TypeError):
                # Don't drop unparseable rows - preserves the audit guarantee
                # even when an old version emitted weird JSON.
                keepers.append(decoded)
                continue
            if isinstance(at, (int, float)) and at < cutoff:
                removed += 1
                continue
            keepers.append(decoded)

        if removed > 0:
            # Replace the list in one round-trip. Pipeline guarantees both
            # commands run on the same connection without interleaving.
            async with redis.pipeline(transaction=True) as pipe:
                pipe.delete(APPROVAL_AUDIT_KEY)
                if keepers:
                    pipe.rpush(APPROVAL_AUDIT_KEY, *keepers)
                await pipe.execute()
            out["audit_events_removed"] = removed

    return out
