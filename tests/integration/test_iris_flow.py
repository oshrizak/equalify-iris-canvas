"""The connector's Iris path, end to end, with Iris and Canvas mocked.

Iris is mocked at its documented HTTP contract (respx); Canvas is a small
in-memory fake standing in for ``CanvasClient``. Covered:

  * watcher: a long PDF becomes one job of several Iris sessions; a file
    Iris cannot take gets a failed record that says why; Iris throttling
    stops submission without losing files.
  * bridge: polling, completion (stitch + store), figures lifted from the
    PDF into Canvas, the Canvas Page written; a failed part; pending
    uploads retried after a 429; Reflow-era jobs left alone once Reflow is
    gone, rebuilt from a migrated snapshot when asked.
  * API: the alt formats and review notes read the stored document; the
    PII decision refuses Iris jobs; approving leaves Iris sessions open by
    default, and with closing turned on closes an unedited document's
    sessions but not an edited one's.
"""

from __future__ import annotations

import hashlib
import time

import fitz
import httpx
import pytest
import respx
from connector.canvas import iris_client
from connector.canvas.iris_jobs import BACKOFF_KEY
from connector.canvas.state import (
    CanvasJob,
    get_iris_body,
    get_job,
    put_edited_html,
    put_iris_body,
    put_job,
    put_legacy_markdown,
)

from tests.integration.iris_fakes import (
    COURSE,
    IRIS,
    FakeCanvas,
    _iris_doc,
    _job,
    _mock_limits,
    _pdf,
    _run,
)

