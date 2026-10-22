"""Validate configuration before the connector boots.

Run after filling in ``.env`` and before starting the stack:

    python scripts/preflight.py
    docker compose run --rm connector python scripts/preflight.py

Each check answers a question that otherwise gets answered later by a
confusing runtime failure. A missing key pair shows up as an LTI launch
that dies mid-handshake; a trailing slash on the public URL shows up as
Canvas rejecting the redirect URI as unregistered; a wrong Iris token
shows up as documents that are discovered and then never convert.

Exits 0 when the connector should be able to start, 1 otherwise. Warnings
never fail the run — they mark settings that are legal but rarely what an
operator intended.

It also contacts the configured Iris (reachable? gated? does it accept our
token?). Pass ``--offline`` to skip that, e.g. when Iris is not up yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run by path (``python scripts/preflight.py``) sys.path[0] is scripts/, so
# the connector package is invisible. Put the repo root first — the whole
# point of this script is to be runnable before anything is installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

errors: list[str] = []
warnings: list[str] = []


def _secret(value: object) -> str:
    """Read a pydantic ``SecretStr`` or a plain string uniformly."""
    if value is None:
        return ""
    get = getattr(value, "get_secret_value", None)
    return (get() if callable(get) else str(value)).strip()


def _text(settings: object, name: str) -> str:
    return (getattr(settings, name, "") or "").strip()


def main() -> int:
    try:
        from connector.config import settings
    except Exception as exc:  # noqa: BLE001 — any import failure is fatal here
        print(f"FAIL: configuration could not be loaded: {exc}", file=sys.stderr)
        print("      Check .env against .env.example.", file=sys.stderr)
        return 1

    # --- Equalify Iris --------------------------------------------------
    iris_url = _text(settings, "iris_api_base_url")
    if not iris_url:
        errors.append("IRIS_API_BASE_URL is not set — the connector has no Iris to convert with.")
    if not _secret(getattr(settings, "iris_api_token", None)):
        warnings.append(
            "IRIS_API_TOKEN is not set. That only works against an open Iris, and "
            "an open Iris lets anyone who can reach it read every converted course "
            "document. Set server.api_token in Iris and the same value here."
        )
    if iris_url and "--offline" not in sys.argv:
        _check_iris(settings)

    # --- Legacy Reflow --------------------------------------------------
    if _text(settings, "reflow_api_base_url"):
        warnings.append(
            "REFLOW_API_BASE_URL is set, so documents converted before the switch "
            "to Iris are still read from Reflow Core. Run "
            "`python -m connector.tools.migrate_reflow_jobs` and then remove it."
        )

    if not getattr(settings, "lti_enabled", False):
        warnings.append(
            "LTI_ENABLED is false. The API will start, but Canvas launches and "
            "the watcher/bridge workers stay switched off."
        )
        _report()
        return 1 if errors else 0

    # --- LTI identity ---------------------------------------------------
    public_url = _text(settings, "lti_public_url")
    if not public_url:
        errors.append(
            "LTI_PUBLIC_URL is not set. Canvas needs an absolute, publicly "
            "reachable URL for the launch and OAuth callbacks."
        )
    else:
        if not public_url.startswith("https://"):
            warnings.append(
                f"LTI_PUBLIC_URL is {public_url!r}. Canvas requires https for "
                "any real install; http only works for local experiments."
            )
        if public_url.endswith("/"):
            warnings.append(
                "LTI_PUBLIC_URL has a trailing slash. Redirect URIs are built by "
                "concatenation, producing '//canvas/oauth/callback', which Canvas "
                "rejects as an unregistered redirect URI."
            )

    for name in (
        "lti_issuer",
        "lti_client_id",
        "lti_deployment_id",
        "lti_auth_login_url",
        "lti_auth_token_url",
        "lti_jwks_url",
    ):
        value = _text(settings, name)
        if not value:
            errors.append(f"{name.upper()} is not set — LTI launches cannot be validated.")
        elif value.upper().startswith("FILL-IN"):
            errors.append(f"{name.upper()} still holds a placeholder value ({value!r}).")

    # --- Key pair --------------------------------------------------------
    # The most common first-run failure, and the least obvious from the
    # error it eventually produces.
    private = Path(_text(settings, "lti_private_key_path") or "keys/private.pem")
    if not private.exists():
        errors.append(
            f"No LTI private key at {private}. Generate one with "
            "scripts/generate_lti_keys.sh — Canvas cannot verify the tool without it."
        )

    # --- Canvas OAuth ----------------------------------------------------
    # Optional for boot, but nothing publishes without it: the watcher and
    # bridge act on a user's behalf, not the tool's.
    oauth_id = _text(settings, "canvas_oauth_client_id")
    oauth_secret = _secret(getattr(settings, "canvas_oauth_client_secret", None))
    if bool(oauth_id) != bool(oauth_secret):
        errors.append(
            "CANVAS_OAUTH_CLIENT_ID and CANVAS_OAUTH_CLIENT_SECRET must be set "
            "together — one without the other cannot complete the OAuth flow."
        )
    elif not oauth_id:
        warnings.append(
            "Canvas OAuth is not configured. Launches will work, but no document "
            "will ever be published: publishing acts as a user, not as the tool."
        )

    _report()
    return 1 if errors else 0


def _check_iris(settings: object) -> None:
    """Ask the configured Iris the three questions that matter (skip with --offline)."""
    import asyncio

    try:
        from connector.canvas.iris_client import IrisApiError, IrisClient
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not load the Iris client to check Iris: {exc}")
        return

    async def probe() -> None:
        iris = IrisClient()
        try:
            health = await iris.health()
        except IrisApiError as exc:
            errors.append(
                f"Iris is not reachable at {iris.base_url}: {exc.message}. Start Iris "
                "or fix IRIS_API_BASE_URL (inside Docker, 'localhost' is the "
                "connector's own container)."
            )
            return
        if health.get("service") != "equalify-iris":
            warnings.append(f"{iris.base_url} answered /v1/health but not as Iris: {health}")
        try:
            if await iris.is_open():
                errors.append(
                    f"Iris at {iris.base_url} accepts requests with no token. Set "
                    "server.api_token (IRIS_API_TOKEN in Iris's .env) and restart it."
                )
            else:
                await iris.me()
        except IrisApiError as exc:
            if exc.status_code == 401:
                errors.append(
                    "Iris rejects IRIS_API_TOKEN. It must equal Iris's server.api_token."
                )
            else:
                warnings.append(f"Iris credential check failed: {exc.message}")
        try:
            limits = await iris.limits(use_cache=False)
            print(
                f"INFO: Iris {health.get('version', '?')} at {iris.base_url}: "
                f"{limits.max_pages} pages per session, tagged PDF "
                f"{'on' if limits.tagged_pdf else 'off'}."
            )
        except IrisApiError as exc:
            warnings.append(f"Could not read Iris limits: {exc.message}")

    asyncio.run(probe())


def _report() -> None:
    for msg in warnings:
        print(f"WARN: {msg}")
    for msg in errors:
        print(f"FAIL: {msg}", file=sys.stderr)
    if errors:
        print(
            f"\npreflight: {len(errors)} problem(s) must be fixed before boot.",
            file=sys.stderr,
        )
    else:
        suffix = f" ({len(warnings)} warning(s))" if warnings else ""
        print(f"preflight: configuration looks usable{suffix}.")


if __name__ == "__main__":
    raise SystemExit(main())
