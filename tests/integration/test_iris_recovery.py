"""How Iris conversions recover, and when they must not.

The cases an independent review found: a stale job copy erasing a recorded
session (and so paying for a part twice), a session Iris expired stranding a
job, every failed part recorded at once, uploads whose outcome is unknown,
stalled jobs Iris rejects, and the retry paths faculty and operators use.
Iris is mocked at its HTTP contract; Canvas is the in-memory fake.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import httpx
import pytest
import respx
from connector.canvas import iris_client
from connector.canvas.iris_jobs import BACKOFF_KEY
from connector.canvas.state import (
    CURRENT_CONSENT_VERSION,
    ConsentRecord,
    get_job,
    put_consent,
    put_job,
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

READY = {"status": "ready_for_review"}


def _parts(*specs: tuple[int, int, str | None, str]) -> list[dict]:
    return [
        {"index": i, "first_page": a, "last_page": b, "session_id": sid, "status": st, "error": None}
        for i, (a, b, sid, st) in enumerate(specs)
    ]


def _diagnostics(router) -> None:
    router.get(url__regex=rf"{IRIS}/v1/sessions/[^/]+/diagnostics").mock(
        return_value=httpx.Response(404, json={"error": {"code": "session_not_found", "message": "x"}}))


async def _consent(redis, user_id: str) -> None:
    await put_consent(redis, ConsentRecord(
        user_id=user_id, user_name=None, user_email=None, course_id=COURSE,
        version=CURRENT_CONSENT_VERSION, agreed_at=time.time(),
    ))


# ---------------------------------------------------------------------------
# Concurrency: the watcher and the bridge on one job
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_part_the_bridge_uploads_while_the_watcher_waits_is_not_uploaded_again(
    redis, canvas, monkeypatch,
):
    import connector.workers.canvas_watcher as watcher
    from connector.workers.reflow_bridge_worker import _tick

    release = asyncio.Event()

    async def slow_audit(filename, content):
        await release.wait()
        return {"score": 61, "is_compliant": False, "violations": [], "audited_at": 1.0}

    monkeypatch.setattr(watcher, "_run_verapdf_audit", slow_audit)
    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        upload = router.post(f"{IRIS}/v1/sessions").mock(side_effect=[
            httpx.Response(201, json={"session_id": "ses_1", "status": "queued", "image_count": 15}),
            httpx.Response(429, headers={"Retry-After": "10"},
                           json={"error": {"code": "rate_limited", "message": "slow down"}}),
            httpx.Response(201, json={"session_id": "ses_2", "status": "queued", "image_count": 15}),
        ])
        router.get(url__regex=rf"{IRIS}/v1/sessions/ses_\d$").mock(
            return_value=httpx.Response(200, json={"status": "running", "phase": "extraction"}))

        scan = asyncio.create_task(
            watcher._scan_course(redis, canvas, iris_client.IrisClient(), COURSE))
        for _ in range(500):  # until the watcher waits on its audit
            if upload.call_count >= 2:
                break
            await asyncio.sleep(0.01)
        await redis.delete(BACKOFF_KEY)  # the throttle window passes
        await _tick(redis, iris_client.IrisClient(), None)  # the bridge uploads part 2
        release.set()
        await scan  # ...and the watcher saves its audit from its old copy
        await _tick(redis, iris_client.IrisClient(), None)

    assert upload.call_count == 3
    keys = await redis.keys("*canvas:job:*")
    job = await get_job(redis, keys[0].decode().rsplit(":", 1)[-1])
    assert [p["session_id"] for p in job.iris_parts] == ["ses_1", "ses_2"]
    assert job.verapdf_score == 61


# ---------------------------------------------------------------------------
# Lost and failed parts
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_part_iris_expired_is_sent_again_while_the_document_is_wanted(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(iris_parts=_parts((1, 15, "ses_a", "ready_for_review"),
                                                 (16, 30, "ses_b", "ready_for_review"))))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(return_value=httpx.Response(200, json=READY))
        # Iris's retention deleted part 2's files: 200 with expired_at, not 410.
        router.get(f"{IRIS}/v1/sessions/ses_b").mock(return_value=httpx.Response(
            200, json={**READY, "expired_at": "2026-10-01T00:00:00.000Z"}))
        upload = router.post(f"{IRIS}/v1/sessions").mock(return_value=httpx.Response(
            201, json={"session_id": "ses_c", "status": "queued", "image_count": 15}))
        router.get(f"{IRIS}/v1/sessions/ses_c").mock(return_value=httpx.Response(200, json=READY))
        router.get(f"{IRIS}/v1/sessions/ses_a/output").mock(
            return_value=httpx.Response(200, text=_iris_doc("<p>Part one</p>")))
        router.get(f"{IRIS}/v1/sessions/ses_c/output").mock(
            return_value=httpx.Response(200, text=_iris_doc("<p>Part two</p>")))
        _diagnostics(router)

        await _tick(redis, iris_client.IrisClient(), None)
        job = await get_job(redis, "job-iris-1")
        assert job.status == "processing"
        assert job.iris_parts[1]["session_id"] is None and job.iris_parts[1]["status"] == "pending"

        await _tick(redis, iris_client.IrisClient(), None)  # re-upload, poll, complete, page
    assert upload.call_count == 1
    job = await get_job(redis, "job-iris-1")
    assert job.status == "awaiting_review", job.error
    assert [p["session_id"] for p in job.iris_parts] == ["ses_a", "ses_c"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_expired_part_of_a_stalled_job_fails_it_once(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(
        status="failed", iris_stalled_at=time.time() - 60, error="no progress",
        iris_parts=_parts((1, 15, "ses_a", "ready_for_review"), (16, 30, "ses_b", "running")),
    ))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(return_value=httpx.Response(200, json=READY))
        status_b = router.get(f"{IRIS}/v1/sessions/ses_b").mock(return_value=httpx.Response(
            200, json={**READY, "expired_at": "2026-10-01T00:00:00.000Z"}))
        upload = router.post(f"{IRIS}/v1/sessions")
        await _tick(redis, iris_client.IrisClient(), None)
        await _tick(redis, iris_client.IrisClient(), None)
    job = await get_job(redis, "job-iris-1")
    assert job.status == "failed" and job.iris_stalled_at is None
    assert "no longer has these pages" in job.error and "Try again" in job.error
    assert upload.call_count == 0
    assert status_b.call_count == 1  # a reported failure is not polled again


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_failed_part_is_recorded_and_retry_sends_them_all(redis, canvas):
    from connector.canvas.iris_jobs import requeue_job
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(iris_parts=_parts(
        (1, 10, "ses_a", "running"), (11, 20, "ses_b", "running"), (21, 30, "ses_c", "running"),
    )))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(return_value=httpx.Response(
            200, json={"status": "failed", "error": "model provider returned 401"}))
        router.get(f"{IRIS}/v1/sessions/ses_b").mock(return_value=httpx.Response(404, json={
            "error": {"code": "session_not_found", "message": "No such session"}}))
        router.get(f"{IRIS}/v1/sessions/ses_c").mock(return_value=httpx.Response(200, json=READY))
        await _tick(redis, iris_client.IrisClient(), None)
    job = await get_job(redis, "job-iris-1")
    assert job.status == "failed"
    # Part 2 was lost while the job was wanted, so it went back for upload;
    # part 1 failed in Iris and is recorded with Iris's reason.
    assert "pages 1-10" in job.error and "model provider returned 401" in job.error
    assert [p["status"] for p in job.iris_parts] == ["failed", "pending", "ready_for_review"]

    result = await requeue_job(redis, job)
    assert result.outcome == "resend"
    job = await get_job(redis, "job-iris-1")
    assert job.status == "processing"
    assert [p["session_id"] for p in job.iris_parts] == [None, None, "ses_c"]
    assert job.reserved_cost_cents == 20 * 11  # the re-sent pages, reserved again


@pytest.mark.integration
@pytest.mark.asyncio
async def test_output_gone_at_completion_is_a_lost_part(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(iris_parts=_parts((1, 30, "ses_a", "running"))))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(return_value=httpx.Response(200, json=READY))
        router.get(f"{IRIS}/v1/sessions/ses_a/output").mock(return_value=httpx.Response(410, json={
            "error": {"code": "session_expired", "message": "expired"}}))
        await _tick(redis, iris_client.IrisClient(), None)
    job = await get_job(redis, "job-iris-1")
    assert job.status == "processing" and not job.completed_at
    assert job.iris_parts[0]["status"] == "pending" and job.iris_parts[0]["lost_resends"] == 1


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_uploads_with_no_answer_are_retried_a_bounded_number_of_times(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(iris_parts=_parts((1, 30, None, "pending"))))
    with respx.mock(assert_all_called=False) as router:
        upload = router.post(f"{IRIS}/v1/sessions").mock(side_effect=[
            httpx.ReadTimeout("no answer"),
            httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow"}}),
            httpx.ReadTimeout("no answer"),
            httpx.Response(504, text="gateway timeout"),
        ])
        for _ in range(4):
            await redis.delete(BACKOFF_KEY)
            await _tick(redis, iris_client.IrisClient(), None)
        await redis.delete(BACKOFF_KEY)
        await _tick(redis, iris_client.IrisClient(), None)  # nothing left to try
    assert upload.call_count == 4  # the 429 did not count against the part
    job = await get_job(redis, "job-iris-1")
    assert job.status == "failed"
    assert "did not confirm receiving" in job.error
    assert job.iris_parts[0]["upload_attempts"] == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stalled_job_whose_part_iris_rejects_is_not_sent_again(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(
        status="failed", iris_stalled_at=time.time() - 60, error="no progress",
        iris_parts=_parts((1, 15, "ses_a", "running"), (16, 30, None, "pending")),
    ))
    with respx.mock(assert_all_called=False) as router:
        upload = router.post(f"{IRIS}/v1/sessions").mock(return_value=httpx.Response(
            422, json={"error": {"code": "pdf_conversion_failed", "message": "Could not process"}}))
        router.get(f"{IRIS}/v1/sessions/ses_a").mock(
            return_value=httpx.Response(200, json={"status": "running"}))
        await _tick(redis, iris_client.IrisClient(), None)
        job = await get_job(redis, "job-iris-1")
        # Failed for good straight away; not mistaken for Iris coming back.
        assert job.status == "failed" and job.iris_stalled_at is None
        assert "Could not process" in job.error
        for _ in range(2):
            await _tick(redis, iris_client.IrisClient(), None)
    assert upload.call_count == 1
    job = await get_job(redis, "job-iris-1")
    assert job.status == "failed" and job.iris_stalled_at is None


# ---------------------------------------------------------------------------
# What the watcher converts
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_course_waits_until_its_owner_accepts_the_current_disclaimer(redis):
    from connector.canvas.state import already_processed
    from connector.lti.platform_store import claim_course_owner_if_unset
    from connector.workers.canvas_watcher import _scan_course

    await claim_course_owner_if_unset(redis, course_id=COURSE, user_id="owner-1")
    await put_consent(redis, ConsentRecord(  # agreed to the Reflow-era text only
        user_id="owner-1", user_name=None, user_email=None, course_id=COURSE,
        version="1.0", agreed_at=time.time(),
    ))
    fake = FakeCanvas({"f1": ("a.pdf", _pdf(2))})
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        upload = router.post(f"{IRIS}/v1/sessions").mock(return_value=httpx.Response(
            201, json={"session_id": "ses_1", "status": "queued", "image_count": 2}))
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)
        assert fake.calls == [] and upload.call_count == 0
        assert not await already_processed(redis, COURSE, "f1")

        await _consent(redis, "owner-1")
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)
    assert upload.call_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_copy_of_a_failed_or_deleted_conversion_is_converted(redis):
    from connector.canvas.iris_jobs import failed_job
    from connector.canvas.state import put_job_for_hash
    from connector.workers.canvas_watcher import _scan_course

    pdf = _pdf(2)
    sha = hashlib.sha256(pdf).hexdigest()
    await put_job(redis, failed_job(
        course_id=COURSE, file_id="old", filename="a.pdf", uploader="u1",
        platform_id=None, error="Iris was down", sha256=sha,
    ))
    old = (await redis.keys("*canvas:job:*"))[0].decode().rsplit(":", 1)[-1]
    await put_job_for_hash(redis, COURSE, sha, old)
    fake = FakeCanvas({"f2": ("a copy.pdf", pdf)})
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        upload = router.post(f"{IRIS}/v1/sessions").mock(return_value=httpx.Response(
            201, json={"session_id": "ses_1", "status": "queued", "image_count": 2}))
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)
    assert upload.call_count == 1
    assert len(await redis.keys("*canvas:job:*")) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrying_a_document_that_could_not_be_prepared_starts_over(redis):
    from connector.canvas.iris_jobs import failed_job, requeue_job
    from connector.canvas.state import already_processed, mark_processed

    job = failed_job(course_id=COURSE, file_id="f9", filename="x.pdf", uploader="u1",
                     platform_id=None, error="This PDF has no pages.")
    await put_job(redis, job)
    await mark_processed(redis, COURSE, "f9")
    job = await get_job(redis, job.reflow_job_id)
    result = await requeue_job(redis, job)
    assert result.outcome == "restart"
    assert await get_job(redis, job.reflow_job_id) is None
    assert not await already_processed(redis, COURSE, "f9")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_operator_retry_skips_courses_waiting_for_consent(redis):
    from connector.lti.platform_store import claim_course_owner_if_unset
    from connector.tools.retry_failed_jobs import run

    await put_job(redis, _job(status="failed", error="Iris could not convert",
                              iris_parts=_parts((1, 30, "ses_a", "failed"))))
    await put_job(redis, _job(reflow_job_id="job-2", canvas_course_id="c2", status="failed",
                              iris_parts=_parts((1, 30, "ses_q", "failed"))))
    await claim_course_owner_if_unset(redis, course_id="c2", user_id="owner-2")  # never agreed

    dry = await run(redis, course_id=None, since=None, dry_run=True)
    assert dry["would_retry"] == 1 and dry["courses_waiting_for_consent"] == ["c2"]
    assert (await get_job(redis, "job-iris-1")).status == "failed"

    done = await run(redis, course_id=None, since=None, dry_run=False)
    assert done["resend"] == 1
    assert (await get_job(redis, "job-iris-1")).status == "processing"
    assert (await get_job(redis, "job-2")).status == "failed"


# ---------------------------------------------------------------------------
# "Try again" on the review screen
# ---------------------------------------------------------------------------

def _plant_failed(redis_client) -> None:
    async def go():
        await put_job(redis_client, _job(status="failed", error="Iris could not convert",
                                         iris_parts=_parts((1, 15, "ses_a", "failed"),
                                                           (16, 30, "ses_b", "ready_for_review"))))
    _run(go())


def _own_origin() -> dict[str, str]:
    from connector.config import settings

    public = (settings.lti_public_url or "https://lti.test").rstrip("/")
    return {"Origin": public}


@pytest.mark.integration
def test_try_again_resends_the_failed_parts(client, instructor_session, csrf_header, redis_client,
                                            monkeypatch):
    from connector.config import settings

    monkeypatch.setattr(settings, "lti_public_url", "https://lti.test")
    _plant_failed(redis_client)
    _run(_consent(redis_client, instructor_session["user_id"]))
    cookies = {"reflow_lti_session": instructor_session["session_id"]}

    files = client.get("/canvas/review/api/files", cookies=cookies).json()["files"]
    assert files[0]["can_retry"] is True

    resp = client.post("/canvas/review/job-iris-1/retry", cookies=cookies,
                       headers={**csrf_header, **_own_origin()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "resend"
    job = _run(get_job(redis_client, "job-iris-1"))
    assert job.status == "processing"
    assert [p["session_id"] for p in job.iris_parts] == [None, "ses_b"]

    again = client.post("/canvas/review/job-iris-1/retry", cookies=cookies,
                        headers={**csrf_header, **_own_origin()})
    assert again.status_code == 409  # only a document that did not convert


@pytest.mark.integration
def test_try_again_needs_the_csrf_token_and_a_current_disclaimer(
    client, instructor_session, csrf_header, redis_client, monkeypatch,
):
    from connector.config import settings

    monkeypatch.setattr(settings, "lti_public_url", "https://lti.test")
    monkeypatch.setattr(settings, "canvas_allowed_origins", "https://canvas.test")
    _plant_failed(redis_client)
    cookies = {"reflow_lti_session": instructor_session["session_id"]}

    no_token = client.post("/canvas/review/job-iris-1/retry", cookies=cookies, headers=_own_origin())
    assert no_token.status_code == 403

    no_consent = client.post("/canvas/review/job-iris-1/retry", cookies=cookies,
                             headers={**csrf_header, **_own_origin()})
    assert no_consent.status_code == 403 and "disclaimer" in no_consent.json()["detail"]

    _run(_consent(redis_client, instructor_session["user_id"]))
    foreign = client.post("/canvas/review/job-iris-1/retry", cookies=cookies,
                          headers={**csrf_header, "Origin": "https://evil.example"})
    assert foreign.status_code == 403
    assert _run(get_job(redis_client, "job-iris-1")).status == "failed"


@pytest.mark.integration
def test_students_cannot_try_again(client, redis_client, monkeypatch):
    from connector.api.canvas_panorama import _csrf_token_for
    from connector.config import settings
    from connector.lti.session import SessionPayload, new_session_id, put_session

    monkeypatch.setattr(settings, "lti_public_url", "https://lti.test")
    _plant_failed(redis_client)
    sid = new_session_id()
    _run(put_session(redis_client, sid, SessionPayload(
        user_id="student-1", user_name="S", user_email=None, course_id=COURSE,
        roles=["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"],
    )))
    _run(_consent(redis_client, "student-1"))
    cookies = {"reflow_lti_session": sid}
    files = client.get("/canvas/review/api/files", cookies=cookies).json()["files"]
    assert files[0]["can_retry"] is False
    resp = client.post("/canvas/review/job-iris-1/retry", cookies=cookies,
                       headers={"X-CSRF-Token": _csrf_token_for(sid), **_own_origin()})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The public figure route
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_figure_route_never_downloads_the_source(client, redis_client):
    async def go():
        await put_job(redis_client, _job(
            status="published", completed_at=time.time(),
            iris_figures=[{"ref": "figures/p3-1.png", "page": 3, "index_on_page": 1,
                           "alt": "Chart", "placeholder": "page-3-chart.png", "decorative": False}],
            figure_canvas_urls={"figures/p3-1.png": "https://canvas.test/files/1/download"},
        ))
    _run(go())
    ok = client.get("/canvas/panorama/alt/job-iris-1/figures/p3-1.png", follow_redirects=False)
    assert ok.status_code == 307 and ok.headers["location"] == "https://canvas.test/files/1/download"
    missing = client.get("/canvas/panorama/alt/job-iris-1/figures/p9-1.png", follow_redirects=False)
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Figure uploads that fail
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_figure_uploads_wait_and_then_give_up(redis, canvas, monkeypatch):
    import connector.workers.reflow_bridge_worker as bridge
    from connector.canvas.state import IRIS_FIGURES_RETRY_KEY

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(5, chart_on=(3,)))

    async def refuse(*args, **kwargs):
        raise RuntimeError("Canvas said no")

    monkeypatch.setattr(canvas, "upload_course_file", refuse)
    await put_job(redis, _job(
        completed_at=time.time(),
        iris_figures=[{"ref": "figures/p3-1.png", "page": 3, "index_on_page": 1,
                       "alt": "Chart", "placeholder": "page-3-chart.png", "decorative": False}],
    ))
    job = await get_job(redis, "job-iris-1")
    await bridge._resolve_iris_figures(redis, canvas, job)
    downloads = canvas.calls.count("download:f1")
    await bridge._resolve_iris_figures(redis, canvas, job)  # waiting: no download
    assert canvas.calls.count("download:f1") == downloads == 1

    for _ in range(bridge._FIGURE_MAX_ATTEMPTS):
        await redis.delete(IRIS_FIGURES_RETRY_KEY.format(job_id="job-iris-1"))
        await bridge._resolve_iris_figures(redis, canvas, job)
    job = await get_job(redis, "job-iris-1")
    assert job.iris_figures_unmatched == ["figures/p3-1.png"]  # shown as its description
    assert canvas.calls.count("download:f1") == bridge._FIGURE_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Second review: edge cases of the fixes
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_gateway_502_is_an_outage_not_an_uncertain_upload(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job(iris_parts=_parts((1, 30, None, "pending"))))
    with respx.mock(assert_all_called=False) as router:
        upload = router.post(f"{IRIS}/v1/sessions").mock(return_value=httpx.Response(502, text="Bad Gateway"))
        for _ in range(5):
            await redis.delete(BACKOFF_KEY)
            await _tick(redis, iris_client.IrisClient(), None)
    assert upload.call_count == 5
    job = await get_job(redis, "job-iris-1")
    assert job.status == "processing"  # waits out the outage, never gives up
    assert job.iris_parts[0]["upload_attempts"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_404_that_is_not_iris_does_not_forget_sessions(redis, canvas):
    from connector.workers.reflow_bridge_worker import _tick

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(30))
    await put_job(redis, _job())
    with respx.mock(assert_all_called=False) as router:
        # A proxy route or a wrong base URL: 404, but not Iris's session_not_found.
        router.get(url__regex=rf"{IRIS}/v1/sessions/ses_[ab]$").mock(
            return_value=httpx.Response(404, text="<html>Not Found</html>"))
        upload = router.post(f"{IRIS}/v1/sessions")
        await _tick(redis, iris_client.IrisClient(), None)
    assert upload.call_count == 0
    job = await get_job(redis, "job-iris-1")
    assert job.status == "processing"
    assert [p["session_id"] for p in job.iris_parts] == ["ses_a", "ses_b"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_upload_cut_short_by_a_restart_still_counts(redis):
    from connector.canvas.iris_jobs import upload_pending_parts

    class Restarting(iris_client.IrisClient):
        async def create_session(self, filename, pdf_bytes, **kw):
            raise asyncio.CancelledError  # the worker is stopped mid-upload

    pdf = _pdf(30)
    await put_job(redis, _job(iris_parts=_parts((1, 30, None, "pending"))))
    for _ in range(3):
        job = await get_job(redis, "job-iris-1")
        with pytest.raises(asyncio.CancelledError):
            await upload_pending_parts(redis, Restarting(), job, pdf)
    job = await get_job(redis, "job-iris-1")
    assert job.iris_parts[0]["upload_attempts"] == 3
    with respx.mock(assert_all_called=False) as router:
        upload = router.post(f"{IRIS}/v1/sessions")
        await upload_pending_parts(redis, iris_client.IrisClient(), job, pdf)
    assert upload.call_count == 0
    job = await get_job(redis, "job-iris-1")
    assert job.status == "failed" and "did not confirm receiving" in job.error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uploads_stop_when_the_job_is_deleted(redis):
    from connector.canvas.iris_jobs import upload_pending_parts
    from connector.canvas.state import purge_job

    class DeletedMidway(iris_client.IrisClient):
        calls = 0

        async def create_session(self, filename, pdf_bytes, **kw):
            DeletedMidway.calls += 1
            await purge_job(redis, await get_job(redis, "job-iris-1"))  # erased meanwhile
            return {"session_id": f"ses_{DeletedMidway.calls}", "status": "queued"}

    await put_job(redis, _job(iris_parts=_parts((1, 15, None, "pending"), (16, 30, None, "pending"))))
    job = await get_job(redis, "job-iris-1")
    assert await upload_pending_parts(redis, DeletedMidway(), job, _pdf(30)) is False
    assert DeletedMidway.calls == 1
    assert await get_job(redis, "job-iris-1") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_clicks_on_try_again_reserve_once(redis):
    from connector.canvas.iris_jobs import requeue_job
    from connector.canvas.spend_cap import get_monthly_spend_cents

    await put_job(redis, _job(status="failed", iris_parts=_parts((1, 30, "ses_a", "failed"))))
    first = await get_job(redis, "job-iris-1")
    second = await get_job(redis, "job-iris-1")
    assert (await requeue_job(redis, first)).outcome == "resend"
    assert (await requeue_job(redis, second)).outcome == "busy"
    assert await get_monthly_spend_cents(redis, COURSE) == 30 * 11


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_copy_of_a_stalled_conversion_is_aliased_not_converted_again(redis):
    from connector.canvas.state import get_file_alias, put_job_for_hash
    from connector.workers.canvas_watcher import _scan_course

    pdf = _pdf(2)
    sha = hashlib.sha256(pdf).hexdigest()
    await put_job(redis, _job(reflow_job_id="stalled-1", canvas_file_id="old", status="failed",
                              iris_stalled_at=time.time(), source_sha256=sha))
    await put_job_for_hash(redis, COURSE, sha, "stalled-1")
    fake = FakeCanvas({"f2": ("a copy.pdf", pdf)})
    with respx.mock(assert_all_called=False) as router:
        _mock_limits(router)
        upload = router.post(f"{IRIS}/v1/sessions")
        await _scan_course(redis, fake, iris_client.IrisClient(), COURSE)
    assert upload.call_count == 0
    assert await get_file_alias(redis, COURSE, "f2") == "stalled-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_figure_attempts_that_make_progress_do_not_count(redis, canvas, monkeypatch):
    import connector.workers.reflow_bridge_worker as bridge
    from connector.canvas.state import IRIS_FIGURES_ATTEMPTS_KEY, IRIS_FIGURES_RETRY_KEY

    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(5, chart_on=(2, 4)))
    real_upload = canvas.upload_course_file
    outcomes = iter([True, False])  # one figure goes up, the next fails

    async def flaky(*args, **kwargs):
        if next(outcomes, True):
            return await real_upload(*args, **kwargs)
        raise RuntimeError("Canvas hiccup")

    monkeypatch.setattr(canvas, "upload_course_file", flaky)
    figures = [{"ref": f"figures/p{n}-1.png", "page": n, "index_on_page": 1, "alt": "Chart",
                "placeholder": f"page-{n}-chart.png", "decorative": False} for n in (2, 4)]
    await put_job(redis, _job(completed_at=time.time(), iris_figures=figures))
    job = await get_job(redis, "job-iris-1")
    await bridge._resolve_iris_figures(redis, canvas, job)
    assert not await redis.exists(IRIS_FIGURES_ATTEMPTS_KEY.format(job_id="job-iris-1"))
    assert not await redis.exists(IRIS_FIGURES_RETRY_KEY.format(job_id="job-iris-1"))
    job = await get_job(redis, "job-iris-1")
    await bridge._resolve_iris_figures(redis, canvas, job)  # carries on at once
    job = await get_job(redis, "job-iris-1")
    assert sorted(job.figure_canvas_urls) == ["figures/p2-1.png", "figures/p4-1.png"]
