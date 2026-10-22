# Equalify Iris API consumption

The connector is a CLIENT of [Equalify Iris](https://github.com/EqualifyEverything/equalify-iris)'s
HTTP API. It imports no Iris code; the seam is HTTP, the same shape the
Reflow integration had. This doc lists the endpoints it calls, the response
fields it depends on, and what it does with each failure. Iris's own
reference is its [`docs/API.md`](https://github.com/EqualifyEverything/equalify-iris/blob/main/docs/API.md).

Built and verified against Iris **1.3.0** (`GET /v1/health` reports the
running version).

All calls go through `connector/canvas/iris_client.py::IrisClient`. The base
URL is `IRIS_API_BASE_URL` (with or without a trailing `/v1`).

## Auth model

Iris has no user accounts. Every session belongs to the deployment, and an
operator chooses between two modes:

- **Gated** — Iris's `server.api_token` (its `IRIS_API_TOKEN`) is set. Callers
  send `Authorization: Bearer <token>`. The connector sends
  `IRIS_API_TOKEN` from its own `.env`.
- **Open** — the token is blank. Anyone who can reach Iris can call it,
  including `GET /v1/sessions`, which **lists every session**; a session id
  is all `GET /v1/sessions/{id}/output` needs. On a deployment that converts
  course documents this means any document is readable by anyone who can
  reach the port.

Run Iris gated, and keep it off the public internet: the connector is its
only legitimate caller. `scripts/preflight.py` and the startup audit both
probe `GET /v1/me` without credentials and report `CRITICAL` if Iris answers.

## Endpoints

### `GET /v1/health` (never gated)

`{"status": "ok", "service": "equalify-iris", "version": "1.3.0"}`. Used by the
startup audit and preflight.

### `GET /v1/me`

200 when the caller may use the API, 401 `unauthorized` when the deployment is
gated and the token is missing or wrong. Probed twice: once without a token
(an answer means Iris is open) and once with ours (a 401 means our token is
wrong).

### `GET /v1/limits` (never gated)

The connector reads `pdf.max_pages` (25 on Iris 1.3.0, a constant in Iris) to
size the parts of long documents, and logs `tagged_pdf`. Cached for 10
minutes. `IRIS_MAX_PAGES_PER_SESSION` can lower, never raise, the page count.

### `POST /v1/sessions`

`multipart/form-data` with exactly one part named **`images`** holding one
PDF. Iris rasterizes the PDF before it answers, so this call uses the longer
`IRIS_UPLOAD_TIMEOUT_SECONDS`.

Response `201`:

```json
{ "session_id": "ses_01HXYZ...", "status": "queued", "image_count": 25, "created_at": "..." }
```

**Not retried blindly.** A POST that reached Iris may already be converting,
and a repeat would convert (and bill) those pages twice. Iris has no
idempotency key, so the connector sorts failures by what they say about the
request (`IrisClient` sets `may_have_reached` on the error):

| Failure | What it means | Connector does |
|---|---|---|
| 429, 503 | Iris is throttling us or busy | Pauses all uploads for `Retry-After`; the part stays pending. |
| Could not connect, the request was not fully sent, or a gateway 502 | Nothing was created (Iris is down) | Pauses uploads 60 s; the part stays pending, however long the outage. |
| 401 / 403 | Our token is wrong | Pauses uploads, logs the credential mismatch. |
| Timed out waiting for the answer, connection cut after sending, 500, 504 | The session **may** exist anyway | Uploads pause 2 minutes, then 10; at most 3 attempts per part, then the job fails ("Iris did not confirm receiving these pages"). Each repeat can leave an unused session in Iris until its retention deletes it. |
| 400, 413, 422 | Iris refused the part itself | The job fails with Iris's message. |

Attempts are counted before the request is sent, so an upload cut short by
a restart or deploy counts too. A job deleted while its parts upload (the
file was removed, an erasure) stops the remaining uploads.

Every part is uploaded under the document's own file name: Iris shows each
page to its model as "&lt;name&gt;, page N of M", and a name carrying "pages
26-50" would invite the model to number its image placeholders from 26.

### `GET /v1/sessions/{id}`

```json
{ "session_id": "ses_...", "status": "running", "phase": "review",
  "iterations_completed": 1, "iterations_max": 3, "image_count": 25,
  "created_at": "...", "updated_at": "...", "error": "only when failed" }
```

`status` is `queued`, `running`, `ready_for_review`, `closed` or `failed`. The
bridge polls every part each tick (`IRIS_POLL_SECONDS`):

| Iris says | Connector does |
|---|---|
| `queued` / `running` | Marks the job active (so the stale-job sweeper leaves it alone) and waits. |
| `ready_for_review` / `closed` on every part | Fetches every output and completes the job. |
| `failed` on any part | Fails the job with Iris's `error` for every failed part, naming their pages. Not polled again until someone asks for the document again ("Try again"). |
| `expired_at` present, or 404 `session_not_found` / 410 `session_expired` (Iris's own error codes; any other 404 is reported, not acted on) | Iris lost the part: its retention deleted the files (the status stays `ready_for_review` with `expired_at`; only `/output` answers 410), or its data was wiped. While the document is wanted (`processing`) the part is uploaded again, at most twice; otherwise it is recorded as failed. |

Every part is read on every poll, so a document with several failed or lost
parts records all of them at once and one "Try again" re-sends them all.

### `GET /v1/sessions/{id}/output`

`text/html`, 409 until the session is `ready_for_review`, 410 once Iris's
retention deleted the files (handled like a lost part). A whole document;
the connector keeps only `<main>`'s content and `<html lang>`, and reads
Iris's comment markers before the sanitizer strips them:

| Marker | What the connector records |
|---|---|
| `<!-- @page-failed N ... -->` | `iris_report.failed_pages` (source-PDF pages) and the reason |
| `<!-- @page-uncorrected N, M ... -->` | `iris_report.uncorrected_pages` |
| `<!-- @unresolved - ... -->` | `iris_report.unresolved` (the issue list) |
| `@editor-truncated`, `@lint-unavailable`, `@review-unread` | flags on `iris_report` |

These become the "Check before approving" notes on the review screen
(`GET /canvas/review/{job}/notes`) and `notes` on the overlay's score payload.

**Images.** Iris reads rasterized pages, so it cannot return image files.
Each `<img>` carries alt text and a placeholder `src` naming its page, e.g.
`page-3-chart.png` (the page's position in the session). The connector
renames each to `figures/p{source page}-{n}.png`, lifts the matching image out
of the source PDF (`pdf_figures.extract_iris_figures`), uploads it to the
course's figure folder, and points the `src` at the Canvas file. An image it
cannot match unambiguously is shown as its description instead of a broken
picture, naming the page of the original PDF where the picture is. On a
two-column page the images are taken down the left column first, the way
Iris reads the page; when the page has no text to show its layout and the
order is in doubt, the page's images are shown as descriptions. A figure
whose upload to Canvas fails is tried again after 15 minutes, and after four
attempts that upload nothing at all it is shown as its description; any
attempt that uploads a figure starts the count again.

The route `/canvas/panorama/alt/{job}/figures/{ref}` only redirects to a
figure's Canvas copy, and for an unpublished document only for an instructor
session in its course. It never lifts figures from the PDF on demand: it
needs no login, and that would let anyone holding a job id make the
connector download the PDF with an instructor's token.

**Math** arrives as MathML Core with the LaTeX in `alttext`; **print pages**
as `<hr role="doc-pagebreak" aria-label="Page 214">`. Both are kept in the
canonical HTML (browsers and Canvas render them) and converted for the alt
formats that cannot (see `docs/ARCHITECTURE.md`).

### `GET /v1/sessions/{id}/diagnostics`

Read once per part at completion. The connector sums `tokens.input`,
`tokens.output`, `tokens.cache_read`, `tokens.cache_write` and
`model_calls.count`, and records the models from `by_agent.*.models`, as
`iris_usage` on the job. Iris reports tokens and deliberately not dollars; if
`IRIS_PRICE_*_PER_MTOK_USD` are set the connector also records `cost_cents`
and trues up the per-course spend counter. Best-effort: a missing diagnostics
answer never blocks the document.

### `POST /v1/sessions/{id}/close`

Iris treats closing as "a human accepted this output". It deletes the
session's scratch files and copies, for each agent that worked on the
document, one source page image and its accepted HTML into its regression
fixtures (`data/fixtures/<agent>/`, the newest five per agent), which it uses
to check future agent updates from its feedback loop. Those copies are kept
outside the session folder, so `storage.session_retention_days` does not
delete them.

The connector never uses that feedback loop, so closing is **off by
default** (`IRIS_CLOSE_SESSIONS_ON_APPROVE=false`). Turned on, the connector
closes a document's sessions when faculty **approve it without editing it**;
an edited document is left open, because what the faculty member accepted
was their correction, not Iris's output. 409 (already closed) and 410
(expired) are ignored, and it runs off the request path, so approval never
waits on Iris.

## Retention on the Iris side

Iris keeps every session's uploaded PDF, page images and output under
`data/sessions/<id>/` **until `storage.session_retention_days` deletes
them; blank (the default) keeps them forever.** The connector stores its
own copy of each finished document and never reads a session again after
it completes, so a short retention (for example 30 days) costs nothing. Set
it on any Iris that converts course documents.

## Long documents

Iris converts at most `pdf.max_pages` pages per session. A longer PDF is cut
into balanced consecutive parts (26 pages at a cap of 25 becomes 13 + 13), one
session each, and the parts' HTML is stitched in page order. Iris names
footnotes by their page within a session (`p1-fn-1`), so the same id turns up
in several parts: an id an earlier part already used is renamed
(`part2-p1-fn-1`) together with the links, `headers` and ARIA references in
its own part that point at it. Every other id keeps its name, and a link to
an id its part does not define (a contents list in part 1, endnotes at the
back of the book in part 2) is pointed at the part that does. Iris's reviewer
reads one part at a time, so content that crosses a seam (a table, a
sentence) is not joined; the review notes name the seams.

`IRIS_MAX_DOCUMENT_PAGES` (default 300) caps what the watcher sends at all.

## Throttling and outages

Iris rate-limits by caller address; the connector is one caller.
`upload_per_minute` is 12 by default.

- **429 on upload** — the part stays pending, and the connector pauses all
  uploads for Iris's `Retry-After` (a Redis key both workers honor). The
  watcher stops creating new jobs while paused; files it did not reach are
  picked up on a later tick.
- **Iris unreachable / 5xx / 401** — same pause (60 s), logged. A 401 is
  logged as a credential mismatch.
- **Stale jobs** — the sweeper fails a job only when Iris showed no activity
  on it for `canvas_stale_job_max_age_seconds` (default 1 h), and only if
  nothing moved it on meanwhile. Long documents waiting their turn in Iris's
  queue are active, not stale. A job failed this way is still checked each
  tick and resumes on its own if Iris comes back with its sessions.

## Asking again

"Try again" on the Accessible Documents page (instructors, for documents
that did not convert), the overlay's convert route, and
`python -m connector.tools.retry_failed_jobs` (operators, in bulk) share one
rule (`iris_jobs.requeue_job`): a conversion that failed part-way re-sends
only the parts that failed or were lost, after reserving their cost against
the course's monthly cap; a document that never started is handed back to
the watcher. Sending anything to Iris requires the requester (or, for
background work, the course owner) to have accepted the current faculty
disclaimer.

## Out of scope for the connector

Deliberately not used:

- `GET /v1/sessions` — the connector keeps its own session ids per job and
  never needs the deployment-wide list.
- `POST /v1/sessions/{id}/feedback` — re-running with feedback. Faculty edit
  the HTML in the connector instead. A natural follow-up for "Request edits".
- `GET /v1/sessions/{id}/fields`, `POST /v1/sessions/{id}/pdf` — the optional
  tagged-PDF route (needs `equalify-iris-pdf`). The connector's own tagged PDF
  (WeasyPrint) is still used.
- `GET /v1/sessions/{id}/logs`, `/v1/stats`, `/v1/quality`, `/v1/suggestions`.

## What Iris does not do that Reflow did

- **No PII scan.** Reflow Core ran Presidio and could pause a document for a
  privacy decision. Iris has no equivalent, so Iris documents never enter
  `awaiting_approval`; the faculty disclaimer (consent version 2.0) says so.
- **PDFs only.** Reflow (Docling) read Word and PowerPoint directly. The
  connector now converts them to PDF with LibreOffice first.
- **No image files.** See Images above.
