"""Every button in the Alternative Formats Menu, through the real app.

Iris is mocked at its HTTP contract and Canvas by the in-memory fake. What
each format must do:

  * Searchable PDF: Iris's tagged copy of the original for an unedited Iris
    document; the connector's own PDF when faculty edited it (Iris cannot
    see edits) or Iris cannot tag it.
  * Plain text and Markdown: downloads (an attachment), not a page that
    replaces Canvas.
  * ePub: a book with the document in it.
  * HTML, reader, ePub: one H1, the document's own; the page title is it.
  * Audio and Translate: "under development" until Amazon Polly or an
    Anthropic key is set up, in the menu payload and at their URLs.
  * Approving makes the tagged PDF in the background.
  * The menu's payload names the file and says, in words, what the checks
    found on the accessible version and on the original.
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
import zipfile

import httpx
import pytest
import respx
from connector.api.canvas_panorama import SCORE_CACHE_KEY
from connector.canvas.state import put_edited_html, put_iris_body, put_job

from tests.integration.iris_fakes import IRIS, _job, _pdf, _run
from tests.tagged_pdf_fixtures import tagged_pdf

pytestmark = pytest.mark.usefixtures("iris_settings")

BODY = (
    "<p><em>This page is an accessible version.</em></p>"
    "<h1>Week 3: Kinematics</h1><p>Distance is speed times time.</p>"
    "<h2>Practice</h2><ul><li>One</li><li>Two</li></ul>"
)


def _plant(redis_client, course_id, *, edited=False, name="Week 3 reading.pdf", status="published", audit=None):
    async def go():
        await put_job(redis_client, _job(
            canvas_course_id=course_id, status=status, completed_at=time.time(),
            content_lang="en", canvas_file_name=name, source_page_count=2,
            iris_parts=[{"index": 0, "first_page": 1, "last_page": 2, "session_id": "ses_z",
                         "status": "ready_for_review", "error": None}],
            iris_figures=[],
            **({"verapdf_score": 93, "verapdf_is_compliant": False, "verapdf_violations": audit}
               if audit is not None else {}),
        ))
        await put_iris_body(redis_client, "job-iris-1", BODY)
        if edited:
            await put_edited_html(redis_client, "job-iris-1", "<h1>Week 3: Kinematics (edited)</h1><p>Fixed.</p>")
    _run(go())


def _limits(router, tagged=True):
    router.get(f"{IRIS}/v1/limits").mock(return_value=httpx.Response(
        200, json={"max_pages": 25, "pdf": {"max_pages": 25}, "tagged_pdf": tagged},
    ))


def _tagged_answer():
    pdf = tagged_pdf([[("H1", "Week 3: Kinematics"), ("P", "Distance is speed times time.")]])
    return pdf, httpx.Response(200, json={
        "filename": "Week 3 reading_tagged.pdf", "pdf": base64.b64encode(pdf).decode(),
        "report": {"warnings": []},
    })


# ---- Searchable PDF --------------------------------------------------------

@pytest.mark.integration
def test_searchable_pdf_is_iris_tagged_copy_of_the_original(client, instructor_session, redis_client):
    _plant(redis_client, instructor_session["course_id"])
    pdf, answer = _tagged_answer()
    with respx.mock(assert_all_called=True) as router:
        _limits(router)
        route = router.post(f"{IRIS}/v1/sessions/ses_z/pdf").mock(return_value=answer)
        resp = client.get("/canvas/panorama/alt/job-iris-1/ocr")
        again = client.get("/canvas/panorama/alt/job-iris-1/ocr")
    assert resp.status_code == 200, resp.text
    assert resp.content == pdf and again.content == pdf
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["x-accessible-pdf"] == "iris-tagged"
    assert 'filename="Week 3 reading_tagged.pdf"' in resp.headers["content-disposition"]
    assert route.call_count == 1  # kept, not asked for again


@pytest.mark.integration
@pytest.mark.timeout(60)
def test_an_edited_document_gets_a_pdf_of_the_edits_without_asking_iris(
    client, instructor_session, redis_client,
):
    pytest.importorskip("weasyprint")
    _plant(redis_client, instructor_session["course_id"], edited=True)
    with respx.mock(assert_all_called=False) as router:
        pdf_route = router.post(url__regex=rf"{IRIS}/v1/sessions/.*/pdf")
        resp = client.get("/canvas/panorama/alt/job-iris-1/ocr")
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-accessible-pdf"] == "connector-edited"
    assert resp.content.startswith(b"%PDF-")
    assert pdf_route.call_count == 0


@pytest.mark.integration
@pytest.mark.timeout(60)
def test_when_iris_cannot_tag_the_connector_renders_its_own(client, instructor_session, redis_client, canvas):
    pytest.importorskip("weasyprint")
    _plant(redis_client, instructor_session["course_id"])
    canvas.files["f1"] = ("Week 3 reading.pdf", _pdf(2))  # born-digital
    with respx.mock(assert_all_called=False) as router:
        _limits(router, tagged=False)
        resp = client.get("/canvas/panorama/alt/job-iris-1/ocr")
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-accessible-pdf"] == "connector-html"
    assert "Week 3 reading.tagged.pdf" in resp.headers["content-disposition"]


# ---- downloads, not navigations -------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize(("fmt", "name"), [("txt", "Week 3 reading.txt"), ("markdown", "Week 3 reading.md")])
def test_text_formats_are_downloads(client, instructor_session, redis_client, fmt, name):
    _plant(redis_client, instructor_session["course_id"])
    resp = client.get(f"/canvas/panorama/alt/job-iris-1/{fmt}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert f'filename="{name}"' in resp.headers["content-disposition"]
    assert "Distance is speed times time." in resp.text


@pytest.mark.integration
def test_a_name_beyond_latin1_still_downloads(client, instructor_session, redis_client):
    _plant(redis_client, instructor_session["course_id"], name="Lectura 3 — análisis 中文.pdf")
    resp = client.get("/canvas/panorama/alt/job-iris-1/txt")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''Lectura%203%20%E2%80%94%20an%C3%A1lisis%20%E4%B8%AD%E6%96%87.txt" in disposition
    assert disposition.encode("latin-1")  # the header itself stays Latin-1


# ---- ePub and the single H1 -------------------------------------------------

@pytest.mark.integration
def test_epub_holds_the_document(client, instructor_session, redis_client):
    _plant(redis_client, instructor_session["course_id"])
    resp = client.get("/canvas/panorama/alt/job-iris-1/epub")
    assert resp.status_code == 200
    assert 'filename="Week 3 reading.epub"' in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as book:
        chapter = book.read("EPUB/content.xhtml").decode("utf-8")
        opf = book.read("EPUB/content.opf").decode("utf-8")
    assert "Distance is speed times time." in chapter
    assert len(re.findall(r"<h1[\s>]", chapter)) == 1
    assert "Week 3: Kinematics" in opf  # the book's title is the document's


@pytest.mark.integration
@pytest.mark.parametrize("fmt", ["html", "html-math", "immersive"])
def test_one_h1_and_the_document_title(client, instructor_session, redis_client, fmt):
    _plant(redis_client, instructor_session["course_id"])
    resp = client.get(f"/canvas/panorama/alt/job-iris-1/{fmt}")
    assert resp.status_code == 200
    assert len(re.findall(r"<h1[\s>]", resp.text)) == 1
    assert re.search(r"<title>Week 3: Kinematics( — Reader)?</title>", resp.text)
    assert '<html lang="en">' in resp.text


# ---- Audio and Translate ----------------------------------------------------

@pytest.mark.integration
def test_audio_and_translate_are_under_development_without_their_services(
    client, instructor_session, redis_client, monkeypatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    _plant(redis_client, instructor_session["course_id"])
    payload = client.get("/canvas/panorama/score_by_job?job_ids=job-iris-1").json()["scores"]["job-iris-1"]
    assert payload["under_development"] == ["audio", "translate"]
    assert "audio" not in payload["available_formats"] and "translate" not in payload["available_formats"]
    assert "ocr" in payload["available_formats"] and "epub" in payload["available_formats"]
    for fmt in ("audio", "translate:es"):
        resp = client.get(f"/canvas/panorama/alt/job-iris-1/{fmt}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "This format is under development."


@pytest.mark.integration
def test_translate_switches_on_with_its_key(client, instructor_session, redis_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    _plant(redis_client, instructor_session["course_id"])
    payload = client.get("/canvas/panorama/score_by_job?job_ids=job-iris-1").json()["scores"]["job-iris-1"]
    assert "translate" in payload["available_formats"]
    assert "translate" not in payload["under_development"]


# ---- approval makes the tagged PDF ------------------------------------------

def _approve(client, instructor_session, csrf_header, trusted_origin_headers):
    return client.post(
        "/canvas/panorama/approve/job-iris-1",
        cookies={"reflow_lti_session": instructor_session["session_id"]},
        headers={**csrf_header, **trusted_origin_headers, "Content-Type": "application/json"},
        json={"comment": None, "waivers": [], "checklist": {}},
    )


@pytest.mark.integration
@pytest.mark.timeout(15)
def test_approving_makes_the_tagged_pdf_in_the_background(
    client, instructor_session, csrf_header, trusted_origin_headers, redis_client,
):
    _plant(redis_client, instructor_session["course_id"], status="awaiting_review")
    _pdf_bytes, answer = _tagged_answer()
    with respx.mock(assert_all_called=False) as router:
        _limits(router)
        route = router.post(f"{IRIS}/v1/sessions/ses_z/pdf").mock(return_value=answer)
        resp = _approve(client, instructor_session, csrf_header, trusted_origin_headers)
        assert resp.status_code == 200, resp.text
        for _ in range(100):
            if route.called:
                break
            time.sleep(0.05)
        assert route.call_count == 1


@pytest.mark.integration
@pytest.mark.timeout(15)
def test_approving_an_edited_document_does_not_ask_iris_to_tag_it(
    client, instructor_session, csrf_header, trusted_origin_headers, redis_client,
):
    _plant(redis_client, instructor_session["course_id"], edited=True, status="awaiting_review")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=rf"{IRIS}/v1/sessions/.*/pdf")
        resp = _approve(client, instructor_session, csrf_header, trusted_origin_headers)
        assert resp.status_code == 200, resp.text
        time.sleep(0.3)
        assert route.call_count == 0


# ---- Accessible HTML draws math -----------------------------------------------

@pytest.mark.integration
def test_accessible_html_loads_mathjax_only_for_latex(client, instructor_session, redis_client):
    _plant(redis_client, instructor_session["course_id"])

    async def body(html):
        await put_iris_body(redis_client, "job-iris-1", html)

    _run(body('<h1>T</h1><p>Area <math alttext="\\pi r^2"><mi>π</mi></math></p>'))
    mathml = client.get("/canvas/panorama/alt/job-iris-1/html").text
    assert "<math" in mathml and "mathjax" not in mathml.lower()

    _run(body("<h1>T</h1><p>Area $$\\pi r^2$$</p>"))
    latex = client.get("/canvas/panorama/alt/job-iris-1/html").text
    assert "mathjax" in latex.lower()


# ---- What the menu says about the two checks --------------------------------

# veraPDF on a PDF with no tags: few rules fail, each of them many times, so
# the share of rules passed (93) flattered it.
UNTAGGED_AUDIT = [
    {"rule_id": "7.1-11", "clause": "7.1", "occurrence_count": 1,
     "description": "The logical structure shall be described by a structure hierarchy"},
    {"rule_id": "7.1-3", "clause": "7.1", "occurrence_count": 70,
     "description": "Content shall be marked as Artifact or tagged as real content"},
]


def _menu_payload(client):
    return client.get("/canvas/panorama/score_by_job?job_ids=job-iris-1").json()["scores"]["job-iris-1"]


@pytest.mark.integration
def test_the_menu_names_the_file_and_says_what_each_check_found(client, instructor_session, redis_client):
    _plant(redis_client, instructor_session["course_id"], audit=UNTAGGED_AUDIT)
    payload = _menu_payload(client)
    assert payload["file_name"] == "Week 3 reading.pdf"
    original = payload["original"]
    assert (original["verdict"], original["headline"]) == ("untagged", "Not tagged")
    assert [p["label"] for p in original["problems"]] == [
        "The PDF has no tags, so screen readers get no headings, lists or reading order",
        "70 pieces of content aren’t tagged",
    ]
    assert payload["status"] == "scored"
    assert set(payload["checks"]) == {"errors", "warnings", "confirm", "items"}
    # The number stays for API callers; the menu no longer shows it.
    assert payload["source_score"] == 93


@pytest.mark.integration
def test_a_file_without_an_audit_has_no_original_summary(client, instructor_session, redis_client):
    _plant(redis_client, instructor_session["course_id"], name="Lab safety.docx")
    payload = _menu_payload(client)
    assert payload["file_name"] == "Lab safety.docx"
    assert "original" not in payload


@pytest.mark.integration
def test_every_surface_says_when_faculty_edited_it(client, instructor_session, redis_client):
    course = instructor_session["course_id"]
    _plant(redis_client, course)
    assert _menu_payload(client)["edited"] is False
    _plant(redis_client, course, edited=True)
    by_file = client.get(f"/canvas/panorama/score?course_id={course}&file_ids=f1").json()["scores"]["f1"]
    by_name = client.get(f"/canvas/panorama/scored_files?course_id={course}").json()["by_filename"]
    assert by_file["edited"] is True
    assert by_name["Week 3 reading.pdf"]["edited"] is True
    assert _menu_payload(client)["edited"] is True


@pytest.mark.integration
def test_a_score_cached_before_the_checks_were_listed_is_worked_out_again(
    client, instructor_session, redis_client,
):
    _plant(redis_client, instructor_session["course_id"])
    stale = {"status": "scored", "score": 41, "severity": "amber", "available_formats": ["html"]}
    _run(redis_client.set(SCORE_CACHE_KEY.format(job_id="job-iris-1"), json.dumps(stale)))
    payload = _menu_payload(client)
    assert "checks" in payload
    assert payload["score"] != 41
