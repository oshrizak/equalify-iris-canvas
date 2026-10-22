# Architecture

`equalify-iris-canvas` is a Canvas LMS connector that sits between Canvas and
[Equalify Iris](https://github.com/EqualifyEverything/equalify-iris), the
service that turns a PDF's pages into one accessible HTML document. It is a
thin client of Iris — the conversion (a vision model per page, then a reader
and a copy editor over the whole document) stays in Iris.

```
   Canvas LMS                            Equalify Iris
   (CSUEB / etc.)                        (one gated deployment)
        ↕                                            ↑
        ↕ LTI 1.3, OAuth, REST                   HTTP API (/v1)
        ↕                                            │
  equalify-iris-canvas  ─────────────────────────────┘
  (this repo)
```

Until the move to Iris the connector converted with Equalify Reflow (Reflow
Core); see [MIGRATING_FROM_REFLOW.md](MIGRATING_FROM_REFLOW.md). Reflow-era
jobs are still served (from a migrated copy, or from Reflow Core while it is
configured), so a few legacy paths remain and are marked as such below.

## Why the conversion service is separate

Earlier, the Canvas integration lived inside the `equalify-reflow` fork — every
Canvas import lived next to the conversion pipeline. The upstream Equalify
maintainer asked to split it, so that the conversion service carries no
Canvas code and Canvas talks to it over a stable public HTTP API. The same
shape serves the WordPress and Drupal integrations, which is why swapping
Reflow Core for Iris touched the connector's client and its handling of the
output, and nothing about Canvas.

## Component responsibilities

### Connector owns
- **LTI 1.3 launch + OIDC** — `connector/lti/`. Handles `/lti/login`,
  `/lti/launch`, JWKS, `/lti/config.json`.
- **Per-instructor Canvas OAuth** — `connector/canvas/user_oauth.py` and
  `connector/api/canvas_oauth.py` implement the flow Canvas Cloud requires
  for general REST access.
- **Canvas REST client** — `connector/canvas/client.py` wraps the Canvas API
  the integration touches (files, pages, modules, discussions, conversations).
- **File watcher** — `connector/workers/canvas_watcher.py` polls watched
  courses, discovers documents, prepares each for Iris
  (`connector/canvas/iris_jobs.py`: Word/PowerPoint → PDF with LibreOffice,
  page count, cost reservation, cut into parts of at most 25 pages) and
  uploads the parts.
- **Bridge** — `connector/workers/reflow_bridge_worker.py` uploads parts Iris
  could not take yet, polls each part's session, and when all are ready
  stitches them (`connector/canvas/iris_output.py`), lifts figures out of the
  source PDF (`connector/canvas/pdf_figures.py`), uploads them into a course
  folder, and creates or updates a Canvas Page.
- **Canonical content** — `connector/canvas/content.py` is the one place
  every surface (Canvas Page, alt formats, WCAG score, editor, review) gets
  a job's accessible document from. For Iris jobs that is the stitched body
  stored in Redis; Iris is never called to show a document.
- **Panorama overlay** — `connector/web/canvas_review/panorama.js` is the
  Theme-Editor-injected JS that paints accessibility dials onto Canvas's
  native file listings. `connector/api/canvas_panorama.py` is the server
  side: per-document metadata, alt-format downloads, faculty
  approve/reject actions, WCAG checks.
- **Review notes** — `connector/canvas/iris_notes.py` turns what Iris says
  about its own output (failed pages, pages that failed its fidelity check,
  open issues) into the review screen's "Check before approving" list.
- **Per-job Canvas-side state** — Redis keys under `eq-pdf:lti:*` and
  `eq-pdf:canvas:*`, namespaced per tenant via `connector/canvas/tenant.py`.
- **Alt-format generators** — `connector/canvas/alt_formats.py` renders
  ePub, OCR'd PDF, plain text, Polly audio, AI translation from the canonical
  accessible HTML.
- **Automated WCAG checks** — `connector/canvas/wcag_checks.py` runs against
  generated HTML before publication.

### Iris (upstream) owns
- Document conversion: per-page vision extraction, verification and
  correction, specialist agents (math, charts, ...), assembly, axe-core
  lint, and the reader / copy-editor review loop.
- Its sessions, on its own box, under its own retention setting.
- The public API (`/v1/*`) the connector calls. See
  [IRIS_API.md](IRIS_API.md) for the exact endpoints.

What Iris does NOT do, that Reflow Core did: no PII scan (so no
`awaiting_approval` pause for Iris jobs), no Word/PowerPoint input, and no
image files in its output (figures come from the source PDF instead).

The connector imports **nothing** from Iris. The seam is HTTP.

## Data flow

A typical happy path:

1. Faculty member uploads a PDF to a Canvas course (or copies one into the
   drop folder).
2. `canvas_watcher` discovers it on its next tick (once the course owner has
   accepted the current faculty disclaimer), downloads it, and if
   these exact bytes were not converted before: counts pages, reserves the
   estimated cost (pages × `IRIS_ESTIMATED_COST_PER_PAGE_CENTS`) against the
   course's monthly cap, writes a `CanvasJob` (`backend="iris"`) with one
   entry per part, and uploads each part with `POST /v1/sessions`.
3. `reflow_bridge_worker` polls `GET /v1/sessions/{id}` for every part each
   `IRIS_POLL_SECONDS`. While any part is queued or running the job is
   marked active, so the stale-job sweeper leaves it alone.
4. When every part is `ready_for_review`, the bridge fetches each part's
   HTML, stitches them in page order (ids of later parts prefixed),
   sanitizes the result and stores it at `eq-pdf:canvas:iris-body:{job}`,
   with the figure manifest, Iris's report and token usage on the job.
5. The bridge lifts each described figure out of the source PDF, uploads
   it to the course's figure folder, renders the canonical HTML (figures
   pointing at Canvas files; unmatched ones shown as their descriptions),
   creates/updates the Canvas Page unpublished, and moves the job to
   `awaiting_review`.
