"""One answer to "what is this job's accessible document?".

Every surface that shows a converted document — the Canvas Page the bridge
writes, the overlay's alt formats, the WCAG checks and dial score, the
review screen, the editor's starting point — asks this module, so they
cannot disagree about the content or about how its images are resolved.

Two kinds of job exist:

* **Iris jobs** (``backend == "iris"``): the bridge stitched Iris's HTML
  into one body and stored it next to the job
  (:func:`connector.canvas.state.put_iris_body`). Nothing here calls Iris.
* **Reflow jobs**, converted before the switch: their markdown is read from
  the snapshot ``connector.tools.migrate_reflow_jobs`` takes, or, while a
  Reflow Core is still configured, fetched from it as before.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from ..config import settings
from .iris_output import apply_figures, figures_from_manifest, html_to_markdown
from .markdown_to_html import (
    IRIS_GENERATOR,
    REFLOW_GENERATOR,
    RenderedPage,
    render,
    render_html_body,
)
from .sanitize import sanitize_html
from .state import CanvasJob, get_iris_body, get_legacy_markdown

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = (".pdf", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".rtf", ".html", ".htm", ".epub")


class ContentUnavailableError(Exception):
    """The job has no document to show (yet, or any more).

    ``status_code`` is what an HTTP handler should answer: 409 while a
    conversion is still running, 410 for a legacy document whose content
    can no longer be reached.
    """

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def title_for(job: CanvasJob | None) -> str:
    """The document's title: its file name without the extension."""
    if job is None or not job.canvas_file_name:
        return "Document"
    name = str(job.canvas_file_name).strip()
    lower = name.lower()
    for ext in _SOURCE_EXTENSIONS:
        if lower.endswith(ext):
            name = name[: -len(ext)]
            break
    return name.strip() or "Document"


def legacy_reflow_configured() -> bool:
    return bool((getattr(settings, "reflow_api_base_url", None) or "").strip())


async def load_legacy_markdown(redis: Redis, job: CanvasJob) -> str:
    """A Reflow job's markdown: the migration snapshot, else Reflow itself."""
    snapshot = await get_legacy_markdown(redis, job.reflow_job_id)
    if snapshot is not None:
        return snapshot
    if not legacy_reflow_configured():
        raise ContentUnavailableError(
            "This document was converted by Equalify Reflow before the switch to "
            "Iris, and its content was not migrated. Convert it again to create "
            "an Iris version.",
            status_code=410,
        )
    from .reflow_client import ReflowClient

    reflow = ReflowClient()
    status = await reflow.get_status(job.reflow_job_id)
    if status.get("status") != "completed":
        raise ContentUnavailableError(
            f"Job not yet completed (status: {status.get('status')})", status_code=409,
        )
    markdown_url = status.get("markdown_url") or status.get("result_url")
    if not markdown_url:
        raise ContentUnavailableError("Completed job is missing markdown_url", status_code=502)
    return await reflow.fetch_markdown(markdown_url)


async def load_iris_body(redis: Redis, job: CanvasJob) -> str:
    body = await get_iris_body(redis, job.reflow_job_id)
    if body is None:
        raise ContentUnavailableError(
            "Iris is still converting this document.", status_code=409,
        )
    return body


async def load_rendered(
    redis: Redis,
    job: CanvasJob,
    *,
    original_pdf_url: str | None = None,
) -> RenderedPage:
    """The canonical accessible document for ``job``, sanitized.

    Figures resolve to the Canvas files the bridge uploaded; an Iris figure
    that could not be lifted from the PDF is shown as its description (see
    :func:`connector.canvas.iris_output.apply_figures`).
    """
    title = title_for(job)
    if job.is_iris:
        body = await load_iris_body(redis, job)
        body = apply_figures(
            body,
            job.figure_canvas_urls,
            manifest=figures_from_manifest(job.iris_figures),
        )
        rendered = render_html_body(
            body,
            title=title,
            lang=job.content_lang,
            original_pdf_url=original_pdf_url,
            generator=IRIS_GENERATOR,
        )
    else:
        markdown = await load_legacy_markdown(redis, job)
        for ref, fig_url in (job.figure_canvas_urls or {}).items():
            markdown = markdown.replace(ref, fig_url)
        rendered = render(
            markdown,
            title=title,
            original_pdf_url=original_pdf_url,
            generator=REFLOW_GENERATOR,
        )
    try:
        rendered.html = sanitize_html(rendered.html)
    except RuntimeError:
        logger.exception("sanitize_html failed on canonical render for %s", job.reflow_job_id)
        raise
    return rendered


async def load_markdown(redis: Redis, job: CanvasJob) -> str:
    """The "Markdown" alt format.

    For a Reflow job that is Reflow's own markdown, as before. For an Iris
    job it is derived from the HTML, which stays the canonical form.
    """
    if not job.is_iris:
        return await load_legacy_markdown(redis, job)
    body = await load_iris_body(redis, job)
    body = apply_figures(
        body, job.figure_canvas_urls, manifest=figures_from_manifest(job.iris_figures),
    )
    return html_to_markdown(sanitize_html(body))
