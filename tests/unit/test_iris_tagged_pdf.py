"""Searchable PDF from Iris: asking, joining, keeping, and saying no."""

from __future__ import annotations

import asyncio
import base64
import time

import httpx
import pytest
import respx
from connector.canvas import iris_client, iris_tagged_pdf, tagged_pdf_store
from connector.canvas.iris_client import IrisClient
from connector.canvas.iris_tagged_pdf import TaggedPdfUnavailable
from connector.canvas.iris_tagged_pdf import iris_tagged_pdf as make
from connector.canvas.state import CanvasJob, forget_job_files

from tests.tagged_pdf_fixtures import check_structure, tagged_pdf

BASE = "http://iris.test"


@pytest.fixture(autouse=True)
def _iris(monkeypatch):
    from connector.config import settings

    monkeypatch.setattr(settings, "iris_api_base_url", BASE)
    monkeypatch.setattr(settings, "iris_api_token", None)
    monkeypatch.setattr(iris_client, "_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(iris_tagged_pdf, "_BUSY_WAIT_CAP_SECONDS", 0.0)
    iris_client.clear_limits_cache()
    yield
    iris_client.clear_limits_cache()


def _job(parts: int = 1, **kw) -> CanvasJob:
    base = dict(
        reflow_job_id="job-t1", canvas_file_id="f1", canvas_file_name="Week 3 reading.pdf",
        canvas_course_id="c1", canvas_user_id="u1", status="published", created_at=time.time(),
        backend="iris", completed_at=1000.0,
        iris_parts=[
            {"index": i, "first_page": 1 + 2 * i, "last_page": 2 + 2 * i,
             "session_id": f"ses_{i}", "status": "ready_for_review", "error": None}
            for i in range(parts)
        ],
    )
    base.update(kw)
    return CanvasJob(**base)


def _limits(router, tagged: bool = True):
    return router.get(f"{BASE}/v1/limits").mock(return_value=httpx.Response(
        200, json={"max_pages": 25, "pdf": {"max_pages": 25}, "tagged_pdf": tagged},
    ))


def _answer(pdf: bytes, warnings=()):
    return httpx.Response(200, json={
        "filename": "Week 3 reading_tagged.pdf",
        "pdf": base64.b64encode(pdf).decode(),
        "report": {"warnings": [{"code": w, "page": 1} for w in warnings]},
    })


PAGE = [[("H1", "Week 3"), ("P", "Text.")]]


@pytest.mark.asyncio
async def test_one_part_is_asked_for_once_then_kept():
    pdf = tagged_pdf(PAGE)
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        route = router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(return_value=_answer(pdf, ["missing_alt"]))
        made = await make(_job())
        again = await make(_job())
    assert made.pdf == pdf and again.pdf == pdf
    assert made.filename == "Week 3 reading_tagged.pdf"
    assert made.warnings == ["missing_alt"]
    assert route.call_count == 1  # the second answer came from the store
    assert route.calls.last.request.content == b"{}"  # no form values, ever


@pytest.mark.asyncio
async def test_parts_are_tagged_one_by_one_and_joined_in_page_order():
    one, two = tagged_pdf(PAGE), tagged_pdf([[("P", "Second part.")]])
    job = _job(parts=2)
    job.iris_parts.reverse()  # stored order must not matter
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(return_value=_answer(one))
        router.post(f"{BASE}/v1/sessions/ses_1/pdf").mock(return_value=_answer(two))
        made = await make(job)
    facts = check_structure(made.pdf)
    assert facts["pages"] == 2 and facts["title"] == "Week 3 reading"


@pytest.mark.asyncio
async def test_busy_iris_is_asked_again():
    pdf = tagged_pdf(PAGE)
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        route = router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(side_effect=[
            httpx.Response(503, headers={"Retry-After": "10"},
                           json={"error": {"code": "busy", "message": "Other PDFs are being tagged."}}),
            _answer(pdf),
        ])
        made = await make(_job())
    assert made.pdf == pdf and route.call_count == 2


@pytest.mark.asyncio
async def test_a_class_asking_at_once_shares_one_run():
    pdf = tagged_pdf(PAGE)
    with respx.mock(assert_all_called=True) as router:
        _limits(router)

        async def slow(request):
            await asyncio.sleep(0.05)
            return _answer(pdf)

        route = router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(side_effect=slow)
        results = await asyncio.gather(*(make(_job()) for _ in range(5)))
    assert all(r.pdf == pdf for r in results)
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (404, "tagged_pdf_unavailable", "does not make tagged PDFs"),
        (404, "session_not_found", "no longer has session ses_0"),
        (410, "session_expired", "no longer has session ses_0"),
        (409, "no_source_pdf", "not made from one PDF"),
        (422, "encrypted", "refused session ses_0"),
        (504, "timeout", "timed out"),
    ],
)
async def test_what_iris_cannot_do_is_said_plainly(status, code, reason):
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(return_value=httpx.Response(
            status, json={"error": {"code": code, "message": "no"}},
        ))
        with pytest.raises(TaggedPdfUnavailable, match=reason):
            await make(_job())


