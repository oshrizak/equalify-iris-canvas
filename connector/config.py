"""Connector configuration.

Reads from environment / ``.env`` via ``pydantic-settings``. The connector
converts documents with Equalify Iris (``iris_*`` settings) and talks to
Canvas over LTI 1.3 + REST (``lti_*`` / ``canvas_*``). The ``reflow_*``
settings are legacy: they are only read for documents that were converted
by Equalify Reflow before the switch to Iris (see
``connector.tools.migrate_reflow_jobs``).

Field naming intentionally mirrors the source fork's names so that ported
modules can keep ``settings.canvas_*`` / ``settings.lti_*`` access patterns
verbatim.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Connector settings from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ------------------------------------------------------------------
    # Equalify Iris — the conversion service the connector is a client of
    # ------------------------------------------------------------------
    iris_api_base_url: str = Field(
        default="http://localhost:8080",
        description=(
            "Base URL of the Equalify Iris deployment (without /v1). The "
            "connector creates sessions, polls them, and fetches their HTML here."
        ),
    )
    iris_api_token: SecretStr | None = Field(
        default=None,
        description=(
            "Iris's shared secret (its server.api_token), sent as "
            "'Authorization: Bearer <token>'. Leave unset only for an Iris that "
            "nothing else can reach: an open Iris lists every session it holds."
        ),
    )
    iris_poll_seconds: int = Field(
        ge=5, le=600, default=30,
        description="How often the bridge polls Iris for session status.",
    )
    iris_request_timeout_seconds: float = Field(
        ge=1, le=600, default=30,
        description="Timeout for ordinary Iris API calls (status, output, close).",
    )
    iris_upload_timeout_seconds: float = Field(
        ge=10, le=3600, default=300,
        description=(
            "Timeout for creating a session. Iris rasterizes the PDF before it "
            "answers, so a 25-page upload can take a minute on a small box."
        ),
    )
    iris_max_pages_per_session: int = Field(
        ge=0, le=1000, default=0,
        description=(
            "Pages per Iris session. Longer PDFs are split into parts of this "
            "size and stitched back together. 0 uses the page cap the Iris "
            "deployment publishes at GET /v1/limits (25 today)."
        ),
    )
    iris_max_document_pages: int = Field(
        ge=0, le=100_000, default=300,
        description=(
            "Largest document the watcher will send to Iris, in pages. Iris "
            "prices by the page, so this is a cost guard. 0 = no limit."
        ),
    )
    iris_close_sessions_on_approve: bool = Field(
        default=False,
        description=(
            "Close the Iris session(s) when faculty approve a document they did "
            "not edit, telling Iris a human accepted its output. Off by default: "
            "on close Iris copies a page image and its accepted HTML per agent "
            "into its regression fixtures (the newest five per agent), which "
            "session retention does not delete, and the connector never uses "
            "the feedback loop those fixtures exist to protect. Edited "
            "documents are never closed, because Iris's output was not what "
            "was accepted."
        ),
    )
    iris_estimated_cost_per_page_cents: int = Field(
        ge=0, le=10_000, default=11,
        description=(
            "Pre-flight cost estimate per page for the spend cap. Iris documents "
            "about 10.7 cents a page for its suggested model setup."
        ),
    )
    iris_convert_office_documents: bool = Field(
        default=True,
        description=(
            "Convert .docx/.doc/.pptx to PDF with LibreOffice before sending them "
            "to Iris (Iris accepts PDFs and images only). Needs `soffice` on PATH; "
            "without it Office files are skipped."
        ),
    )
    iris_soffice_path: str = Field(
        default="soffice",
        description="LibreOffice binary used for the Office-to-PDF step.",
    )
    iris_soffice_timeout_seconds: int = Field(
        ge=10, le=1800, default=180,
        description="How long one Office-to-PDF conversion may run.",
    )
    # Optional prices for turning Iris's token counts into a cost per job.
    # Iris reports tokens and deliberately not dollars; leave these at 0 to
    # record tokens only. One rate set is an approximation when Iris routes
    # agents to different models.
    iris_price_input_per_mtok_usd: float = Field(ge=0, default=0.0)
    iris_price_output_per_mtok_usd: float = Field(ge=0, default=0.0)
    iris_price_cache_read_per_mtok_usd: float = Field(ge=0, default=0.0)
    iris_price_cache_write_per_mtok_usd: float = Field(ge=0, default=0.0)

    # ------------------------------------------------------------------
    # Legacy: Reflow Core. Only used for documents converted before the
    # switch to Iris, and only until they are migrated.
    # ------------------------------------------------------------------
    reflow_api_base_url: str | None = Field(
        default=None,
        description=(
            "Legacy. Base URL of the Reflow Core that converted older documents. "
            "Set it while migrating (python -m connector.tools.migrate_reflow_jobs) "
            "or to keep serving un-migrated Reflow documents; leave unset otherwise."
        ),
    )
    reflow_api_key: SecretStr | None = Field(
        default=None,
        description="Legacy. API key for the Reflow Core above.",
    )
    reflow_poll_seconds: int = Field(
        ge=5,
        le=600,
        default=30,
        description="Legacy. Superseded by IRIS_POLL_SECONDS.",
    )

    # ------------------------------------------------------------------
    # FastAPI runtime
    # ------------------------------------------------------------------
    api_host: str = Field(default="0.0.0.0", description="Host the FastAPI server binds to.")
    api_port: int = Field(
        ge=1, le=65535, default=8000,
        description="Port the FastAPI server listens on.",
    )
    log_level: str = Field(default="INFO", description="Log level (DEBUG, INFO, WARNING, ERROR).")
    environment: str = Field(default="production", description="Runtime label ('dev' or 'production').")

    # ------------------------------------------------------------------
    # API key auth — clients calling THIS connector
    # ------------------------------------------------------------------
    enable_api_key_auth: bool = Field(
        default=True, description="Require an API key on protected endpoints."
    )
    api_key_header_name: str = Field(
        default="X-API-Key", description="Header to read API keys from."
    )
    api_keys: SecretStr | None = Field(
        default=None,
        description=(
            "Comma-separated allowlist. Each entry is a bare key or a 'label:key' "
            "pair (split on the first colon). The label is logged on every "
            "authenticated request; the key itself is never logged."
        ),
    )

    # ------------------------------------------------------------------
    # Redis — Canvas-side state (``eq-pdf:lti:*``, ``eq-pdf:canvas:*``)
    # ------------------------------------------------------------------
    redis_url: str = Field(
        default="redis://redis:6379/0",
        description="Redis URL for Canvas-side state and LTI session storage.",
    )
    redis_max_connections: int = Field(
        ge=1, le=1000, default=10,
        description="Maximum connections in the Redis client pool.",
    )

    # ------------------------------------------------------------------
    # S3 — legacy: presigned URLs returned by Reflow Core. Iris serves its
    # output over its own API, so none of this is used for Iris documents.
    # ------------------------------------------------------------------
    aws_region: str = Field(default="us-east-1", description="AWS region for boto3 clients.")
    s3_public_url: str | None = Field(
        default=None,
        description="Public S3 URL used in faculty-facing links.",
    )
    # The 2026-06-17 bug from the source fork: without this the bridge fails
    # to fetch markdown from Reflow Core's presigned URLs when the public host
    # isn't reachable from inside the connector container.
    s3_internal_url: str | None = Field(
        default=None,
        description=(
            "Internal S3 URL the connector rewrites presigned URLs to before "
            "fetching. Required when ``s3_public_url`` isn't reachable from the "
            "connector (e.g. presigned URL points at 'localhost' but the "
            "connector lives in Docker)."
        ),
    )

    # ------------------------------------------------------------------
    # LTI 1.3
    # ------------------------------------------------------------------
    lti_enabled: bool = Field(default=False, description="Enable LTI endpoints and Canvas workers.")
    lti_issuer: str | None = Field(
        default=None, description="Canvas issuer URL (e.g. https://canvas.instructure.com)."
    )
    lti_client_id: str | None = Field(default=None, description="Canvas Developer Key client_id.")
    lti_deployment_id: str | None = Field(default=None, description="Canvas deployment_id.")
    lti_auth_login_url: str | None = Field(
        default=None, description="Canvas OIDC authorize_redirect endpoint."
    )
    lti_auth_token_url: str | None = Field(
        default=None, description="Canvas OAuth2 token endpoint."
    )
    lti_jwks_url: str | None = Field(default=None, description="Canvas JWKS endpoint.")
    lti_private_key_path: str = Field(
        default="/app/keys/lti_private.pem", description="Path to LTI signing private key."
    )
    lti_public_key_path: str = Field(
        default="/app/keys/lti_public.pem", description="Path to LTI signing public key."
    )
    lti_state_ttl_seconds: int = Field(
        ge=60, le=3600, default=600, description="OIDC state nonce TTL (seconds)."
    )
    lti_public_url: str | None = Field(
        default=None,
        description="Public origin of this tool (used in LTI tool config and notification deep links).",
    )

    # ------------------------------------------------------------------
    # Canvas LMS
    # ------------------------------------------------------------------
    canvas_tenant: str = Field(
        default="default",
        description=(
            "Tenant id for Redis key namespacing. 'default' uses unprefixed keys; "
            "any other value namespaces every key as ``eq-pdf:t:{tenant}:...`` so "
            "multiple campuses can share one Redis."
        ),
    )
    canvas_api_url: str | None = Field(default=None, description="Canvas instance base URL.")
    canvas_api_token: SecretStr | None = Field(
        default=None,
        description="Canvas API token (fallback when not using per-instructor OAuth).",
    )
    canvas_watched_courses: str = Field(
        default="", description="Comma-separated Canvas course ids the watcher polls."
    )
    multi_tenant_watcher: bool = Field(
        default=False,
        description="Watcher iterates LTI-registered platforms instead of ``canvas_watched_courses``.",
    )
    canvas_oauth_client_id: str = Field(
        default="",
        description=(
            "Canvas API Key (non-LTI Developer Key) client_id for the user-OAuth2 "
            "flow. Required because LTI 1.3 keys are not accepted by Canvas's "
            "/login/oauth2/auth endpoint."
        ),
    )
    canvas_oauth_client_secret: SecretStr | None = Field(
        default=None,
        description="Secret for the user-OAuth API Key.",
    )
    canvas_allowed_origins: str = Field(
        default="",
        description="Comma-separated Canvas origins allowed to call panorama endpoints.",
    )
    consent_extra_frame_ancestors: str = Field(
        default="",
        description=(
            "Comma-separated extra origins allowed to embed the consent page, "
            "added to the default 'https://*.instructure.com'. Set this when "
            "Canvas is self-hosted on your own domain, or when you front the "
            "tool with a tunnel during development — e.g. "
            "'https://*.example.edu,https://*.ngrok-free.dev'. Wrong or "
            "missing values show up as a consent page that renders blank "
            "inside the Canvas iframe."
        ),
    )
    canvas_allowed_origin_regex: str = Field(
        default="",
        description="Regex of Canvas origins allowed (takes precedence over ``canvas_allowed_origins``).",
    )
    canvas_poll_seconds: int = Field(
        ge=15, le=3600, default=60, description="Canvas file poll interval (seconds)."
    )

    # Per-course AI-API spend cap. Default of 0 disables the cap;
    # operators opt in by setting a per-course budget.
    canvas_monthly_spend_cap_usd_default: int = Field(
        ge=0, le=100_000, default=0,
        description="Default monthly AI-API spend cap in USD per Canvas course (0=unlimited).",
    )
    canvas_monthly_spend_cap_overrides: str = Field(
        default="",
        description='JSON map of course_id->cap_usd overrides, e.g. \'{"50594": 250}\'.',
    )
    canvas_estimated_cost_per_doc_cents: int = Field(
        ge=0, le=10000, default=10,
        description="Pre-flight per-document cost estimate in cents (used by the spend cap).",
    )

    # ------------------------------------------------------------------
    # What Reflow is allowed to convert
    #
    # Two independent triggers:
    #   1. The drop folder — always live. Faculty copy a PDF into it to say
    #      "convert this one". Explicit, per-document authorisation, and it
    #      works before anyone has enabled the course.
    #   2. Full-course scanning — off until a human turns it on for that
    #      course (see state.set_course_enabled). Only then does the watcher
    #      sweep Files/Modules/Pages/Assignments/etc.
    #
    # Default is opt-in because silent automatic processing of everything a
    # faculty member ever uploaded is not something a one-time consent
    # disclaimer can carry.
    # ------------------------------------------------------------------
    canvas_require_course_optin: bool = Field(
        default=True,
        description=(
            "When true, full-course scanning requires explicit per-course "
            "opt-in. The drop folder still works regardless. Set false to "
            "restore the legacy scan-everything behaviour."
        ),
    )
    canvas_drop_folder_name: str = Field(
        default="Reflow - Convert to Accessible",
        description=(
            "Course Files folder that faculty copy PDFs into to request "
            "conversion. Created on demand. Kept ASCII — Canvas folder names "
            "appear in URLs."
        ),
    )

    # How large a converted document may be before the bridge stops writing it
    # inline into the Canvas Page and falls back to a link stub.
    #
    # Canvas's hard application limit is 511,999 characters, measured against a
    # Canvas Cloud instance: past it Canvas returns HTTP 400 "is too long
    # (maximum is 511,999 characters)". There is no edge size rule on this path
    # — an earlier assumption of a ~8KB WAF cap was wrong. Some Canvas Cloud
    # deployments do enforce *content* rules at the CDN; see
    # docs/TROUBLESHOOTING.md.
    #
    # The default leaves ~22% headroom under that ceiling for markup the
    # renderer may add later. At roughly 1.8KB of HTML per PDF page this is
    # about 220 pages inline, which covers essentially every course document.
    canvas_page_inline_max_chars: int = Field(
        ge=0, le=511_999, default=400_000,
        description=(
            "Max rendered-HTML length written directly into a Canvas Page. "
            "Documents above this fall back to a link stub. Canvas rejects "
            "bodies over 511,999 characters outright. 0 forces stub mode."
        ),
    )

    # Canvas-side data retention. 0 disables purging.
    canvas_job_retention_days: int = Field(
        ge=0, le=3650, default=90,
        description="Days to retain Canvas job records in Redis (0=keep forever).",
    )
    canvas_audit_retention_days: int = Field(
        ge=0, le=3650, default=365,
        description="Days to retain approval-audit events in Redis (0=keep forever).",
    )
    canvas_retention_sweep_every_ticks: int = Field(
        ge=1, le=10080, default=240,
        description="Run the retention sweep every N watcher ticks (~4h at the 60s default poll).",
    )

    # ------------------------------------------------------------------
    # Prometheus metrics
    # ------------------------------------------------------------------
    enable_metrics: bool = Field(
        default=True, description="Enable Prometheus metrics collection and the /metrics endpoint."
    )
    metrics_port: int = Field(
        ge=1, le=65535, default=8001,
        description="Port for the Prometheus metrics server.",
    )


settings = Settings()
