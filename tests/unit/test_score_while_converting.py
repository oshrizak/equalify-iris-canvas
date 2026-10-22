"""Asking for a document's score while Iris is still converting it is normal.

The review page and the overlay ask for the dial as soon as a job exists. A
job that is still converting has no document to score yet, and that used to
be logged as an ERROR with a traceback on every poll.
"""

from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest
from connector.api.canvas_panorama import _compute_score
from connector.canvas.state import CanvasJob, put_job


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_document_still_converting_is_unscanned_and_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = fakeredis.aioredis.FakeRedis()
    await put_job(redis, CanvasJob(
        reflow_job_id="j-busy", canvas_file_id="f1", canvas_file_name="Reader.pdf",
        canvas_course_id="c1", canvas_user_id="u1", status="processing",
        created_at=1.0, backend="iris",
        iris_parts=[{"index": 0, "first_page": 1, "last_page": 2, "session_id": "ses_1",
                     "status": "running", "error": None}],
    ))
    with caplog.at_level(logging.DEBUG, logger="connector.api.canvas_panorama"):
        score = await _compute_score("j-busy", redis)
    assert score.status == "unscanned" and score.score is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
