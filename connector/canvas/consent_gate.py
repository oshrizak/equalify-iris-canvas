"""Whether a course's documents may be sent for conversion in the background.

Background work (the watcher's discovery, an operator's bulk retry) acts for
the course owner: the instructor whose Canvas token the watcher reads with.
Their agreement to the current disclaimer is what authorizes sending the
course's documents to Iris, so a course whose owner has not accepted the
current version waits until they do.
"""

from __future__ import annotations

from redis.asyncio import Redis

from .state import get_consent, needs_consent


async def course_owner_consents(redis: Redis, course_id: str) -> tuple[bool, str | None]:
    """``(allowed, owner_id)`` for background conversion in ``course_id``.

    A course with no owner (a single-tenant deployment reading with an
    operator token from ``CANVAS_WATCHED_COURSES``) is the operator's choice
    and is allowed.
    """
    from ..lti.platform_store import get_course_owner

    owner = await get_course_owner(redis, course_id)
    if not owner:
        return True, None
    return not needs_consent(await get_consent(redis, owner)), owner
