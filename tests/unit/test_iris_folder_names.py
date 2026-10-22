"""The course folders took Iris's name without stranding Reflow-era courses.

New courses get "Iris - Convert to Accessible" and "Iris Generated Images".
A course that already has the Reflow-named folders keeps using them: its
drop folder still converts, and it does not get a second folder of either
kind.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from connector.canvas.errors import CanvasApiError
from connector.canvas.state import get_drop_folder_name
from connector.workers.canvas_watcher import _discover_drop_folder_files
from connector.workers.reflow_bridge_worker import _figure_folder

NEW_DROP = "Iris - Convert to Accessible"
OLD_DROP = "Reflow - Convert to Accessible"
PDF = {"id": "f1", "display_name": "Week 1.pdf", "content-type": "application/pdf", "size": 10}


class Canvas:
    def __init__(self, folders: list[dict[str, Any]] | None = None, *, broken: bool = False) -> None:
        self.folders = folders or []
        self.broken = broken
        self.created: list[str] = []

    async def list_course_folders(self, course_id: str) -> list[dict[str, Any]]:
        if self.broken:
            raise CanvasApiError(403, "no")
        return self.folders

    async def list_folder_files(self, folder_id: str) -> list[dict[str, Any]]:
        return [PDF]

    async def create_course_folder(self, course_id: str, name: str) -> dict[str, Any]:
        self.created.append(name)
        return {"id": "new"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_reflow_named_drop_folder_still_converts_and_gets_no_twin() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    canvas = Canvas([{"id": "old", "name": OLD_DROP}])
    found = await _discover_drop_folder_files(canvas, "c1", NEW_DROP, redis)  # type: ignore[arg-type]
    assert list(found) == ["f1"]
    assert canvas.created == []
    assert await get_drop_folder_name(redis, "c1") == OLD_DROP


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_course_without_one_gets_the_iris_name() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    canvas = Canvas([])
    assert await _discover_drop_folder_files(canvas, "c1", NEW_DROP, redis) == {}  # type: ignore[arg-type]
    assert canvas.created == [NEW_DROP]
    assert await get_drop_folder_name(redis, "c1") == NEW_DROP


@pytest.mark.unit
@pytest.mark.asyncio
async def test_folder_names_are_matched_however_faculty_type_them() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    canvas = Canvas([{"id": "x", "name": "iris - convert to ACCESSIBLE"}])
    assert list(await _discover_drop_folder_files(canvas, "c1", NEW_DROP, redis)) == ["f1"]  # type: ignore[arg-type]
    assert canvas.created == []


def _root(name: str) -> dict[str, Any]:
    return {"name": name, "full_name": f"course files/{name}"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("folders", "expected"), [
    ([], "Iris Generated Images"),
    ([_root("Reflow Generated Images")], "Reflow Generated Images"),
    ([_root("Reflow Generated Images"), _root("Iris Generated Images")], "Iris Generated Images"),
    ([{"name": "Reflow Generated Images", "full_name": "course files/Unfiled/Reflow Generated Images"}],
     "Iris Generated Images"),
])
async def test_figures_go_to_the_folder_the_course_already_uses(folders, expected) -> None:
    assert await _figure_folder(Canvas(folders), "c1") == expected  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_not_being_able_to_look_means_the_iris_folder() -> None:
    assert await _figure_folder(Canvas(broken=True), "c1") == "Iris Generated Images"  # type: ignore[arg-type]
