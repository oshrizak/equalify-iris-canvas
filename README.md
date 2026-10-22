# Equalify Iris for Canvas

A Canvas LMS LTI 1.3 connector for [Equalify Iris](https://github.com/EqualifyEverything/equalify-iris).
The connector launches inside Canvas, watches courses for new PDFs, sends
them to Iris for conversion into accessible HTML, and publishes the result
back into Canvas as Pages — with a faculty review workflow, a dial-badge
overlay over Canvas's own file UI, and a full alt-format catalogue
(accessible HTML, tagged PDF, ePub, Braille, audio, translation, plain text,
Markdown).

Its siblings bring Iris to websites:
[Equalify Iris for WordPress](https://github.com/EqualifyEverything/equalify-iris-wp)
and [Equalify Iris for Drupal](https://github.com/oshrizak/equalify-iris-drupal).

```
   Canvas LMS                         Equalify Iris
        ↕                                   ↑
        ↕ LTI 1.3, OAuth, REST          HTTP API (/v1)
        ↕                                   │
  equalify-iris-canvas  ────────────────────┘
  (this repo)
```

The connector owns the Canvas-side experience and state; Iris owns the
document pipeline. The two services talk over HTTP — the connector imports
zero Iris code. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
full picture and [`docs/IRIS_API.md`](docs/IRIS_API.md) for the exact
endpoints the connector consumes.

> **Upgrading from Equalify Reflow?** This connector converted with Reflow
> Core until it moved to Iris. Read
> [`docs/MIGRATING_FROM_REFLOW.md`](docs/MIGRATING_FROM_REFLOW.md) before
> upgrading a live deployment: it covers moving to this repository without
> losing the connector's Redis data, running both services during the
> switch, copying Reflow-converted documents into the connector, the
> re-acknowledged faculty disclaimer, and what changes for faculty.

## Who this is for

- **Faculty** — upload a PDF, get an accessible Canvas Page in minutes,
  review before students see it, fix anything with an in-browser editor,
  decide what counts as a waivable accessibility finding.
- **Students** — open the row's dial, get the accessible version in the
  format that works for them (HTML, Braille, audio, translated to their
  L1, etc.).
- **Operators / Canvas admins** — runbook-driven deployment, see
  [`OPERATIONS.md`](OPERATIONS.md).

## Features

### Faculty surfaces

- **Accessibility dial on every PDF row** in Canvas Files, with two
  honest numbers: PDF/UA-1 score (veraPDF) on the original PDF, and a
  WCAG structural-check score on the generated HTML. These measure
  different things and the UI labels them as such — there is no
  before/after framing.
- **Pending-scan marker** on rows the watcher hasn't picked up yet, so
  faculty isn't left wondering whether processing started.
- **In-modal alt-format menu** grouped by purpose: Read, Listen &
  translate, Document formats. 10 generated formats; the original
  source PDF stays in Canvas Files where faculty uploaded it.
- **Per-document review screen** (`/canvas/review/{job}`) — side-by-side
  live PDF preview + accessible HTML (or live Canvas Page once
  published). Approve, reject, pull-back-to-draft, unpublish.
- **Inline HTML editor** for the accessible version — fix table semantics,
  alt text, headings, etc.; saves become the source of truth for every
  downstream format.
- **"Check before approving" notes** on the review screen: the pages Iris
  could not convert, the pages that failed Iris's own accuracy check, and
  the accessibility issues its reviewer left open — Iris reports these, and
  they are exactly where a reviewer should look. A document that did not
  convert gets a **"Try again"** button that re-sends only the parts that
  failed.
- **WCAG publication gate** (opt-in via `REQUIRE_WCAG_GATE=true`) — the
  approval modal renders a 4-item visual-inspection checklist plus
  per-rule waivers for any automated WCAG errors. Faculty can't publish
  with unwaived errors when the gate is on; nothing changes when it's off.

### Alt-formats

| Format | Tool | Notes |
|---|---|---|
| Accessible HTML | mistune + sanitize | Canonical source for every other format |
| HTML with math | + MathJax (mhchem extension) | Not in the menu: Accessible HTML loads MathJax itself when a (Reflow-era) document carries LaTeX, and browsers draw Iris's MathML. The URL still answers. |
| Plain text | tag-strip | Not in the menu (it drops headings, lists and tables). The URL still answers, as a download. |
| Markdown | markdownify | Not in the menu. The URL still answers, as a download: derived from the canonical HTML, math as `$...$`, print pages as `[Page N]`. |
| Searchable PDF | Iris's tagged PDF (`equalify-iris-pdf`) | The original file, page for page, with a structure tree and a text layer built from Iris's HTML; scans included. A document Iris converted in parts is tagged part by part and joined. Made when faculty approve the document and kept on the connector's `connector-data` volume. When Iris cannot tag it, or faculty edited the document (Iris cannot see their edits): WeasyPrint (pdf/ua-1) renders the accessible HTML, or for an untouched scan ocrmypdf adds a text layer. |
| ePub | ebooklib | EPUB3 |
| Audio (MP3) | Amazon Polly | Requires `AWS_DEFAULT_REGION` + IAM credentials with `polly:SynthesizeSpeech`. Without them the menu shows it as under development. |
| Translate | Anthropic Claude (Sonnet 4.5) | Requires `ANTHROPIC_API_KEY`; without it the menu shows it as under development. Prompt explicitly preserves LaTeX math and `\ce{}` chemistry markup verbatim. |
| Braille (BRF) | liblouis (`lou_translate`) + in-house BANA layout | Structured transcription, not flat text: UEB contracted braille for prose, with a maths code (Nemeth where available, otherwise UEB Technical) applied *only* to expressions. Centred headings, 3-1 paragraphs, 1-3 lists, figure descriptions with the caption first, print page numbers, 40×25 pages with form feeds, words never split. See [`docs/BRAILLE.md`](docs/BRAILLE.md). |

### Behind the scenes

- **LTI 1.3 launch** — OIDC handshake, signed JWT validation, public
  JWKS, tool-config JSON for Canvas Developer Key paste-in.
- **Per-instructor Canvas OAuth2** — required because Canvas Cloud's
  general `/api/v1/*` REST endpoints don't accept LTI Advantage service
  tokens. Tokens are encrypted at rest with AES-GCM (see Security
  below).
- **Canvas file watcher** — discovers new PDFs (and Word/PowerPoint files,
  converted to PDF with LibreOffice) and submits them to Iris. PDFs longer
  than Iris's per-session page cap (25) are cut into parts and stitched
  back together. Configurable per-course or multi-tenant across every
  registered LTI platform.
- **Bridge worker** — polls Iris for completion, stitches the parts' HTML
  into one document stored next to the job, lifts each figure Iris
  described out of the source PDF (Iris cannot return image files),
  uploads figures into a course folder, creates or updates a Canvas Page.
  Honors Iris's rate limits (429 / `Retry-After`), retries uploads a
  bounded number of times, and re-sends parts Iris lost.
- **veraPDF integration** — every source PDF audited against PDF/UA-1
  on submission; failed rules surfaced inline in the alt-format modal.
- **Cost tracking** — the spend cap reserves per page (Iris prices by the
  page); at completion Iris's token counts are recorded per job, and
  turned into a cost when per-token prices are configured.

### Security & ops

- **OAuth tokens encrypted at rest** with AES-GCM (`connector/canvas/privacy.py`).
  Key derivation falls back through `TOKEN_ENCRYPTION_KEY` →
  `CSRF_SECRET_KEY` → a constant; the connector logs `CRITICAL` once
  per process on the constant fallback so it's impossible to miss in
  prod logs.
- **CSRF on every state-changing POST.** No exceptions; the panorama
  overlay fetches a token via `/canvas/panorama/csrf` on load.
- **Rate limiting per `(endpoint, user_id)`** on every state-changing
  POST. 30/min for approve/reject; 60/min for the auto-save editor;
  10/min for PII decisions; 5/min for bulk approve. See
  [`OPERATIONS.md`](OPERATIONS.md#rate-limiting).
- **Trusted-origin allowlist** (`CANVAS_ALLOWED_ORIGINS`) blocks cross-
  origin state changes from anything that isn't your Canvas host.
- **Append-only audit log** of every approve / reject / unpublish / PII
  decision, with retention controls.
- **Startup secrets audit** logs `CRITICAL` for every production-required
  secret that's unset. Doesn't block boot; impossible to miss.
- **Redis persistence** (AOF + RDB) on a named volume; container restart
  no longer wipes faculty consent records or the audit log. Off-host
  backup via `scripts/backup-redis.sh` (cron-friendly, optional S3
  upload).
- **Integration tests** covering the LTI session + CSRF + rate-limit
  pipeline for the PII decision flow and the publish-approval flow.

## Quick start (local dev)

```bash
cp .env.example .env
# Fill in IRIS_API_TOKEN (and IRIS_API_BASE_URL unless Iris is on this
# host's port 8080), LTI_*, CANVAS_*.
# Generate the secrets:
docker compose run --rm connector python -m connector.tools.generate_keys >> .env
./scripts/generate_lti_keys.sh

# Check the configuration before booting — catches the common first-run
# mistakes (missing key pair, placeholder client id, trailing slash on
# LTI_PUBLIC_URL) with a readable message instead of a runtime failure.
docker compose run --rm connector python scripts/preflight.py

docker compose up
```

Visit `http://localhost:8000/health` for liveness and
`http://localhost:8000/lti/config.json` for the JSON to paste into a
Canvas Developer Key (full walkthrough:
[`docs/CANVAS_SETUP.md`](docs/CANVAS_SETUP.md)).

For dev hot-reload, layer the override:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

For an end-to-end smoke test against a local Iris, see
[`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md).

## Deploying

The connector is one container plus a Redis it owns. Full detail —
image, secrets, networking, scaling, health checks — is in
[`docs/DEPLOY.md`](docs/DEPLOY.md); the shape of it is:

```bash
# 1. On the host: clone, configure, generate keys
git clone https://github.com/oshrizak/equalify-iris-canvas.git
cd equalify-iris-canvas
cp .env.example .env          # then fill it in — see docs/DEPLOY.md
./scripts/generate_lti_keys.sh
docker compose run --rm connector python -m connector.tools.generate_keys >> .env

# 2. Verify the configuration before it matters
docker compose run --rm connector python scripts/preflight.py

# 3. Register the tool in Canvas (creates + deploys the Developer Key,
#    prints the LTI_CLIENT_ID and LTI_DEPLOYMENT_ID to paste into .env)
docker compose run --rm connector python scripts/provision_canvas.py --apply

# 4. Boot
docker compose up -d
```

Three things decide whether this works on the first try:

- **`LTI_PUBLIC_URL` must be the public HTTPS URL Canvas will reach**, with
  no trailing slash. Canvas validates the redirect URI against it exactly.
- **Terminate TLS in front of the container.** Canvas will not launch a
  tool over plain HTTP.
- **An instructor must authorize the tool once per course** before anything
  publishes. The watcher and bridge act as that user, not as the tool —
  Canvas Cloud does not accept LTI Advantage tokens for the REST API.

When something returns 403 or 401, read
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) before changing
scopes or keys. Several failures in this integration look like permission
problems and are not.

## Configuration

Every setting is documented in [`.env.example`](.env.example). Minimum
to boot:

- `IRIS_API_BASE_URL` + `IRIS_API_TOKEN` — where Iris is reachable and
  its shared secret (Iris's `server.api_token`). Run Iris gated and off the
  public internet: it has no user accounts, so an open Iris serves every
  converted document to anyone who can reach it. Give it a session
  retention too (`storage.session_retention_days`); it keeps every upload
  forever by default.
- `LTI_ENABLED=true` plus `LTI_ISSUER`, `LTI_CLIENT_ID`,
  `LTI_DEPLOYMENT_ID`, `LTI_PUBLIC_URL` (from your Canvas Developer
  Key).
- `CANVAS_API_URL` and `CANVAS_OAUTH_CLIENT_ID` +
  `CANVAS_OAUTH_CLIENT_SECRET` (multi-tenant per-instructor OAuth).
- `CANVAS_ALLOWED_ORIGINS` — every Canvas host you'll serve from.

Required-for-production but optional-for-dev:

- `TOKEN_ENCRYPTION_KEY`, `CSRF_SECRET_KEY` — generated via
  `python -m connector.tools.generate_keys`.
- `REQUIRE_WCAG_GATE=true` — enforces the publication gate.
- `ANTHROPIC_API_KEY` — only if you're exposing Translate.
- `AWS_DEFAULT_REGION` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`
  — only if you're exposing Audio MP3 (Polly).

The connector logs a `CRITICAL` audit line on startup for every
production-required secret that's unset. See
[`OPERATIONS.md`](OPERATIONS.md#secrets-checklist-before-first-launch)
for the complete checklist.

## Repository layout

```
connector/
├── main.py              FastAPI app + worker lifespan + startup secrets audit
├── config.py            Settings (env-driven, pydantic-settings)
├── dependencies.py      Shared Redis pool
├── lti/                 LTI 1.3 handshake, JWKS, platform registry
├── canvas/              Canvas client, OAuth, alt-formats, state, privacy,
│                        iris_* (client, parts, stitching, notes),
│                        content, pdf_figures, math_render, verapdf_audit
├── api/                 Canvas-facing routers (consent, OAuth, panorama, review)
├── workers/             canvas_watcher + reflow_bridge_worker (the bridge)
├── tools/               Operator CLIs (generate_keys, migrate_reflow_jobs, retry_failed_jobs, …)
├── utils/               Cross-cutting helpers (rate_limit, retry_helpers)
└── web/canvas_review/   Front-end (overlay JS, review HTML templates)

docs/                    Architecture, setup, deploy, troubleshooting
scripts/
├── preflight.py         Validate .env before boot
├── provision_canvas.py  Create + deploy the Canvas LTI developer key
├── generate_lti_keys.sh LTI 1.3 key pair
└── backup-redis.sh      Operator backup helper
tests/
├── unit/                Pure-logic tests
└── integration/         End-to-end LTI session + CSRF + rate-limit
```

## Documentation index

- [`OPERATIONS.md`](OPERATIONS.md) — operator runbook: secrets, backups,
  rate limits, breakage modes, recovery procedures.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — component map, data
  flow, Redis key shape, multi-tenant model.
- [`docs/IRIS_API.md`](docs/IRIS_API.md) — the HTTP contract with
  Equalify Iris.
- [`docs/MIGRATING_FROM_REFLOW.md`](docs/MIGRATING_FROM_REFLOW.md) — moving
  a live deployment from Reflow Core to Iris.
- [`docs/REFLOW_API.md`](docs/REFLOW_API.md) — legacy: the Reflow Core
  contract, still used for un-migrated Reflow documents.
- [`docs/CANVAS_SETUP.md`](docs/CANVAS_SETUP.md) — Canvas admin
  walkthrough: Developer Keys, scopes, placements.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — image, env, networking, health.
- [`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md) — end-to-end smoke
  test and the first-week failure modes.
- [`docs/BRAILLE.md`](docs/BRAILLE.md) — how BRF is produced, why prose
  and maths use different braille tables, and what a production manual
  requires of the output.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — failure modes
  seen in production and how to tell them apart. Read this first when a
  Canvas call returns 403 or 401; several of them look like permissions
  problems and are not.
- [`CHANGELOG.md`](CHANGELOG.md) — released changes.

## License

[AGPL-3.0-or-later](LICENSE). Matches upstream Iris and Reflow.

## Acknowledgements

Extracted from the [`equalify-reflow`](https://github.com/EqualifyEverything/equalify-reflow)
Canvas-integration fork that ran the first CSU East Bay pilot. The
original Canvas implementation work was done by the contributors to
that fork. The conversion service is [Equalify Iris](https://github.com/EqualifyEverything/equalify-iris),
maintained by Equalify Inc., the University of Illinois Chicago, and
California State University.