6. Faculty reviews in Canvas or the LTI tool, with Iris's notes listed
   first. `/canvas/panorama/approve/{job_id}` runs WCAG checks; if clean (or
   waived), the Page publishes and students see it. Approving a document
   faculty did not edit also closes its Iris sessions.

## Redis key shape

```
eq-pdf[:t:{tenant}][:p:{platform_id}]:<suffix>
```

`canvas/tenant.py::tk()` builds these. `tenant` defaults to `default` (legacy,
unprefixed). `platform_id` is added only when the call passes `platform=`,
sandboxing per-Canvas-instance data when one connector deployment serves many
institutions.

Several workers hold copies of the same job at once (the watcher, the
bridge, the stale-job sweeper, the API). `state.put_job` therefore writes
back only the fields its holder changed since reading the job, merged onto
the record as it is now under `WATCH`, so a stale copy can never undo
another writer's change (for Iris, forgetting a recorded session would mean
uploading and paying for that part again). A deleted job is not recreated,
and `only_if` makes a write conditional (the sweeper fails a job only if it
is still `processing`).

Important suffixes:

| Suffix | Purpose |
|---|---|
| `canvas:job:{id}` | A converted-document job's connector-side state (`backend`: `iris` or legacy `reflow`) |
| `canvas:iris-body:{id}` | Iris jobs: the stitched, sanitized document body |
| `canvas:legacy-markdown:{id}` | Reflow jobs: migrated copy of Reflow's markdown |
| `canvas:iris-activity:{id}` | Iris jobs: "Iris is working on this" marker (TTL) for the sweeper |
| `canvas:iris-upload-backoff` | Iris asked us to slow down (or is down): uploads paused until it expires |
| `canvas:iris-upload-lock:{id}` | One worker uploads a job's parts at a time |
| `canvas:iris-figures-retry:{id}` / `canvas:iris-figures-attempts:{id}` | Iris jobs: wait between, and count, attempts at uploading figures |
| `canvas:file:{course_id}:{file_id}` | Watcher discovery cache + job pointer |
| `canvas:state:{nonce}` | LTI OIDC state, single-use |
| `canvas:session:{session_id}` | Faculty LTI session payload |
| `lti:platform:{platform_id}` | `PlatformInstall` records |
| `lti:platforms` | Set of known platforms |
| `lti:course:{course_id}:owner` | Whose OAuth token authorises scans of the course |
| `canvas:user_oauth:*` | Per-instructor OAuth access + refresh tokens |
| `canvas:audit:*` | Append-only approval/decision audit log |

