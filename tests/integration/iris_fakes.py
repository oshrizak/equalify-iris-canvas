"""Fakes and helpers shared by the Iris integration tests.

Iris is mocked at its HTTP contract with respx; Canvas is this small
in-memory stand-in for ``CanvasClient``. The fixtures that use them live in
``conftest.py`` (``iris_settings``, ``redis``, ``canvas``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import fitz
import httpx
from connector.canvas.errors import CanvasApiError
from connector.canvas.state import CanvasJob

IRIS = "http://iris.test"
COURSE = "c1"


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, rgb)
    return pix.tobytes("png")


def _pdf(pages: int, *, chart_on: tuple[int, ...] = ()) -> bytes:
    doc = fitz.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), f"Page {n} text.")
        if n in chart_on:
            page.insert_image(fitz.Rect(150, 300, 450, 500), stream=_png(300, 200, (n * 5 % 255, 80, 160)))
    return doc.tobytes()


class FakeCanvas:
    """Just enough of CanvasClient for the watcher and the bridge."""

    def __init__(self, files: dict[str, tuple[str, bytes]] | None = None) -> None:
        self.files = dict(files or {})
        self.uploads: list[tuple[str, str]] = []
        self.pages: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.folder_id = "fold-1"

    # discovery (watcher)
    async def list_course_folders(self, course_id: str) -> list[dict[str, Any]]:
        from connector.config import settings

        return [{"id": self.folder_id, "name": settings.canvas_drop_folder_name}]

    async def list_folder_files(self, folder_id: str) -> list[dict[str, Any]]:
        return [
            {"id": fid, "display_name": name, "content-type": "application/pdf", "size": len(data)}
            for fid, (name, data) in self.files.items()
        ]

    async def list_course_pdfs(self, course_id: str) -> list[dict[str, Any]]:
        return await self.list_folder_files(self.folder_id)

    async def create_course_folder(self, course_id: str, name: str) -> dict[str, Any]:
        return {"id": self.folder_id}

    # files
    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        self.calls.append(f"meta:{file_id}")
        if file_id not in self.files:
            raise CanvasApiError(404, "not found")
        return {"id": file_id}

    async def download_file(self, file_id: str) -> bytes:
        self.calls.append(f"download:{file_id}")
        if file_id not in self.files:
            raise CanvasApiError(404, "not found")
        return self.files[file_id][1]

    async def upload_course_file(self, course_id, filename, content, *, content_type, folder_path, **_):
        self.uploads.append((filename, content_type))
        return {"id": len(self.uploads), "url": f"https://canvas.test/files/{len(self.uploads)}/download"}

    # pages
    async def update_page(self, course_id, page_ref, title, body):
        self.calls.append(f"update:{page_ref}")
        if page_ref not in self.pages:
            raise CanvasApiError(404, "no such page")
        self.pages[page_ref].update(title=title, body=body)
        return self.pages[page_ref]

    async def create_page(self, course_id, title, body, *, published=False, **_):
        slug = title.lower().replace(" ", "-")
        page = {"page_id": 77, "url": slug, "html_url": f"https://canvas.test/courses/{course_id}/pages/{slug}",
                "title": title, "body": body, "published": published}
        self.pages[slug] = page
        return page


def _iris_doc(body: str, after: str = "") -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="utf-8"><title>x</title></head>\n'
        f"<body>\n<main>\n{body}\n</main>{after}\n</body>\n</html>\n"
    )


def _mock_limits(router, pages: int = 25) -> None:
    router.get(f"{IRIS}/v1/limits").mock(return_value=httpx.Response(200, json={
        "max_pages": pages, "pdf": {"max_pages": pages},
        "upload": {"max_files": 25, "max_request_bytes": 134217728},
        "rate_limits": {"general_per_minute": 240, "upload_per_minute": 12}, "tagged_pdf": False,
    }))


def _job(**kw: Any) -> CanvasJob:
    base: dict[str, Any] = dict(
        reflow_job_id="job-iris-1", canvas_file_id="f1", canvas_file_name="Week 3 reading.pdf",
        canvas_course_id=COURSE, canvas_user_id="u1", status="processing",
        created_at=time.time(), backend="iris", source_page_count=30,
        source_has_text_layer=True,
        iris_parts=[
            {"index": 0, "first_page": 1, "last_page": 15, "session_id": "ses_a", "status": "queued", "error": None},
            {"index": 1, "first_page": 16, "last_page": 30, "session_id": "ses_b", "status": "queued", "error": None},
        ],
    )
    base.update(kw)
    return CanvasJob(**base)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
