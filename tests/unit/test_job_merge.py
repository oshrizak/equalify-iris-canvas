"""put_job writes back only what its holder changed.

The watcher, the bridge, the stale-job sweeper and the API all hold copies
of the same job. Writing a whole stale copy back would undo what the others
saved in between; for Iris that means forgetting a session id and uploading
(and paying for) that part again.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from connector.canvas.state import CanvasJob, get_job, put_job


def _job() -> CanvasJob:
    return CanvasJob(
        reflow_job_id="j1", canvas_file_id="f1", canvas_file_name="Reader.pdf",
        canvas_course_id="c1", canvas_user_id="u1", status="processing",
        created_at=1.0, backend="iris",
        iris_parts=[
            {"index": 0, "first_page": 1, "last_page": 13, "session_id": "ses_1",
             "status": "queued", "error": None},
            {"index": 1, "first_page": 14, "last_page": 26, "session_id": None,
             "status": "pending", "error": None},
        ],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_stale_copy_does_not_erase_a_recorded_session() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    watcher = _job()
    await put_job(redis, watcher)
    bridge = await get_job(redis, "j1")
    assert bridge is not None

    # The bridge uploads part 2 while the watcher waits on its audit.
    bridge.iris_parts[1].update(session_id="ses_3", status="queued")
    await put_job(redis, bridge)

    # The watcher then saves the audit from its old copy.
    watcher.verapdf_score = 72
    await put_job(redis, watcher)

    stored = await get_job(redis, "j1")
    assert stored is not None
    assert stored.iris_parts[1]["session_id"] == "ses_3"
    assert stored.verapdf_score == 72
    # ...and the watcher's copy now knows about the session too.
    assert watcher.iris_parts[1]["session_id"] == "ses_3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_changes_to_different_fields_both_survive() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    await put_job(redis, _job())
    a = await get_job(redis, "j1")
    b = await get_job(redis, "j1")
    assert a is not None and b is not None
    a.status = "failed"
    a.error = "no progress"
    a.iris_stalled_at = 5.0
    await put_job(redis, a)
    b.iris_parts[0]["status"] = "running"
    await put_job(redis, b)

    stored = await get_job(redis, "j1")
    assert stored is not None
    assert (stored.status, stored.error, stored.iris_stalled_at) == ("failed", "no progress", 5.0)
    assert stored.iris_parts[0]["status"] == "running"
    # b adopted a's status, so its next write cannot quietly revert it.
    assert b.status == "failed"
    b.content_lang = "en"
    await put_job(redis, b)
    stored = await get_job(redis, "j1")
    assert stored is not None and stored.status == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_part_dicts_stay_live_across_saves() -> None:
    """Upload loops hold references to part dicts and save after each one."""
    redis = fakeredis.aioredis.FakeRedis()
    await put_job(redis, _job())
    job = await get_job(redis, "j1")
    assert job is not None
    first, second = job.iris_parts
    first["status"] = "running"
    await put_job(redis, job)
    second.update(session_id="ses_2", status="queued")
    await put_job(redis, job)
    stored = await get_job(redis, "j1")
    assert stored is not None
    assert [p["session_id"] for p in stored.iris_parts] == ["ses_1", "ses_2"]
    assert job.iris_parts[1] is second


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_deleted_job_is_not_recreated() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    await put_job(redis, _job())
    job = await get_job(redis, "j1")
    assert job is not None
    for key in await redis.keys("*canvas:job:j1"):
        await redis.delete(key)
    job.status = "awaiting_review"
    await put_job(redis, job)
    assert await get_job(redis, "j1") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resetting_a_part_is_a_change_like_any_other() -> None:
    """A retry clears a failed part's session; that must be written."""
    redis = fakeredis.aioredis.FakeRedis()
    await put_job(redis, _job())
    job = await get_job(redis, "j1")
    assert job is not None
    job.iris_parts[0].update(session_id=None, status="pending", error=None)
    await put_job(redis, job)
    stored = await get_job(redis, "j1")
    assert stored is not None and stored.iris_parts[0]["session_id"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_conditional_write_loses_to_a_change_it_did_not_see() -> None:
    """The stale-job sweeper fails a job only if it is still processing: the
    bridge may have completed it between the sweeper's read and its write."""
    redis = fakeredis.aioredis.FakeRedis()
    await put_job(redis, _job())
    sweeper = await get_job(redis, "j1")
    bridge = await get_job(redis, "j1")
    assert sweeper is not None and bridge is not None
    bridge.completed_at = 9.0
    bridge.status = "awaiting_review"
    await put_job(redis, bridge)

    sweeper.status = "failed"
    written = await put_job(redis, sweeper, only_if={"status": "processing", "completed_at": None})
    assert written is False
    stored = await get_job(redis, "j1")
    assert stored is not None and stored.status == "awaiting_review"