## Alt-format pipeline

The canonical accessible representation is the HTML body from
`connector/canvas/content.load_rendered`: Iris's stitched HTML for Iris jobs,
or for legacy jobs the HTML `canvas/markdown_to_html.render` makes from
Reflow's markdown. Every other alt-format derives from that same
RenderedPage, which carries the document language Iris reported:

- **HTML / HTML with math** — `alt_formats.html_full_document`. Iris writes
  math as MathML Core (with the LaTeX in `alttext`), which browsers render
  natively. `alt_formats.has_math_content` detects MathML, LaTeX delimiters
  and `\ce{}`/`\pu{}` mhchem markup (legacy Reflow documents); on a hit, the
  rendered page loads MathJax with the mhchem extension. Inline-`$...$`
  regex is tightened so `$5 and another $7` prose doesn't trigger MathJax.
- **Plain text** — tag-strip, after MathML is read out as its LaTeX
  (`iris_output.mathml_to_latex`); stripping MathML's tags alone would
  leave `x²` as "x 2". Print-page rules are dropped.
- **Markdown** — Iris jobs: `iris_output.html_to_markdown` (markdownify),
  math as `$...$`/`$$...$$`, print pages as `[Page N]`. Legacy jobs:
  Reflow's own markdown, passed through.
- **ePub** — `ebooklib` (EPUB3), in the document's language.
- **Searchable PDF (auto-route)** —
  `canvas/alt_formats.pdf_has_text_layer` decides. Image-only scans
  go through `ocrmypdf`. Born-digital PDFs go through WeasyPrint with
  `pdf_variant='pdf/ua-1'` for a real Tagged PDF (structure tree,
  reading order, alt text). MathML is turned into LaTeX first, then math
  is pre-rendered to inline SVG via `canvas/math_render`, because
  WeasyPrint neither runs JavaScript nor lays out MathML.
- **Audio MP3** — Amazon Polly neural TTS, chunked at ~2800 chars per
  Polly call.
- **Translate** — Anthropic Claude (Sonnet 4.5) via the Messages API, from
  the same LaTeX-in-text form as plain text. Prompt explicitly preserves
  LaTeX and `\ce{}` markup verbatim so equations don't get translated into
  the target language's prose. (This is the connector's own call, separate
  from the model provider Iris uses.)
- **Braille (BRF)** — liblouis: UEB for the prose and a math code for the
  math (see `canvas/braille.py`). Iris's MathML becomes LaTeX runs and its
  `<hr role="doc-pagebreak" aria-label="Page 214">` markers become the
  `[Page 214]` print-page markers the braille layout already paginates by.

### Figures

