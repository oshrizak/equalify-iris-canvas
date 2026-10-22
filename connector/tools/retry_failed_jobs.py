"""Send documents that did not convert back to Iris, in bulk.

After an outage or a configuration mistake on the Iris side (a model
provider key that expired, a wrong model name), every document converted in
that window fails. Faculty can use "Try again" on the Accessible Documents
page one document at a time; this does the same for many at once, using the
same rules (see ``connector.canvas.iris_jobs.requeue_job``):

* an Iris conversion that failed part-way re-sends only the parts that
  failed or that Iris lost, after reserving their cost against the course's
  monthly cap;
* a document that never started (the file could not be prepared), or a
  Reflow-era conversion that never produced anything, is handed back to the
  watcher, which converts it with Iris on its next tick.

Courses whose owner has not accepted the current disclaimer are skipped and
listed; their documents wait, like everything else in those courses.

Usage (inside the connector container)::

    python -m connector.tools.retry_failed_jobs --dry-run
    python -m connector.tools.retry_failed_jobs
    python -m connector.tools.retry_failed_jobs --course-id 50594
    python -m connector.tools.retry_failed_jobs --since 2026-10-20

Fix whatever made them fail first: retrying into the same problem only
spends the reservation again.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from ..canvas.audit import emit_operator_action
from ..canvas.consent_gate import course_owner_consents
from ..canvas.iris_jobs import requeue_job
from ..canvas.state import CanvasJob, get_job
from ..canvas.tenant import tk
from ..dependencies import get_redis_client


async def _failed_jobs(redis: Any, course_id: str | None, since: float | None) -> list[CanvasJob]:
    out: list[CanvasJob] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=tk("canvas:job:*"), count=200)
        for raw in keys:
            key = raw.decode() if isinstance(raw, bytes) else raw
            job = await get_job(redis, key.rsplit(":", 1)[-1])
            if job is None or job.status != "failed" or job.completed_at:
                continue
            if course_id and str(job.canvas_course_id) != str(course_id):
                continue
            if since and float(job.created_at or 0) < since:
                continue
            out.append(job)
        if cursor == 0:
            break
    out.sort(key=lambda j: j.created_at or 0)
    return out


async def run(
    redis: Any, *, course_id: str | None, since: float | None, dry_run: bool,
) -> dict[str, Any]:
    jobs = await _failed_jobs(redis, course_id, since)
    outcomes: Counter[str] = Counter()
    waiting: set[str] = set()
    consent: dict[str, bool] = {}
    for job in jobs:
        course = str(job.canvas_course_id)
        if course not in consent:
            consent[course], _owner = await course_owner_consents(redis, course)
        if not consent[course]:
            waiting.add(course)
            outcomes["waiting_for_consent"] += 1
            continue
        label = f"{course}/{job.canvas_file_id} {job.canvas_file_name!r}"
        if dry_run:
            print(f"  would retry {label}: {job.error or 'no reason recorded'}")
            outcomes["would_retry"] += 1
            continue
        result = await requeue_job(redis, job)
        outcomes[result.outcome] += 1
        print(f"  {result.outcome:<12} {label}")
    summary: dict[str, Any] = {"failed_jobs": len(jobs), **outcomes}
    if waiting:
        summary["courses_waiting_for_consent"] = sorted(waiting)
    return summary


async def _main(args: argparse.Namespace) -> int:
    redis = await anext(get_redis_client())
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=UTC).timestamp()
    print(f"{'DRY RUN' if args.dry_run else 'RETRY'}: failed conversions"
          f"{' in course ' + args.course_id if args.course_id else ''}"
          f"{' since ' + args.since if args.since else ''}")
    emit_operator_action(
        "retry_failed_jobs.start", course_id=args.course_id, since=args.since,
        dry_run=bool(args.dry_run),
    )
    summary = await run(redis, course_id=args.course_id, since=since, dry_run=args.dry_run)
    for key, value in summary.items():
        print(f"  {key}: {value}")
    emit_operator_action("retry_failed_jobs.end", dry_run=bool(args.dry_run), **summary)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--course-id", help="Only this Canvas course.")
    p.add_argument("--since", help="Only documents first seen on or after this date (YYYY-MM-DD, UTC).")
    p.add_argument("--dry-run", action="store_true", help="List what would be retried; change nothing.")
    sys.exit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()
