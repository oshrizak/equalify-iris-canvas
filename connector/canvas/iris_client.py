"""Async client for the Equalify Iris HTTP API.

Iris (https://github.com/EqualifyEverything/equalify-iris) turns the pages of
a PDF into one accessible HTML document. The connector is a client of its
``/v1`` API and imports nothing from it; the seam is HTTP, exactly as it was
with Reflow Core.

Endpoints consumed:

  * ``GET  /v1/health``                  liveness + build version
  * ``GET  /v1/limits``                  page cap, upload sizes, rate limits
  * ``GET  /v1/me``                      200 = usable with our credentials,
                                         401 = gated and our token is wrong
  * ``POST /v1/sessions``                multipart, one ``images`` part per file
  * ``GET  /v1/sessions/{id}``           status: queued | running |
                                         ready_for_review | closed | failed
  * ``GET  /v1/sessions/{id}/output``    the HTML (409 until ready)
  * ``GET  /v1/sessions/{id}/diagnostics`` token counts and timings
  * ``POST /v1/sessions/{id}/close``     a human accepted the output

Auth is Iris's optional shared secret (``server.api_token``), sent as
``Authorization: Bearer``. Iris has no per-user identity: every session
belongs to the deployment, and on an open deployment ``GET /v1/sessions``
lists all of them. That is why the connector should always run against a
gated Iris (see ``connector.main`` and ``scripts/preflight.py``).

Every Iris error has one shape, ``{"error": {"code", "message", "details"}}``,
which :class:`IrisApiError` carries through so callers can branch on
``code`` (``rate_limited``, ``session_not_found``, ``session_expired``,
``invalid_state``, ``invalid_request``, ...) instead of on prose.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Statuses Iris reports for a session (docs/API.md "Poll status").
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_READY = "ready_for_review"
STATUS_CLOSED = "closed"
STATUS_FAILED = "failed"
IN_FLIGHT_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})
FINISHED_STATUSES = frozenset({STATUS_READY, STATUS_CLOSED})

# Iris's documented page cap per session (util/pdf.ts MAX_PDF_PAGES). Used
# only when GET /v1/limits cannot be read.
DEFAULT_MAX_PAGES = 25

# GETs are idempotent, so a transient failure is retried a couple of times.
# POSTs are not: creating a session twice would convert (and bill) twice.
_GET_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 10.0
_RETRYABLE_STATUS = frozenset({502, 503, 504})
# Transport failures that happen before a request is fully sent.
_NOT_SENT = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
    httpx.WriteError, httpx.WriteTimeout,
)

# Limits change only when the Iris deployment changes its model or config,
# so one read every few minutes is plenty.
_LIMITS_TTL_SECONDS = 600.0
_limits_cache: dict[str, tuple[float, IrisLimits]] = {}


class IrisApiError(Exception):
    """An Iris call that did not succeed.

    ``status_code`` is the HTTP status (``None`` for a transport failure),
    ``code`` Iris's machine-readable error code when it sent one, and
    ``retry_after`` the seconds Iris asked us to wait (429/503).
    ``may_have_reached`` is set on a failed POST whose request may have
    arrived in full anyway (the answer timed out or was cut off, or a
    gateway gave up waiting): Iris may have created the session regardless.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
        may_have_reached: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.details = details or {}
        self.may_have_reached = may_have_reached

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429

    @property
    def is_transient(self) -> bool:
        """Worth retrying later: Iris unreachable, busy, or throttling us."""
        return self.status_code is None or self.status_code in (429, 502, 503, 504)

    def __str__(self) -> str:  # pragma: no cover - trivial
        bits = [self.message]
        if self.status_code is not None:
            bits.append(f"(HTTP {self.status_code}{', ' + self.code if self.code else ''})")
        return " ".join(bits)


