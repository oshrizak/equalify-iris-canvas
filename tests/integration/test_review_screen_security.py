"""The review screens' own security: what a launch proves, and nothing more.

A launch proves a role in one course. The review pages must not render
another course's data, must not let a student publish or delete pages, must
not act on a request forged by another site, and must not run script that
arrives in a file name or a query string.
"""

from __future__ import annotations

import time

import pytest
from connector.api.canvas_panorama import _csrf_token_for
from connector.canvas.state import CanvasJob, get_job, put_job
from connector.lti.routes import SESSION_COOKIE
from connector.lti.session import SessionPayload, new_session_id, put_session

INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
COURSE = "c1"
EVIL_NAME = '<img src=x onerror="alert(1)">Week 3.pdf'


async def _session(redis, roles: list[str]) -> str:
    sid = new_session_id()
    await put_session(redis, sid, SessionPayload(
        user_id="u1", user_name="Pat <b>Faculty</b>", user_email=None,
        course_id=COURSE, roles=roles,
    ))
    return sid


async def _plant(redis, *, page_url: str | None = 'x";alert(2);//') -> None:
    await put_job(redis, CanvasJob(
        reflow_job_id="job-1", canvas_file_id="f1", canvas_file_name=EVIL_NAME,
        canvas_course_id=COURSE, canvas_user_id="u1", status="awaiting_review",
        created_at=time.time(), canvas_page_url=page_url,
    ))


@pytest.mark.asyncio
async def test_a_file_name_cannot_run_script_on_the_review_page(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    await _plant(redis_client)
    page = client.get("/canvas/review/job-1").text
    assert EVIL_NAME not in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;Week 3.pdf" in page
    # The page URL lands in a JavaScript string: its quote cannot end it.
    assert 'x";alert(2)' not in page and 'x\\";alert(2);//' in page


@pytest.mark.asyncio
async def test_pages_for_another_course_are_refused(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    reflected = '/canvas/review?course_id=";alert(1);//'
    assert client.get(reflected).status_code == 403
    assert client.get("/canvas/review/api/files?course_id=other").status_code == 403
    assert client.get("/canvas/review/api/pending?course_id=other").status_code == 403
    assert client.get("/canvas/review/dashboard?course_id=other").status_code == 403
    own = client.get(f"/canvas/review?course_id={COURSE}")
    assert own.status_code == 200
    assert "Pat &lt;b&gt;Faculty&lt;/b&gt;" in own.text
    dashboard = client.get(f"/canvas/review/dashboard?course_id={COURSE}")
    assert dashboard.status_code == 200 and f"<code>{COURSE}</code>" in dashboard.text


@pytest.mark.asyncio
async def test_approve_and_reject_need_an_instructor_and_the_csrf_token(client, redis_client):
    await _plant(redis_client, page_url=None)  # no Canvas Page to publish here

    student = await _session(redis_client, [LEARNER])
    client.cookies.set(SESSION_COOKIE, student)
    for action in ("approve", "reject"):
        resp = client.post(f"/canvas/review/job-1/{action}",
                           headers={"X-CSRF-Token": _csrf_token_for(student)})
        assert resp.status_code == 403
    assert (await get_job(redis_client, "job-1")).status == "awaiting_review"

    teacher = await _session(redis_client, [INSTRUCTOR])
    client.cookies.set(SESSION_COOKIE, teacher)
    forged = client.post("/canvas/review/job-1/approve")  # no token: another site's form
    assert forged.status_code == 403
    assert (await get_job(redis_client, "job-1")).status == "awaiting_review"

    ok = client.post("/canvas/review/job-1/approve", headers={"X-CSRF-Token": _csrf_token_for(teacher)})
    assert ok.status_code == 200, ok.text
    assert (await get_job(redis_client, "job-1")).status == "published"


@pytest.mark.asyncio
async def test_drafts_need_an_instructor_session_whatever_the_link_says(client, redis_client):
    """``?preview=1`` used to open any draft with no session at all, and the
    public overlay list hands out job ids."""
    await put_job(redis_client, CanvasJob(
        reflow_job_id="job-d", canvas_file_id="f9", canvas_file_name="Draft.pdf",
        canvas_course_id=COURSE, canvas_user_id="u1", status="awaiting_review",
        created_at=time.time(), backend="iris", completed_at=time.time(),
        iris_parts=[], iris_figures=[{"ref": "figures/p1-1.png", "page": 1, "index_on_page": 1,
                                      "alt": "x", "placeholder": "page-1-x.png", "decorative": False}],
        figure_canvas_urls={"figures/p1-1.png": "https://canvas.test/files/9/download"},
    ))
    from connector.canvas.state import put_iris_body

    await put_iris_body(redis_client, "job-d", "<h1>Unpublished</h1>")
    client.cookies.clear()
    anonymous = client.get("/canvas/panorama/alt/job-d/html?preview=1")
    assert anonymous.status_code == 403 and "sign in" in anonymous.json()["detail"]
    figure = client.get("/canvas/panorama/alt/job-d/figures/p1-1.png", follow_redirects=False)
    assert figure.status_code == 403

    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [LEARNER]))
    assert client.get("/canvas/panorama/alt/job-d/html?preview=1").status_code == 403

    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    own = client.get("/canvas/panorama/alt/job-d/html")
    assert own.status_code == 200 and "Unpublished" in own.text
