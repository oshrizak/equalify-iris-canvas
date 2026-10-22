"""The course opt-in switch, end to end through the real app.

"Off by default" is only safe if there's a way to turn it on that isn't a
Redis command. These drive the endpoints the review page's toggle calls.

The authorisation check is the point: a student launching the tool must not
be able to authorise processing of an entire course.
"""

from __future__ import annotations

import pytest
from connector.api.canvas_panorama import _csrf_token_for
from connector.lti.routes import SESSION_COOKIE
from connector.lti.session import SessionPayload, new_session_id, put_session

INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
COURSE = "50594"
URL = f"/canvas/review/api/optin?course_id={COURSE}"


async def _session(redis, roles: list[str]) -> str:
    sid = new_session_id()
    await put_session(
        redis,
        sid,
        SessionPayload(
            user_id="u1",
            user_name="Zach",
            user_email="pat@example.edu",
            course_id=COURSE,
            roles=roles,
        ),
    )
    return sid


def _post(client, enabled: bool, *, csrf: bool = True, url: str = URL):
    """POST as the review page does: with the session's CSRF token."""
    sid = client.cookies.get(SESSION_COOKIE) or ""
    headers = {"X-CSRF-Token": _csrf_token_for(sid)} if csrf else {}
    return client.post(url, json={"enabled": enabled}, headers=headers)


@pytest.mark.asyncio
async def test_course_starts_disabled(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    body = client.get(URL).json()

    assert body["enabled"] is False, "a course must not be scanned until asked"
    assert body["can_change"] is True
    assert body["drop_folder"] == "Iris - Convert to Accessible", "faculty need to be told the folder name"


@pytest.mark.asyncio
async def test_the_page_names_the_folder_the_course_actually_has(client, redis_client):
    """A course set up before the move to Iris keeps its Reflow-named folder."""
    from connector.canvas.state import remember_drop_folder_name

    await remember_drop_folder_name(redis_client, COURSE, "Reflow - Convert to Accessible")
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    assert client.get(URL).json()["drop_folder"] == "Reflow - Convert to Accessible"


@pytest.mark.asyncio
async def test_instructor_can_enable_and_disable(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))

    assert _post(client, True).status_code == 200
    after = client.get(URL).json()
    assert after["enabled"] is True
    assert after["enabled_by"] == "pat@example.edu", (
        "who authorised processing must be recorded, not anonymous"
    )
    assert after["enabled_at"] > 0

    assert _post(client, False).status_code == 200
    assert client.get(URL).json()["enabled"] is False


@pytest.mark.asyncio
async def test_student_cannot_change_it(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [LEARNER]))

    assert client.get(URL).json()["can_change"] is False
    assert _post(client, True).status_code == 403


@pytest.mark.asyncio
async def test_student_cannot_undo_an_instructor_decision(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    assert _post(client, True).status_code == 200

    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [LEARNER]))
    assert _post(client, False).status_code == 403
    assert client.get(URL).json()["enabled"] is True


@pytest.mark.asyncio
async def test_requires_a_session(client):
    client.cookies.clear()
    assert client.get(URL).status_code == 401
    assert client.post(URL, json={"enabled": True}).status_code == 401


@pytest.mark.asyncio
async def test_changing_it_needs_the_csrf_token(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    assert _post(client, True, csrf=False).status_code == 403
    assert client.get(URL).json()["enabled"] is False


@pytest.mark.asyncio
async def test_an_instructor_cannot_switch_on_another_course(client, redis_client):
    """A launch proves a role in one course; it says nothing about others."""
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    other = "/canvas/review/api/optin?course_id=99999"
    assert _post(client, True, url=other).status_code == 403
    assert client.get(other).status_code == 403
