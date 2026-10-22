"""Uploading a file to Canvas survives a "slow down" from its file storage.

A figure upload in Canvas beta was refused with 429 at the storage step, and
the whole figure then waited 15 minutes for the bridge's next pass. Each try
now starts the upload over (a refused upload URL may not work twice) after
the wait Canvas asks for.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from connector.canvas import client as client_module
from connector.canvas.client import CanvasClient
from connector.canvas.errors import CanvasApiError

BASE = "https://canvas.test"
INIT = f"{BASE}/api/v1/courses/7/files"
STORE = "https://inst-fs.test/files"
FILE_JSON = {"id": 55, "url": f"{BASE}/files/55/download"}


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seen: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        seen.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    return seen


def _client() -> CanvasClient:
    return CanvasClient(base_url=BASE, api_token="t")


def _init_route(router: respx.MockRouter) -> respx.Route:
    return router.post(INIT).mock(
        return_value=httpx.Response(200, json={"upload_url": STORE, "upload_params": {"k": "v"}})
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_throttled_upload_waits_as_asked_and_starts_over(waits: list[float]) -> None:
    with respx.mock(assert_all_called=True) as router:
        init = _init_route(router)
        router.post(STORE).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(201, json=FILE_JSON),
        ])
        result = await _client().upload_course_file("7", "fig.png", b"png", content_type="image/png")
    assert result == FILE_JSON
    assert init.call_count == 2  # a fresh upload URL for the second try
    assert waits == [3.0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_retry_after_the_waits_grow_and_the_error_surfaces(waits: list[float]) -> None:
    with respx.mock(assert_all_called=True) as router:
        init = _init_route(router)
        router.post(STORE).mock(return_value=httpx.Response(503))
        with pytest.raises(CanvasApiError) as caught:
            await _client().upload_course_file("7", "fig.png", b"png")
    assert caught.value.status_code == 503
    assert init.call_count == 3
    assert waits == [2.0, 4.0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_long_retry_after_is_capped(waits: list[float]) -> None:
    with respx.mock(assert_all_called=True) as router:
        _init_route(router)
        router.post(STORE).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "600"}),
            httpx.Response(201, json=FILE_JSON),
        ])
        await _client().upload_course_file("7", "fig.png", b"png")
    assert waits == [30.0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_failures_are_not_retried(waits: list[float]) -> None:
    with respx.mock(assert_all_called=True) as router:
        init = _init_route(router)
        router.post(STORE).mock(return_value=httpx.Response(400, text="bad upload"))
        with pytest.raises(CanvasApiError) as caught:
            await _client().upload_course_file("7", "fig.png", b"png")
    assert caught.value.status_code == 400
    assert init.call_count == 1
    assert waits == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_redirect_is_still_confirmed_with_the_token(waits: list[float]) -> None:
    confirm = f"{BASE}/api/v1/files/55/create_success"
    with respx.mock(assert_all_called=True) as router:
        _init_route(router)
        router.post(STORE).mock(return_value=httpx.Response(302, headers={"Location": confirm}))
        done = router.get(confirm).mock(return_value=httpx.Response(200, json=FILE_JSON))
        result = await _client().upload_course_file("7", "fig.png", b"png")
    assert result == FILE_JSON
    assert done.calls.last.request.headers["Authorization"] == "Bearer t"
    assert waits == []