@pytest.mark.asyncio
async def test_no_call_is_made_when_iris_cannot_tag_or_the_job_is_not_ready():
    with respx.mock(assert_all_called=False) as router:
        _limits(router, tagged=False)
        pdf_route = router.post(url__regex=rf"{BASE}/v1/sessions/.*/pdf")
        with pytest.raises(TaggedPdfUnavailable, match="does not make tagged PDFs"):
            await make(_job())
        with pytest.raises(TaggedPdfUnavailable, match="not finished"):
            await make(_job(iris_parts=[{"index": 0, "first_page": 1, "last_page": 2,
                                         "session_id": "ses_0", "status": "running"}]))
        with pytest.raises(TaggedPdfUnavailable, match="Reflow"):
            await make(_job(backend="reflow"))
    assert pdf_route.call_count == 0


@pytest.mark.asyncio
async def test_a_reconverted_document_is_made_again_and_the_old_file_goes(tmp_path):
    first, second = tagged_pdf(PAGE), tagged_pdf([[("P", "New version.")]])
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(return_value=_answer(first))
        router.post(f"{BASE}/v1/sessions/ses_9/pdf").mock(return_value=_answer(second))
        await make(_job())
        renewed = _job(iris_parts=[{"index": 0, "first_page": 1, "last_page": 2,
                                    "session_id": "ses_9", "status": "closed"}])
        made = await make(renewed)
    assert made.pdf == second
    folder = tagged_pdf_store.job_dir("job-t1")
    assert folder is not None and len(list(folder.iterdir())) == 1


@pytest.mark.asyncio
async def test_purging_a_job_deletes_its_file():
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        router.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(return_value=_answer(tagged_pdf(PAGE)))
        await make(_job())
    folder = tagged_pdf_store.job_dir("job-t1")
    assert folder is not None and folder.exists()
    forget_job_files("job-t1")
    assert not folder.exists()


def test_unsafe_job_ids_are_never_paths():
    assert tagged_pdf_store.job_dir("../etc") is None
    assert tagged_pdf_store.job_dir("") is None
    assert tagged_pdf_store.job_dir("d87ded60-5216-4b2a-8ff4-2fcb9d57226b") is not None


@pytest.mark.asyncio
@respx.mock
async def test_the_client_refuses_an_answer_that_is_not_a_pdf():
    respx.post(f"{BASE}/v1/sessions/ses_0/pdf").mock(return_value=httpx.Response(
        200, json={"filename": "x.pdf", "pdf": base64.b64encode(b"<html>").decode(), "report": {}},
    ))
    with pytest.raises(iris_client.IrisApiError, match="not a PDF"):
        await IrisClient(base_url=BASE, api_token="").tagged_pdf("ses_0")
