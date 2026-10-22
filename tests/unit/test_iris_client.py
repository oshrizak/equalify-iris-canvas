"""The HTTP contract with Equalify Iris.

Every Reflow-era regression in the pilot was a wire-contract mistake (a
route that did not exist, a field under a different name) that unit tests
stubbing the handler boundary could not see. These pin the Iris contract
the connector depends on: field names, auth header, error shape, and which
failures are worth retrying.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from connector.canvas import iris_client
from connector.canvas.iris_client import (
    IrisApiError,
    IrisClient,
    IrisLimits,
    clear_limits_cache,
    error_from_response,
    normalize_base_url,
)

BASE = "http://iris.test"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(iris_client, "_RETRY_BASE_DELAY", 0.0)
    clear_limits_cache()
    yield
    clear_limits_cache()


def _client(token: str = "s3cret") -> IrisClient:
    return IrisClient(base_url=BASE, api_token=token)


def test_base_url_with_or_without_v1_is_the_same():
    assert normalize_base_url("http://iris:8080/v1/") == "http://iris:8080"
    assert normalize_base_url("http://iris:8080/") == "http://iris:8080"
    assert normalize_base_url("") == "http://localhost:8080"


@pytest.mark.asyncio
@respx.mock
async def test_create_session_sends_one_images_part_and_the_bearer_token():
    route = respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(201, json={
            "session_id": "ses_01ABC", "status": "queued", "image_count": 3,
            "created_at": "2026-10-21T10:00:00.000Z",
        })
    )
    out = await _client().create_session("Week 3.pdf", b"%PDF-1.7 fake")
    assert out["session_id"] == "ses_01ABC"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer s3cret"
    body = request.content
    # Iris reads exactly one part name, "images" (routes/sessions.ts).
    assert b'name="images"; filename="Week 3.pdf"' in body
    assert b"application/pdf" in body


@pytest.mark.asyncio
@respx.mock
async def test_no_token_sends_no_authorization_header():
    route = respx.get(f"{BASE}/v1/sessions/ses_1").mock(
        return_value=httpx.Response(200, json={"session_id": "ses_1", "status": "running"})
    )
    await IrisClient(base_url=BASE, api_token="").get_session("ses_1")
    assert "authorization" not in route.calls.last.request.headers


def test_error_shape_is_carried_through():
    resp = httpx.Response(
        429,
        headers={"Retry-After": "41"},
        json={"error": {"code": "rate_limited", "message": "Too many requests", "details": {"limit": 12}}},
        request=httpx.Request("POST", f"{BASE}/v1/sessions"),
    )
    err = error_from_response(resp, "upload")
    assert err.status_code == 429
    assert err.code == "rate_limited"
    assert err.retry_after == 41.0
    assert err.details == {"limit": 12}
    assert err.is_rate_limited and err.is_transient
    assert "Too many requests" in err.message


def test_a_refused_page_is_not_transient():
    resp = httpx.Response(
        400,
        json={"error": {"code": "invalid_request", "message": "Page 7 of a.pdf is 5.1 MB"}},
        request=httpx.Request("POST", f"{BASE}/v1/sessions"),
    )
    err = error_from_response(resp, "upload")
    assert not err.is_transient
    assert "Page 7" in err.message


@pytest.mark.asyncio
@respx.mock
async def test_gets_retry_a_gateway_hiccup():
    route = respx.get(f"{BASE}/v1/sessions/ses_1").mock(side_effect=[
        httpx.Response(503, json={"error": {"code": "server_error", "message": "restarting"}}),
        httpx.Response(200, json={"session_id": "ses_1", "status": "ready_for_review"}),
    ])
    out = await _client().get_session("ses_1")
    assert out["status"] == "ready_for_review"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_gets_do_not_retry_a_missing_session():
    route = respx.get(f"{BASE}/v1/sessions/ses_gone").mock(
        return_value=httpx.Response(404, json={"error": {"code": "session_not_found", "message": "No such session"}})
    )
    with pytest.raises(IrisApiError) as info:
        await _client().get_session("ses_gone")
    assert info.value.status_code == 404
    assert info.value.code == "session_not_found"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_uploads_are_never_retried():
    route = respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(503, json={"error": {"code": "busy", "message": "busy"}})
    )
    with pytest.raises(IrisApiError):
        await _client().create_session("a.pdf", b"x")
    # A POST that reached Iris may already be converting; retrying blind
    # would convert (and bill) the document twice.
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_unreachable_iris_is_a_transient_error():
    respx.get(f"{BASE}/v1/sessions/ses_1").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(IrisApiError) as info:
        await _client().get_session("ses_1")
    assert info.value.status_code is None
    assert info.value.is_transient


@pytest.mark.asyncio
@respx.mock
async def test_is_open_reads_me_without_credentials():
    route = respx.get(f"{BASE}/v1/me").mock(
        return_value=httpx.Response(401, json={"error": {"code": "unauthorized", "message": "x"}})
    )
    assert await _client().is_open() is False
    assert "authorization" not in route.calls.last.request.headers
    route.mock(return_value=httpx.Response(200, json={"defaults": {"max_review_iterations": 3}}))
    assert await _client().is_open() is True


@pytest.mark.asyncio
@respx.mock
async def test_limits_are_parsed_and_cached():
    route = respx.get(f"{BASE}/v1/limits").mock(return_value=httpx.Response(200, json={
        "max_pages": 25, "pdf": {"max_pages": 25},
        "upload": {"max_files": 25, "max_request_bytes": 134217728},
        "rate_limits": {"general_per_minute": 240, "upload_per_minute": 12},
        "tagged_pdf": True,
    }))
    client = _client()
    limits = await client.limits()
    assert limits == IrisLimits(
        max_pages=25, max_request_bytes=134217728, upload_per_minute=12,
        tagged_pdf=True, raw=limits.raw,
    )
    await client.limits()
    assert route.call_count == 1


def test_limits_fall_back_to_25_pages_on_a_strange_payload():
    assert IrisLimits.from_payload({}).max_pages == 25
    assert IrisLimits.from_payload({"pdf": {"max_pages": "lots"}}).max_pages == 25
    assert IrisLimits.from_payload({"rate_limits": None}).upload_per_minute is None
