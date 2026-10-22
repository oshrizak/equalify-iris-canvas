"""Carry documents converted by Equalify Reflow over to the Iris connector.

Before the switch, the connector kept no copy of a converted document: every
view of an alt format (HTML, ePub, braille, audio, ...) fetched Reflow's
markdown from Reflow Core on demand. Once Reflow Core is shut down those
views have nothing to read. This tool, run ONCE while Reflow Core is still
up, copies each finished document's markdown into the connector's own
Redis, after which Reflow can go.

Canvas Pages the bridge already wrote are Canvas's and are not touched: they
keep working without either service.

Jobs Reflow never finished (still converting, waiting on a PII decision, or
failed) cannot be snapshotted. ``--requeue-unfinished`` hands those files to
the watcher again so Iris converts them; they then follow the normal Iris
path, updating the same Canvas Page when one exists.

Usage (inside the connector container, with REFLOW_API_BASE_URL and
REFLOW_API_KEY still set)::

    python -m connector.tools.migrate_reflow_jobs --dry-run
    python -m connector.tools.migrate_reflow_jobs
    python -m connector.tools.migrate_reflow_jobs --requeue-unfinished
    python -m connector.tools.migrate_reflow_jobs --course-id 50594

Idempotent: a job that already has a snapshot is skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from ..canvas.audit import emit_operator_action
from ..canvas.content import legacy_reflow_configured
from ..canvas.reflow_client import ReflowClient
from ..canvas.state import (
    PROCESSED_KEY,
    CanvasJob,
    get_job,
    get_legacy_markdown,
    purge_job,
    put_legacy_markdown,
)
from ..canvas.tenant import tk
from ..dependencies import get_redis_client

logger = logging.getLogger(__name__)

# States in which Reflow had finished converting (the page may or may not
# have been written, and faculty may or may not have decided).
_FINISHED = {"awaiting_review", "published", "rejected", "page_failed"}
_UNFINISHED = {"processing", "awaiting_approval", "failed"}


async def _legacy_jobs(redis: Any, course_id: str | None) -> list[CanvasJob]:
    out: list[CanvasJob] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=tk("canvas:job:*"), count=200)
        for raw in keys:
            key = raw.decode() if isinstance(raw, bytes) else raw
            job = await get_job(redis, key.rsplit(":", 1)[-1])
            if job is None or job.is_iris:
                continue
            if course_id and str(job.canvas_course_id) != str(course_id):
                continue
            out.append(job)
        if cursor == 0:
            break
    out.sort(key=lambda j: j.created_at or 0)
    return out


async def _drop_hash_pointers(redis: Any, job: CanvasJob) -> int:
    """Forget content-hash entries that point at this job.

    Otherwise the watcher would see the same bytes again and alias the file
    onto the job being retired instead of converting it with Iris.
    """
    removed = 0
    pattern = tk("canvas:course:{course_id}:hash:*").format(course_id=job.canvas_course_id)
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=200)
        for raw in keys:
            value = await redis.get(raw)
            if value is None:
                continue
            text = value.decode() if isinstance(value, bytes) else str(value)
            if text == job.reflow_job_id:
                await redis.delete(raw)
                removed += 1
        if cursor == 0:
            break
    return removed


async def _snapshot(redis: Any, reflow: ReflowClient, job: CanvasJob) -> str:
    status = await reflow.get_status(job.reflow_job_id)
    if str(status.get("status") or "").lower() != "completed":
        return f"not completed in Reflow (status {status.get('status')!r})"
    url = status.get("markdown_url") or status.get("result_url")
    if not url:
        return "Reflow reports completed but returned no markdown_url"
    markdown = await reflow.fetch_markdown(url)
    if not markdown.strip():
        return "Reflow returned empty markdown"
    await put_legacy_markdown(redis, job.reflow_job_id, markdown)
    return ""


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not legacy_reflow_configured():
        print(
            "REFLOW_API_BASE_URL is not set. Point it (and REFLOW_API_KEY) at the "
            "Reflow Core that converted these documents, then run this again.",
            file=sys.stderr,
        )
        return 1
    redis = await anext(get_redis_client())
    reflow = ReflowClient()
    jobs = await _legacy_jobs(redis, args.course_id)
    print(f"{len(jobs)} Reflow-era job(s){' in course ' + args.course_id if args.course_id else ''}.")

    counts = {"snapshotted": 0, "already": 0, "failed": 0, "requeued": 0, "left": 0}
    emit_operator_action(
        "migrate_reflow_jobs.start",
        target_course=args.course_id, job_count=len(jobs), dry_run=args.dry_run,
        requeue_unfinished=args.requeue_unfinished,
    )
    for job in jobs:
        label = f"[{job.reflow_job_id}] {job.canvas_file_name} ({job.status})"
        if job.status in _FINISHED:
            if await get_legacy_markdown(redis, job.reflow_job_id) is not None:
                counts["already"] += 1
                continue
            if args.dry_run:
                print(f"  would snapshot {label}")
                counts["snapshotted"] += 1
                continue
            try:
                problem = await _snapshot(redis, reflow, job)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                problem = f"{exc.__class__.__name__}: {exc}"
            if problem:
                counts["failed"] += 1
                print(f"  FAILED {label}: {problem}")
            else:
                counts["snapshotted"] += 1
                print(f"  snapshot {label}")
        elif job.status in _UNFINISHED:
            if not args.requeue_unfinished:
                counts["left"] += 1
                print(f"  unfinished, left as is {label} (use --requeue-unfinished)")
                continue
            if args.dry_run:
                print(f"  would requeue for Iris {label}")
                counts["requeued"] += 1
                continue
            await _drop_hash_pointers(redis, job)
            await purge_job(redis, job)
            # purge_job already clears the processed marker; this is the
            # same call spelled out, so the intent survives a refactor.
            await redis.srem(PROCESSED_KEY.format(course_id=job.canvas_course_id), job.canvas_file_id)
            counts["requeued"] += 1
            print(f"  requeued for Iris {label}")
        else:
            counts["left"] += 1

    print(
        "done: {snapshotted} snapshotted, {already} already done, {failed} failed, "
        "{requeued} requeued for Iris, {left} left as is".format(**counts)
    )
    if counts["failed"]:
        print(
            "Failed snapshots keep working only while Reflow Core is reachable. "
            "Re-run this tool, or convert those files again with Iris.",
        )
    emit_operator_action("migrate_reflow_jobs.end", target_course=args.course_id, **counts)
    return 0 if counts["failed"] == 0 else 2


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--course-id", help="Only this Canvas course.")
    p.add_argument("--dry-run", action="store_true", help="Report what would happen; change nothing.")
    p.add_argument(
        "--requeue-unfinished", action="store_true",
        help="Send files Reflow never finished back to the watcher, for Iris.",
    )
    sys.exit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()
