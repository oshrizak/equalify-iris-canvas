"""Connector entrypoint.

Boots a FastAPI app that exposes:

  * ``/health`` — liveness probe
  * LTI 1.3 handshake endpoints (``/lti/*``)
  * Canvas API routers (``/canvas/consent``, ``/canvas/oauth``,
    ``/canvas/panorama``, ``/canvas/review``)

And starts two background tasks under the lifespan:

  * ``canvas_watcher`` — polls Canvas courses, discovers files,
    submits them to Equalify Iris.
  * ``reflow_bridge_worker`` — polls Iris for completion and
    materialises results into Canvas Pages.

Both workers are no-ops when ``LTI_ENABLED=false`` (they shut down
cleanly on the shutdown event).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from connector.api import canvas_consent, canvas_oauth, canvas_panorama, canvas_review
from connector.config import settings
from connector.dependencies import _get_redis_pool
from connector.logging_setup import configure_logging
from connector.lti import router as lti_router
from connector.workers.canvas_watcher import start_canvas_watcher
from connector.workers.reflow_bridge_worker import start_bridge

logger = logging.getLogger("connector")

_PANORAMA_JS_PATH = Path(__file__).resolve().parent / "web" / "canvas_review" / "panorama.js"


def _audit_startup_secrets() -> None:
    """Log a CRITICAL for every production-required secret that's unset.

    These don't block startup — a dev environment can run fine with
    placeholders — but production deployments should grep their startup
    logs for ``CRITICAL`` after every release.
    """
    import os

    findings: list[str] = []

    # OAuth token encryption — the most directly exploitable miss.
    if not os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip() and not os.environ.get("CSRF_SECRET_KEY", "").strip():
        findings.append(
            "TOKEN_ENCRYPTION_KEY (and CSRF_SECRET_KEY fallback) UNSET — "
            "instructor OAuth tokens encrypted with a hardcoded key; anyone with "
            "this source can decrypt a Redis dump. Generate with "
            "`python -m connector.tools.generate_keys` and set in .env."
        )

    # CSRF token signing — separate but related.
    if not os.environ.get("CSRF_SECRET_KEY", "").strip():
        findings.append(
            "CSRF_SECRET_KEY UNSET — CSRF tokens are signed with a derivation "
            "of the LTI keypair fingerprint, stable but not a secret. Set in .env."
        )

    # Iris auth. Iris has no per-user identity: on an open deployment
    # anyone who can reach it can list and read every session, i.e. every
    # course document this connector ever converted. The live probe in
    # _audit_iris() checks the other half (is Iris itself gated?).
    if not os.environ.get("IRIS_API_TOKEN", "").strip():
        findings.append(
            "IRIS_API_TOKEN UNSET — the connector is calling Iris without its "
            "shared secret, which only works if Iris is open (server.api_token "
            "blank). An open Iris serves every converted course document to "
            "anyone who can reach it. Set server.api_token in Iris and the same "
            "value as IRIS_API_TOKEN here."
        )

    if not findings:
        logger.info("startup secrets audit: OK (no missing production secrets)")
        return

    for f in findings:
        logger.critical("startup secrets audit: %s", f)


async def _audit_iris() -> None:
    """One look at the Iris deployment at boot: reachable, gated, ours.

    Logged, never fatal — Iris may simply start after the connector, and
    the bridge retries on its own. What must not pass quietly is an Iris
    that anyone can read without a token, or a token Iris rejects.
    """
    from connector.canvas.iris_client import IrisApiError, IrisClient

    iris = IrisClient()
    try:
        health = await iris.health()
    except IrisApiError as exc:
        logger.warning(
            "Iris not reachable at %s (%s). Conversions wait until it is.",
            iris.base_url, exc,
        )
        return
    try:
        open_deployment = await iris.is_open()
    except IrisApiError as exc:
        logger.warning("Iris gate probe failed: %s", exc)
        open_deployment = False
    if open_deployment:
        logger.critical(
            "startup secrets audit: Iris at %s answers without a token. Anyone "
            "who can reach it can list and read every converted course "
            "document. Set server.api_token in Iris (and IRIS_API_TOKEN here), "
            "and keep Iris off the public internet.", iris.base_url,
        )
    else:
        try:
            await iris.me()
        except IrisApiError as exc:
            if exc.status_code == 401:
                logger.critical(
                    "startup secrets audit: Iris rejects IRIS_API_TOKEN (401). "
                    "Every upload will fail until it matches Iris's server.api_token."
                )
            else:
                logger.warning("Iris credential check failed: %s", exc)
    try:
        limits = await iris.limits()
        pages = limits.max_pages
    except IrisApiError:
        pages = None
    logger.info(
        "Iris %s at %s (pages per session: %s, tagged PDF: %s)",
        health.get("version", "?"), iris.base_url, pages,
        "yes" if (pages is not None and limits.tagged_pdf) else "no",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # connector.logging_setup implements JSON output and request/user/course
    # context propagation, and docs/DEPLOY.md documents both — but nothing
    # ever called it, so the connector emitted plain text and the context
    # vars went nowhere. Wire it up here, where logging is configured once
    # per process. Format follows LOG_FORMAT (json in production, text in dev).
    configure_logging(settings.log_level)
    logger.info(
        "connector starting up — environment=%s iris_api_base_url=%s lti_enabled=%s",
        settings.environment,
        settings.iris_api_base_url,
        settings.lti_enabled,
    )

    # Startup secrets audit — CRITICAL if anything that's required for
    # production is unset. Doesn't block boot; an operator can still
    # bring the connector up in dev with placeholders. The logs make
    # it impossible to miss when reviewing a fresh deploy.
    _audit_startup_secrets()
    if settings.lti_enabled:
        try:
            await asyncio.wait_for(_audit_iris(), timeout=15)
        except Exception:  # noqa: BLE001 - an audit must never block boot
            logger.warning("Iris startup check did not complete", exc_info=True)

    # Use the shared connection pool the request-time dependency uses, so
    # workers and HTTP handlers share Redis connections instead of
    # opening a second pool.
    import redis.asyncio as redis_module

    redis_client = redis_module.Redis(connection_pool=_get_redis_pool())
    shutdown_event = asyncio.Event()

    watcher_task: asyncio.Task[None] | None = None
    bridge_task: asyncio.Task[None] | None = None
    if settings.lti_enabled:
        watcher_task = asyncio.create_task(
            start_canvas_watcher(redis_client, shutdown_event=shutdown_event),
            name="canvas_watcher",
        )
        bridge_task = asyncio.create_task(
            start_bridge(redis_client, shutdown_event=shutdown_event),
            name="conversion_bridge",
        )
        logger.info("started background workers (watcher + bridge)")
    else:
        logger.info("LTI disabled — workers not started")

    try:
        yield
    finally:
        logger.info("connector shutting down — signalling workers")
        shutdown_event.set()
        # Drain any running worker tasks so cancellation is clean.
        pending = [t for t in (watcher_task, bridge_task) if t is not None]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10.0)
            except TimeoutError:
                logger.warning("workers did not exit within 10s — cancelling forcefully")
                for t in pending:
                    t.cancel()


app = FastAPI(
    title="Equalify Iris for Canvas",
    description=(
        "Canvas LTI 1.3 connector bridging Canvas LMS to the Equalify Iris "
        "accessible-HTML service. See docs/ARCHITECTURE.md for the architecture."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Canvas embeds the panorama bundle inside an iframe served from the
# institutional Canvas origin (e.g. school.instructure.com) and makes
# cross-origin fetches from there to the connector. Without explicit
# CORS allow-origin headers the browser blocks every fetch — see the
# /canvas/panorama/csrf, /canvas/consent/status, etc. failures observed
# in early pilot testing. Operators configure the allowlist via
# CANVAS_ALLOWED_ORIGINS (comma-separated) and optionally
# CANVAS_ALLOWED_ORIGIN_REGEX for wildcard matches.
_origins = [
    o.strip()
    for o in (settings.canvas_allowed_origins or "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=(settings.canvas_allowed_origin_regex or None),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # A browser reads "*" literally on a request made with credentials, so
    # the overlay's downloads (which send the LTI cookie, for instructor
    # previews) need the download's file name listed by name.
    expose_headers=["*", "Content-Disposition", "X-Accessible-Pdf"],
)

app.include_router(lti_router)
app.include_router(canvas_consent.router)
app.include_router(canvas_oauth.router)
app.include_router(canvas_panorama.router)
app.include_router(canvas_review.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe used by Docker and orchestrators."""
    return {"status": "ok"}


@app.get("/panorama.js", include_in_schema=False)
async def panorama_js_root() -> Response:
    """Serve the Theme-Editor JS bundle from the site root.

    Canvas's Theme Editor injects ``<script src="/panorama.js">`` on every
    page; the script lives under the connector's own origin (via
    ``LTI_PUBLIC_URL``) and decides per-page whether to render the
    accessibility overlay. Mirrored at ``/lti/panorama.js`` for the
    legacy loader.
    """
    if not _PANORAMA_JS_PATH.exists():
        return Response(status_code=404, content="// panorama.js bundle not found")
    return Response(
        content=_PANORAMA_JS_PATH.read_bytes(),
        media_type="application/javascript",
        headers={
            # Strong no-cache for browsers, edge proxies, and CDNs.
            # Cloudflare in particular ignores a lone ``no-cache`` directive
            # and applies its own ``max-age=14400`` to JS responses by
            # default; explicit ``no-store`` plus the CF-specific override
            # below stop it from caching at the edge.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "CDN-Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