pytestmark = pytest.mark.usefixtures("iris_settings")


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_watcher_turns_a_long_pdf_into_one_job_of_two_sessions(redis):
    from connector.canvas.spend_cap import get_monthly_spend_cents
    from connector.canvas.state import already_processed, get_job_for_hash
    from connector.workers.canvas_watcher import _scan_course

    pdf = _pdf(30)
    fake = FakeCanvas({"f1": ("Week 3 reading.pdf", pdf)})
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        upload = router.post(f"{IRIS}/v1/sessions").mock(side_effect=[
            httpx.Response(201, json={"session_id": "ses_a", "status": "running", "image_count": 15}),
            httpx.Response(201, json={"session_id": "ses_b", "status": "queued", "image_count": 15}),
        ])
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)

    assert upload.call_count == 2
    job_id = await get_job_for_hash(redis, COURSE, hashlib.sha256(pdf).hexdigest())
    job = await get_job(redis, job_id)
    assert job.is_iris and job.status == "processing"
    assert [(p["first_page"], p["last_page"], p["session_id"]) for p in job.iris_parts] == [
        (1, 15, "ses_a"), (16, 30, "ses_b"),
    ]
    assert job.source_page_count == 30 and job.reserved_cost_cents == 30 * 11
    assert await already_processed(redis, COURSE, "f1")
    assert await get_monthly_spend_cents(redis, COURSE) == 330


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watcher_records_why_a_file_cannot_be_converted(redis):
    from connector.workers.canvas_watcher import _scan_course

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    locked = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    fake = FakeCanvas({"f2": ("Grades.pdf", locked)})
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        upload = router.post(f"{IRIS}/v1/sessions")
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)
    assert upload.call_count == 0
    jobs = [await get_job(redis, k.decode().rsplit(":", 1)[-1]) for k in await redis.keys("*canvas:job:*")]
    assert len(jobs) == 1 and jobs[0].status == "failed"
    assert "password-protected" in jobs[0].error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watcher_leaves_files_alone_while_iris_is_throttling(redis):
    from connector.canvas.state import already_processed
    from connector.workers.canvas_watcher import _scan_course

    await redis.set(BACKOFF_KEY, "429", ex=60)
    fake = FakeCanvas({"f3": ("a.pdf", _pdf(2))})
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)
    assert not await redis.keys("*canvas:job:*")
    assert not await already_processed(redis, COURSE, "f3")  # picked up on a later tick


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_bridge_polls_completes_lifts_figures_and_writes_the_page(redis, canvas):
    from connector.canvas.state import has_recent_iris_activity
    from connector.workers.reflow_bridge_worker import _tick

    pdf = _pdf(30, chart_on=(3,))
    canvas.files["f1"] = ("Week 3 reading.pdf", pdf)
    await put_job(redis, _job())

    part_a = _iris_doc(
        '<h1>Week 3</h1><p>Intro <a href="#fn-1">1</a></p><p id="fn-1">Note A</p>'
        '<figure><img src="page-3-chart.png" alt="Enrollment by year, rising"><figcaption>Fig 1</figcaption></figure>'
    )
    part_b = _iris_doc(
        '<p>Later <a href="#fn-1">1</a></p><p id="fn-1">Note B</p>'
        '<img src="page-2-photo.png" alt="Photo of the lab bench">',
        after="\n<!-- @page-uncorrected 4\n  x\n-->\n<!-- @unresolved\n  - Table on page 4 lost its header\n-->",
    )
    with respx.mock(assert_all_called=False) as router:
        sessions_a = router.get(f"{IRIS}/v1/sessions/ses_a").mock(
            return_value=httpx.Response(200, json={"session_id": "ses_a", "status": "ready_for_review"}))
        router.get(f"{IRIS}/v1/sessions/ses_b").mock(side_effect=[
            httpx.Response(200, json={"session_id": "ses_b", "status": "running", "phase": "review"}),
            httpx.Response(200, json={"session_id": "ses_b", "status": "ready_for_review"}),
        ])
        router.get(f"{IRIS}/v1/sessions/ses_a/output").mock(return_value=httpx.Response(200, text=part_a))
        router.get(f"{IRIS}/v1/sessions/ses_b/output").mock(return_value=httpx.Response(200, text=part_b))
        for sid in ("ses_a", "ses_b"):
            router.get(f"{IRIS}/v1/sessions/{sid}/diagnostics").mock(return_value=httpx.Response(200, json={
                "tokens": {"input": 1000, "output": 200, "cache_read": 50, "cache_write": 10},
                "model_calls": {"count": 7},
                "by_agent": {"page": {"models": ["us.anthropic.claude-sonnet-4-6"]}},
            }))

        await _tick(redis, iris_client.IrisClient(), None)  # part b still running
        job = await get_job(redis, "job-iris-1")
        assert job.status == "processing" and not job.completed_at
        assert await has_recent_iris_activity(redis, "job-iris-1")

        await _tick(redis, iris_client.IrisClient(), None)  # both ready
        assert sessions_a.call_count >= 2

    job = await get_job(redis, "job-iris-1")
    assert job.status == "awaiting_review", job.error
    assert job.completed_at and job.content_lang == "en"
    assert job.iris_usage["input"] == 2000 and job.iris_usage["sessions"] == 2
    assert job.iris_report["uncorrected_pages"] == [19]  # part 2, session page 4
    body = await get_iris_body(redis, "job-iris-1")
    assert 'id="part2-fn-1"' in body and 'href="#part2-fn-1"' in body

    # The chart on PDF page 3 was lifted and uploaded; the lab photo Iris
    # put on page 17 has no picture there, so it is described instead.
    assert job.figure_canvas_urls == {"figures/p3-1.png": "https://canvas.test/files/1/download"}
    assert job.iris_figures_unmatched == ["figures/p17-1.png"]
    assert canvas.uploads == [("f1-p3-1.png", "image/png")]
    page = next(iter(canvas.pages.values()))
    assert page["published"] is False
    assert "generated by Equalify Iris" in page["body"]
    assert "https://canvas.test/files/1/download" in page["body"]
    assert "Image (page 17 of the original PDF): Photo of the lab bench" in page["body"]
    assert "Original PDF" in page["body"]
    assert job.canvas_page_url.endswith("/pages/week-3-reading")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bridge_fails_the_job_when_a_part_fails(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job())
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(
            return_value=httpx.Response(200, json={"session_id": "ses_a", "status": "ready_for_review"}))
        router.get(f"{IRIS}/v1/sessions/ses_b").mock(return_value=httpx.Response(200, json={
            "session_id": "ses_b", "status": "failed",
            "error": "bedrock: response hit the 32000-token output ceiling",
        }))
        await _tick(redis, iris_client.IrisClient(), None)
    job = await get_job(redis, "job-iris-1")
    assert job.status == "failed"
    assert "pages 16-30" in job.error and "output ceiling" in job.error
    assert not canvas.pages


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_bridge_retries_pending_uploads_and_honours_429(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    pdf = _pdf(30)
    canvas.files["f1"] = ("Week 3 reading.pdf", pdf)
    job = _job(source_sha256=hashlib.sha256(pdf).hexdigest())
    job.iris_parts[1].update(session_id=None, status="pending")
    await put_job(redis, job)

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(
            return_value=httpx.Response(200, json={"session_id": "ses_a", "status": "running"}))
        upload = router.post(f"{IRIS}/v1/sessions").mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "30"},
                           json={"error": {"code": "rate_limited", "message": "Too many requests"}}),
            httpx.Response(201, json={"session_id": "ses_b2", "status": "queued"}),
        ])
        await _tick(redis, iris_client.IrisClient(), None)
        job = await get_job(redis, "job-iris-1")
        assert job.iris_parts[1]["session_id"] is None and job.status == "processing"
        assert 0 < await redis.ttl(BACKOFF_KEY) <= 30

        await _tick(redis, iris_client.IrisClient(), None)  # still backing off
        assert upload.call_count == 1

        await redis.delete(BACKOFF_KEY)
        await _tick(redis, iris_client.IrisClient(), None)
        assert upload.call_count == 2
    job = await get_job(redis, "job-iris-1")
    assert job.iris_parts[1]["session_id"] == "ses_b2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_file_that_changed_mid_conversion_starts_over(redis, canvas):
    """Parts cut from the old bytes cannot be stitched to the new ones: the
    job is dropped and the file left for the watcher to convert as it is."""
    from connector.canvas.state import PROCESSED_KEY, already_processed, mark_processed
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    job = _job(source_sha256="0" * 64)
    job.iris_parts[1].update(session_id=None, status="pending")
    await put_job(redis, job)
    await mark_processed(redis, COURSE, "f1")
    with respx.mock(assert_all_called=False) as router:
        upload = router.post(f"{IRIS}/v1/sessions")
        await _tick(redis, iris_client.IrisClient(), None)
    assert upload.call_count == 0
    assert await get_job(redis, "job-iris-1") is None
    assert not await already_processed(redis, COURSE, "f1")
    assert PROCESSED_KEY  # imported for clarity of what "processed" means


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_file_deleted_while_parts_wait_is_purged(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    job = _job()
    job.iris_parts[1].update(session_id=None, status="pending")
    await put_job(redis, job)
    canvas.files.pop("f1", None)  # gone from Canvas: downloads answer 404
    with respx.mock(assert_all_called=False) as router:
        upload = router.post(f"{IRIS}/v1/sessions")
        await _tick(redis, iris_client.IrisClient(), None)
    assert upload.call_count == 0
    assert await get_job(redis, "job-iris-1") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflow_jobs_are_left_alone_once_reflow_is_gone(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    await put_job(redis, CanvasJob(
        reflow_job_id="old-1", canvas_file_id="f9", canvas_file_name="Old.pdf",
        canvas_course_id=COURSE, canvas_user_id="u1", status="processing", created_at=1.0,
    ))
    with respx.mock(assert_all_called=True):  # no HTTP at all
        await _tick(redis, iris_client.IrisClient(), None)
    assert canvas.calls == []
    assert (await get_job(redis, "old-1")).status == "processing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_migrated_reflow_job_rebuilds_its_page_from_the_snapshot(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f9"] = ("Old.pdf", _pdf(1))
    await put_job(redis, CanvasJob(
        reflow_job_id="old-2", canvas_file_id="f9", canvas_file_name="Old.pdf",
        canvas_course_id=COURSE, canvas_user_id="u1", status="processing", created_at=1.0,
    ))
    await put_legacy_markdown(redis, "old-2", "# Old reading\n\nConverted by Reflow.\n")
    await _tick(redis, iris_client.IrisClient(), None)
    job = await get_job(redis, "old-2")
    assert job.status == "awaiting_review"
    body = next(iter(canvas.pages.values()))["body"]
    assert "generated by Equalify Reflow" in body and "Converted by Reflow." in body


# ---------------------------------------------------------------------------
# API surfaces
# ---------------------------------------------------------------------------

def _plant_completed(redis_client, course_id: str, *, edited: bool = False) -> None:
    async def go():
        await put_job(redis_client, _job(
            canvas_course_id=course_id, status="awaiting_review", completed_at=time.time(),
            content_lang="en",
            iris_parts=[{"index": 0, "first_page": 1, "last_page": 3, "session_id": "ses_z",
                         "status": "ready_for_review", "error": None}],
            source_page_count=3,
            iris_report={"failed_pages": [2], "unresolved": ["Heading levels skip on page 1"], "parts": 1},
            iris_figures=[], signals={"heading_levels": [1], "has_text_layer": True, "language": "en"},
        ))
        await put_iris_body(redis_client, "job-iris-1", (
            '<h1>Week 3</h1><p>Area <math alttext="\\pi r^2" display="block">'
            "<mi>π</mi></math></p><table><tr><th scope=\"col\">A</th></tr><tr><td>1</td></tr></table>"
        ))
        if edited:
            await put_edited_html(redis_client, "job-iris-1", "<h1>Week 3 (edited)</h1><p>x</p>")
    _run(go())


@pytest.mark.integration
def test_alt_formats_and_notes_read_the_stored_document(client, instructor_session, redis_client):
    _plant_completed(redis_client, instructor_session["course_id"])
    cookies = {"reflow_lti_session": instructor_session["session_id"]}
    with respx.mock(assert_all_called=True):  # nothing here may call Iris
        html = client.get("/canvas/panorama/alt/job-iris-1/html?preview=1", cookies=cookies)
        md = client.get("/canvas/panorama/alt/job-iris-1/markdown?preview=1", cookies=cookies)
        notes = client.get("/canvas/review/job-iris-1/notes", cookies=cookies)
        score = client.get("/canvas/panorama/score_by_job?job_ids=job-iris-1")
    assert html.status_code == 200 and "Week 3" in html.text and "Equalify Iris" in html.text
    assert md.status_code == 200 and "$$\\pi r^2$$" in md.text
    assert notes.status_code == 200
    codes = [n["code"] for n in notes.json()["notes"]]
    assert codes[:2] == ["pages_missing", "open_issues"]
    payload = score.json()["scores"]["job-iris-1"]
    assert payload["converter"] == "iris" and payload["notes"]


@pytest.mark.integration
def test_pii_decision_refuses_iris_documents(
    client, instructor_session, csrf_header, trusted_origin_headers, redis_client,
):
    _plant_completed(redis_client, instructor_session["course_id"])
    resp = client.post(
        "/canvas/panorama/pii-decision/job-iris-1",
        cookies={"reflow_lti_session": instructor_session["session_id"]},
        headers={**csrf_header, **trusted_origin_headers},
        json={"decision": "approved", "justification": "Reviewed by faculty in Canvas."},
    )
    assert resp.status_code == 409


def _approve(client, instructor_session, csrf_header, trusted_origin_headers):
    return client.post(
        "/canvas/panorama/approve/job-iris-1",
        cookies={"reflow_lti_session": instructor_session["session_id"]},
        headers={**csrf_header, **trusted_origin_headers, "Content-Type": "application/json"},
        json={"comment": None, "waivers": [], "checklist": {}},
    )


@pytest.mark.integration
@pytest.mark.timeout(15)
def test_approving_an_unedited_document_closes_its_iris_session(
    client, instructor_session, csrf_header, trusted_origin_headers, redis_client, monkeypatch,
):
    from connector.config import settings

    monkeypatch.setattr(settings, "iris_close_sessions_on_approve", True)
    _plant_completed(redis_client, instructor_session["course_id"])
    with respx.mock(assert_all_called=False) as router:
        close = router.post(f"{IRIS}/v1/sessions/ses_z/close").mock(
            return_value=httpx.Response(200, json={"session_id": "ses_z", "status": "closed"}))
        resp = _approve(client, instructor_session, csrf_header, trusted_origin_headers)
        assert resp.status_code == 200, resp.text
        for _ in range(50):  # closing happens off the request path
            if close.called:
                break
            time.sleep(0.05)
        assert close.call_count == 1


@pytest.mark.integration
@pytest.mark.timeout(15)
def test_approving_an_edited_document_leaves_iris_open(
    client, instructor_session, csrf_header, trusted_origin_headers, redis_client, monkeypatch,
):
    from connector.config import settings

    monkeypatch.setattr(settings, "iris_close_sessions_on_approve", True)
    _plant_completed(redis_client, instructor_session["course_id"], edited=True)
    with respx.mock(assert_all_called=False) as router:
        close = router.post(f"{IRIS}/v1/sessions/ses_z/close")
        resp = _approve(client, instructor_session, csrf_header, trusted_origin_headers)
        assert resp.status_code == 200, resp.text
        time.sleep(0.3)
        assert close.call_count == 0


@pytest.mark.integration
@pytest.mark.timeout(15)
def test_approving_leaves_iris_open_by_default(
    client, instructor_session, csrf_header, trusted_origin_headers, redis_client,
):
    # Closing makes Iris keep page images as regression fixtures outside its
    # session retention, so it is opt-in.
    _plant_completed(redis_client, instructor_session["course_id"])
    with respx.mock(assert_all_called=False) as router:
        close = router.post(f"{IRIS}/v1/sessions/ses_z/close")
        resp = _approve(client, instructor_session, csrf_header, trusted_origin_headers)
        assert resp.status_code == 200, resp.text
        time.sleep(0.3)
        assert close.call_count == 0


# ---------------------------------------------------------------------------
# Stale-job sweeper and recovery
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweeper_spares_busy_iris_jobs_and_fails_silent_ones(redis):
    from connector.canvas.state import mark_iris_activity
    from connector.workers.canvas_watcher import sweep_stale_jobs

    old = time.time() - 7200
    await put_job(redis, _job(reflow_job_id="busy", created_at=old))
    await mark_iris_activity(redis, "busy", 3600)
    await put_job(redis, _job(reflow_job_id="silent", created_at=old))
    waiting = _job(reflow_job_id="waiting", created_at=old)
    waiting.iris_parts[1].update(session_id=None, status="pending")
    await put_job(redis, waiting)
    await redis.set(BACKOFF_KEY, "429: Too many requests", ex=60)

    assert await sweep_stale_jobs(redis) == 1
    assert (await get_job(redis, "busy")).status == "processing"
    assert (await get_job(redis, "waiting")).status == "processing"  # throttled, not stuck
    silent = await get_job(redis, "silent")
    assert silent.status == "failed" and silent.iris_stalled_at
    assert "no progress" in silent.error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stalled_job_resumes_when_iris_answers_again(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(status="failed", iris_stalled_at=time.time(), error="no progress"))
    with respx.mock(assert_all_called=False) as router:
        for sid in ("ses_a", "ses_b"):
            router.get(f"{IRIS}/v1/sessions/{sid}").mock(
                return_value=httpx.Response(200, json={"session_id": sid, "status": "running"}))
        await _tick(redis, iris_client.IrisClient(), None)
    job = await get_job(redis, "job-iris-1")
    assert job.status == "processing" and job.error is None and job.iris_stalled_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failure_iris_reported_is_not_polled_again(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(status="failed", error="Iris could not convert this document"))
    with respx.mock(assert_all_called=True):  # no Iris calls at all
        await _tick(redis, iris_client.IrisClient(), None)
    assert (await get_job(redis, "job-iris-1")).status == "failed"