Iris reads rasterized pages and returns no image files: each `<img>` has a
description and a placeholder `src` naming its page (`page-3-chart.png`, the
page's position within its Iris session). Stitching renames each to
`figures/p{source page}-{n}.png` and records a manifest on the job.
`pdf_figures.extract_iris_figures` then matches each page's placeholders to
that page's embedded rasters in reading order — skipping scanned-page
backgrounds, tiny images and running-head furniture — and declines when the
match is ambiguous. Matched figures are uploaded to the course folder
`Iris Generated Images` (a course that already has `Reflow Generated Images`
keeps using that one); unmatched ones render as visible descriptions naming
the original page.
Vector graphics have no raster to lift and always render as descriptions.

The `/canvas/panorama/alt/{job}/figures/{ref}` route serves a figure's
Canvas copy, or lifts it from the PDF on demand. For legacy Reflow jobs it
works as before: clean figure bytes pulled from the source PDF, with
Reflow's S3 PNGs (which carry a vision-pipeline grid overlay) as the
last-resort fallback while Reflow is still configured.

## Security model

State-changing requests need to clear, in order:

1. **LTI session cookie** (`reflow_lti_session`) — set by `/lti/launch`,
   carries the user id, course id, and roles.
2. **CSRF token** (`X-CSRF-Token` header, fetched via
   `/canvas/panorama/csrf`) — HMAC over the session id with
   `CSRF_SECRET_KEY`.
3. **Trusted-origin gate** — request's Origin (or Referer base) must be
   in `CANVAS_ALLOWED_ORIGINS` or match `CANVAS_ALLOWED_ORIGIN_REGEX`.
4. **Rate limit** — per `(endpoint, user_id)`. Redis-backed fixed
   window. See [OPERATIONS.md](../OPERATIONS.md#rate-limiting) for the
   table.
5. **Role check** — Instructor / TA / ContentDeveloper / Admin for
   approve/reject/edit/PII paths.
6. **Course check** — the LTI session's `course_id` must match the
   job's `canvas_course_id`.

Instructor OAuth tokens are encrypted at rest with AES-GCM
(`canvas/privacy.py`). The encryption key derives from
`TOKEN_ENCRYPTION_KEY`; if unset it falls back to a derivation of
`CSRF_SECRET_KEY`; if both are unset, a hardcoded constant — the
connector logs `CRITICAL` once per process in that mode, plus a
boot-time audit line. Production deployments set both keys.

The publication gate (`REQUIRE_WCAG_GATE=true`) makes WCAG-error
findings and the 4-item reviewer checklist a hard prerequisite for
the approve handler. When off, both run but failures are advisory.

## Multi-tenant model

One connector deployment can serve many Canvas instances at once. Two layers
of isolation:

- **Deployment tenant** — process-wide, set via `CANVAS_TENANT`. Picks the
  base Redis prefix. Used when one institution wants several logical
  deployments inside the same Redis (e.g. dev / staging / prod on shared
  infra).
- **LTI platform** — per call, set via `tk(suffix, platform=...)`. Sandboxes
  per-Canvas-instance data. Two Canvas instances managed by the same
  connector cannot read each other's per-platform records.

When `MULTI_TENANT_WATCHER=true`, the watcher iterates every registered
`PlatformInstall` and walks the courses associated with it via
`lti:platform:{pid}:courses`. When false, it just walks `CANVAS_WATCHED_COURSES`
under the single configured Canvas API token.

## Lifespan

`connector/main.py` runs both workers as background `asyncio.Task`s under the
FastAPI lifespan, sharing the Redis connection pool with request-time
handlers. Workers are gated on `LTI_ENABLED` so a connector running purely
behind a placement-less Developer Key (rare) doesn't burn cycles. On
shutdown the lifespan signals an `asyncio.Event` and waits up to 10 s
before cancelling.

## What lives where

```
connector/
├── main.py                 # FastAPI app + worker lifespan
├── config.py               # Settings (env-driven, pydantic-settings)
├── dependencies.py         # Singleton Redis pool + get_redis_client
├── logging_setup.py        # Context-aware JSON/text logging
├── lti/                    # LTI 1.3
│   ├── routes.py           # /lti/login, /launch, /jwks, /config.json
│   ├── jwt_validation.py   # Validate Canvas-issued launch JWTs
│   ├── keys.py             # RSA keypair + JWKS doc
│   ├── platform.py         # PlatformInstall, URL derivation
│   ├── platform_store.py   # Per-platform Redis upsert + index
│   ├── session.py          # OIDC state + post-launch session
│   └── config.py           # Thin view over global Settings
├── canvas/
│   ├── client.py           # Canvas REST API wrapper
│   ├── oauth.py            # LTI Advantage service-token mint
│   ├── user_oauth.py       # Per-instructor Canvas OAuth
│   ├── iris_client.py      # Equalify Iris HTTP client (the seam)
│   ├── iris_documents.py   # Office -> PDF, page counts, cutting into parts
│   ├── iris_jobs.py        # Create / upload / poll / complete an Iris job
│   ├── iris_output.py      # Stitch Iris HTML, figures manifest, markers
│   ├── iris_notes.py       # Iris's own warnings as review notes
│   ├── content.py          # The canonical document for any job
│   ├── consent_gate.py     # May this course's documents go to Iris?
│   ├── reflow_client.py    # Legacy: Reflow Core HTTP client
│   ├── state.py            # CanvasJob storage
│   ├── tenant.py           # Redis key namespacer (tk)
│   ├── alt_formats.py      # ePub, audio, OCR PDF, translation
│   ├── markdown_to_html.py # Canvas finishing for Iris HTML; legacy Reflow MD → HTML
│   ├── sanitize.py         # HTML allowlist for instructor edits
│   ├── wcag_checks.py      # Axe-core-style checks
│   ├── panorama.py         # Overlay dial data shapes
│   ├── signals.py          # Per-document conversion-quality signals
│   ├── privacy.py          # PII redaction + token encryption
│   ├── spend_cap.py        # Per-course AI spend cap
│   ├── audit.py            # Append-only audit log
│   └── errors.py           # CanvasApiError
├── api/
│   ├── canvas_consent.py   # Faculty disclaimer + opt-in
│   ├── canvas_oauth.py     # Canvas user-OAuth authorise + callback
│   ├── canvas_panorama.py  # Overlay JSON, dial badges, alt-formats, PII gate
│   ├── canvas_review.py    # Faculty review queue + per-doc page
│   └── _pii_approval_page.py  # HTML renderer for PII approval gate
├── canvas/
│   ├── pdf_figures.py      # Extract clean figure rasters from source PDF
│   ├── math_render.py      # LaTeX / mhchem → inline SVG for Tagged PDF
│   ├── verapdf_audit.py    # PDF/UA-1 audit subprocess wrapper
│   └── …                   # (other modules listed earlier in this file)
├── utils/
│   ├── rate_limit.py       # Redis-backed per-(endpoint, user) limiter
│   └── retry_helpers.py    # Exponential backoff for upstream calls
├── tools/                  # Operator CLIs (python -m connector.tools.*)
│   ├── generate_keys.py    # TOKEN_ENCRYPTION_KEY + CSRF_SECRET_KEY mint
│   ├── migrate_reflow_jobs.py # Copy Reflow-converted documents in before retiring Reflow
│   ├── retry_failed_jobs.py # Send documents that did not convert back to Iris
│   ├── reprocess_figures.py # Legacy: backfill PDF-extracted figures for Reflow jobs
│   └── …
├── workers/
│   ├── canvas_watcher.py   # Course poll + Iris submission
│   └── reflow_bridge_worker.py  # Iris poll + stitch + Canvas Page write
└── web/canvas_review/
    ├── panorama.js         # Theme-Editor overlay bundle (dial, modal, gate UI)
    ├── dashboard.html      # Instructor dashboard template
    ├── index.html          # Review queue template
    └── one.html            # Per-document review template (side-by-side)
```

## Tests

```
tests/
├── unit/           Pure-logic tests (markdown rendering, math detection,
│                   PDF figure matching, rate-limit math, structure-tree
│                   walking, PDF text-layer detection, ...)
└── integration/    End-to-end with fakeredis + real FastAPI app +
                    real LTI session + CSRF + rate-limit plumbing. Outbound
                    HTTP (Iris, legacy Reflow Core) mocked with respx;
                    Canvas faked in memory for the worker tests.
```

The integration suite specifically covers the wire contracts: the Iris
flow (upload, poll, stitch, figures, page write, throttling, recovery),
the approve-and-publish flow, and the legacy PII decision flow — the
latter two broke multiple times during the CSU East Bay pilot at
boundaries the unit suite couldn't see (wrong upstream URLs, missing CSRF
token, missing trusted-origin headers).