@dataclass(frozen=True)
class IrisLimits:
    """What one upload to this Iris deployment may be (``GET /v1/limits``)."""

    max_pages: int = DEFAULT_MAX_PAGES
    max_request_bytes: int | None = None
    upload_per_minute: int | None = None
    tagged_pdf: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IrisLimits:
        pdf = payload.get("pdf") or {}
        upload = payload.get("upload") or {}
        rate = payload.get("rate_limits") or {}
        pages = pdf.get("max_pages") or payload.get("max_pages") or DEFAULT_MAX_PAGES
        try:
            pages = max(1, int(pages))
        except (TypeError, ValueError):
            pages = DEFAULT_MAX_PAGES
        return cls(
            max_pages=pages,
            max_request_bytes=_int_or_none(upload.get("max_request_bytes")),
            upload_per_minute=_int_or_none(rate.get("upload_per_minute")) if rate else None,
            tagged_pdf=bool(payload.get("tagged_pdf")),
            raw=payload,
        )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_base_url(raw: str | None) -> str:
    """``http://iris:8080/v1/`` and ``http://iris:8080`` mean the same thing."""
    base = (raw or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base or "http://localhost:8080"


def _default_token() -> str:
    raw = getattr(settings, "iris_api_token", None)
    if raw is None:
        return ""
    if hasattr(raw, "get_secret_value"):
        raw = raw.get_secret_value()
    return str(raw or "").strip()


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def error_from_response(resp: httpx.Response, action: str) -> IrisApiError:
    """Turn a non-2xx Iris response into an :class:`IrisApiError`.

    Iris's own message is kept verbatim: it is written for the person who has
    to act on it (it names the page that was too large, the cap that was hit,
    how long to wait), and paraphrasing it would only lose that.
    """
    code: str | None = None
    message = ""
    details: dict[str, Any] = {}
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = err.get("code")
            message = str(err.get("message") or "")
            details = err.get("details") or {}
    except ValueError:
        pass
    if not message:
        message = (resp.text or "").strip()[:300] or resp.reason_phrase or "error"
    return IrisApiError(
        f"Iris {action} failed: {message}",
        status_code=resp.status_code,
        code=code,
        retry_after=_retry_after_seconds(resp),
        details=details,
    )


class IrisClient:
    """Thin async wrapper over the Iris ``/v1`` API.

    One ``httpx.AsyncClient`` per call keeps the client safe to share across
    the watcher, the bridge and request handlers without lifecycle plumbing;
    the traffic is a handful of requests per document, so pooling buys
    nothing measurable.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        *,
        timeout: float | None = None,
        upload_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = normalize_base_url(
            base_url if base_url is not None else getattr(settings, "iris_api_base_url", None)
        )
        token = api_token if api_token is not None else _default_token()
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = float(
            timeout if timeout is not None
            else getattr(settings, "iris_request_timeout_seconds", 30.0)
        )
        self._upload_timeout = float(
            upload_timeout if upload_timeout is not None
            else getattr(settings, "iris_upload_timeout_seconds", 300.0)
        )
        self._transport = transport

    # ---- plumbing -----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/v1{path}"

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def _get(self, path: str, action: str, *, auth: bool = True) -> httpx.Response:
        """GET with a short retry on transport errors and 502/503/504."""
        url = self._url(path)
        headers = self._headers if auth else {}
        delay = _RETRY_BASE_DELAY
        last: IrisApiError | None = None
        for attempt in range(1, _GET_ATTEMPTS + 1):
            try:
                async with self._client(self._timeout) as client:
                    resp = await client.get(url, headers=headers)
            except httpx.TransportError as exc:
                last = IrisApiError(f"Iris {action} failed: {exc.__class__.__name__}: {exc}")
            else:
                if resp.status_code < 400:
                    return resp
                last = error_from_response(resp, action)
                if resp.status_code not in _RETRYABLE_STATUS:
                    raise last
            if attempt < _GET_ATTEMPTS:
                await asyncio.sleep(min(delay, _RETRY_MAX_DELAY))
                delay *= 2
        assert last is not None
        raise last

    async def _post(
        self, path: str, action: str, *, timeout: float | None = None, **kwargs: Any,
    ) -> httpx.Response:
        try:
            async with self._client(timeout or self._timeout) as client:
                resp = await client.post(self._url(path), headers=self._headers, **kwargs)
        except _NOT_SENT as exc:
            # The request never got through: nothing was created.
            raise IrisApiError(
                f"Iris {action} failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            # Sent in full, then the answer timed out or the connection broke.
            raise IrisApiError(
                f"Iris {action} failed: {exc.__class__.__name__}: {exc}",
                may_have_reached=True,
            ) from exc
        if resp.status_code >= 400:
            error = error_from_response(resp, action)
            # An Iris error after staging, or a gateway that stopped waiting
            # for an answer. (A 502 is a gateway that could not reach Iris
            # at all, which is an outage, not an uncertain upload.)
            error.may_have_reached = resp.status_code in (500, 504)
            raise error
        return resp

    # ---- deployment ---------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """``{"status": "ok", "service": "equalify-iris", "version": ...}``."""
        return (await self._get("/health", "health check", auth=False)).json()

    async def me(self) -> dict[str, Any]:
        """Iris's own "is this caller allowed" probe. Raises 401 when gated
        and our token is missing or wrong."""
        return (await self._get("/me", "credential check")).json()

    async def is_open(self) -> bool:
        """True when Iris answers ``/v1/me`` with NO credentials at all.

        An open Iris lets anyone who can reach it list and read every session
        — i.e. every course document the connector ever converted.
        """
        try:
            await self._get("/me", "open-deployment probe", auth=False)
        except IrisApiError as exc:
            if exc.status_code == 401:
                return False
            raise
        return True

    async def limits(self, *, use_cache: bool = True) -> IrisLimits:
        """The deployment's page cap and upload limits (cached ~10 minutes)."""
        now = time.monotonic()
        cached = _limits_cache.get(self.base_url)
        if use_cache and cached and now - cached[0] < _LIMITS_TTL_SECONDS:
            return cached[1]
        resp = await self._get("/limits", "limits lookup", auth=False)
        limits = IrisLimits.from_payload(resp.json())
        _limits_cache[self.base_url] = (now, limits)
        return limits

    # ---- sessions -----------------------------------------------------

    async def create_session(
        self, filename: str, pdf_bytes: bytes, *, content_type: str = "application/pdf",
    ) -> dict[str, Any]:
        """Upload one PDF as a new session. Returns ``{session_id, status, ...}``.

        Not retried here: a POST that reached Iris has already been paid for.
        The caller decides, using ``IrisApiError.may_have_reached``: an upload
        that may have arrived is sent again only a limited number of times,
        since each attempt can leave an unused session behind in Iris.
        """
        files = {"images": (filename, pdf_bytes, content_type)}
        resp = await self._post(
            "/sessions", "upload", timeout=self._upload_timeout, files=files,
        )
        payload = resp.json()
        if not payload.get("session_id"):
            raise IrisApiError(
                "Iris upload returned no session_id", status_code=resp.status_code,
            )
        return payload

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return (await self._get(f"/sessions/{session_id}", f"status check ({session_id})")).json()

    async def get_output(self, session_id: str) -> str:
        resp = await self._get(f"/sessions/{session_id}/output", f"output fetch ({session_id})")
        return resp.text

    async def get_diagnostics(self, session_id: str) -> dict[str, Any]:
        resp = await self._get(
            f"/sessions/{session_id}/diagnostics", f"diagnostics fetch ({session_id})",
        )
        return resp.json()

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """Tell Iris a human accepted this session's output.

        Iris captures the accepted pages as regression fixtures and deletes
        the session's temp files. 409 means it was not ``ready_for_review``
        (already closed, or re-running) and is safe to ignore.
        """
        return (await self._post(f"/sessions/{session_id}/close", f"close ({session_id})")).json()


def clear_limits_cache() -> None:
    """Forget cached ``/v1/limits`` answers (tests, config reloads)."""
    _limits_cache.clear()
