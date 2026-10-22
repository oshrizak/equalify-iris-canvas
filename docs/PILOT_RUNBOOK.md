# Pilot runbook

Steps for running a Canvas pilot end-to-end against the connector, plus the
failure modes that show up most often during the first week.

## Pre-flight

Before the first faculty launch:

1. **Iris is reachable, gated, and takes our token.**
   `docker compose run --rm connector python scripts/preflight.py` checks all
   three. By hand, from the connector host:
   - `curl $IRIS_API_BASE_URL/v1/health` returns
     `{"status":"ok","service":"equalify-iris",...}`;
   - `curl -o /dev/null -w '%{http_code}' $IRIS_API_BASE_URL/v1/me` returns
     **401**. A 200 means Iris is open: anyone who can reach it can read
     every document it converted. Set Iris's `IRIS_API_TOKEN` before going on;
   - the same with `-H "Authorization: Bearer $IRIS_API_TOKEN"` returns 200.
2. **Iris is not on the public internet.** From a machine off your network,
   `https://<your public host>/v1/health` must not answer as Iris. Check
   reverse-proxy catch-all rules in particular.
3. **Iris has a working model.** Run the smoke test below and read the
   HTML it prints. A session that fails straight away usually means the
   model provider's credentials or model names are wrong; Iris's logs say
   which.
4. **LTI keypair exists.** `ls keys/` should show `lti_private.pem` and
   `lti_public.pem`. If not, `./scripts/generate_lti_keys.sh`.
5. **Canvas Developer Keys configured.** See [CANVAS_SETUP.md](CANVAS_SETUP.md).
6. **`CANVAS_WATCHED_COURSES`** lists the course IDs you want scanned (single
   tenant), OR `MULTI_TENANT_WATCHER=true` (multi-tenant — watcher iterates
   registered platforms).

## Local end-to-end smoke

Easiest verification the connector talks to a running Iris: a two-page
PDF through one session, start to finish (a few minutes at most with a real
model).

```bash
docker compose up -d
docker compose exec connector python -c "
import asyncio
import pymupdf
from connector.canvas.iris_client import IrisClient

doc = pymupdf.open()
for n in (1, 2):
    doc.new_page().insert_text((72, 100), f'Smoke test page {n}. The quick brown fox.')
pdf = doc.tobytes()

async def main():
    iris = IrisClient()
    print('iris', (await iris.health()).get('version'))
    sid = (await iris.create_session('smoke.pdf', pdf))['session_id']
    print('session', sid)
    while (status := (await iris.get_session(sid))['status']) in ('queued', 'running'):
        await asyncio.sleep(5)
    print('status', status)
    if status == 'ready_for_review':
        print((await iris.get_output(sid))[:400])
    await iris.close_session(sid)

asyncio.run(main())
"
```

A healthy setup prints Iris's version, a `ses_...` id, `status
ready_for_review` and the start of the HTML. On `failed`, Iris's own logs
(and the `error` field of `GET /v1/sessions/{id}`) say why.

## Common failure modes

### Watcher log: `Canvas watcher started with no watched courses; idling`

Expected when `CANVAS_WATCHED_COURSES=` is empty and
`MULTI_TENANT_WATCHER=false`. Either populate the list or flip the flag.

### Watcher log: `Iris uploads paused for 60s: ...`

The connector could not reach Iris, or Iris answered 401/5xx. New documents
wait (nothing is lost: files the watcher did not reach are picked up on a
later tick, and parts already recorded are uploaded by the bridge). Run the
pre-flight checks above. `Iris refused our credentials (HTTP 401)` in the
same log means `IRIS_API_TOKEN` differs from Iris's.

### Watcher log: `Iris uploads paused for <n>s: 429 ...`

Iris's upload rate limit (12 a minute by default, per caller; the
connector is one caller). Expected on a bulk day: the pause follows Iris's
`Retry-After` and the queue drains on its own. Raise Iris's
`rate_limits.upload_per_minute` if it is the steady state.

### A document fails with "This document has N pages, more than the 300-page limit"

`IRIS_MAX_DOCUMENT_PAGES` guards spend. Raise it (0 = no limit) and use
"Try again" on the Accessible Documents page, or split the document.

### A document fails with "Iris could not convert pages X-Y of this document: ..."

Iris failed that part's session; the text after the colon is Iris's own
`error`. Common causes: the model provider (credentials, quota, a model
that cannot read images) or a page Iris could not finish. Fix the cause,
then "Try again" on the Accessible Documents page re-sends only the parts
that failed. After an outage that failed many documents, retry them all at
once:

```bash
docker compose exec connector python -m connector.tools.retry_failed_jobs --dry-run
docker compose exec connector python -m connector.tools.retry_failed_jobs --since 2026-10-20
```

### A document fails with "Iris did not confirm receiving these pages after 3 attempts"

Three uploads of the same part ended without an answer (timeouts, a cut
connection, Iris or a gateway erroring after the upload arrived). Each may
have created a session anyway, so the connector stops there. Check Iris's
logs and `IRIS_UPLOAD_TIMEOUT_SECONDS` (Iris rasterizes the part before it
answers; a slow box with large pages can need more than 300 s), then use
"Try again".

### A document fails with "Iris showed no progress on this document for more than N minutes"

The stale-job sweeper gave up after `canvas_stale_job_max_age_seconds`
(1 h) without any sign of activity from Iris: Iris was down or lost its
data. The bridge keeps checking such jobs and resumes them by itself if
Iris reports progress; otherwise "Try again" starts again.

### Watcher log: `not converting course <id> until its owner (<user>) accepts the current disclaimer`

Expected right after the upgrade. The disclaimer changed (Iris runs no PII
scan), and background conversion in a course acts for its owner, the
instructor whose Canvas token the watcher uses. Nothing is lost: the course
resumes on its own once that instructor opens the tool and accepts the new
text. Ask owners of active courses to launch the tool once.

### Pictures show as "Image (page N of the original PDF): ..." text

Iris describes images but cannot hand them back, so the bridge lifts each
picture out of the source PDF. When it cannot tell which picture on a page a
description belongs to, or the picture is drawn with vector graphics rather
than embedded, the page shows the description as text instead of guessing.
Bridge log: `figures — X uploaded, Y shown as descriptions`. Faculty can
put the picture back in the page editor.

### Watcher log: `Skipping Word/PowerPoint files: ... LibreOffice (soffice) is not installed`

The image was built with `WITH_LIBREOFFICE=false`, or
`IRIS_CONVERT_OFFICE_DOCUMENTS=false`. Rebuild with LibreOffice; skipped
files are converted on the next tick after that.

### Legacy (Reflow-era documents): `failed to fetch markdown … host not reachable`

Only while `REFLOW_API_BASE_URL` is still set for the migration. Reflow
Core returned a presigned URL pointing at a hostname the connector can't
reach. Set `S3_INTERNAL_URL` to the hostname the connector can reach (e.g.
`http://floci:4566` inside Docker) and `S3_PUBLIC_URL` to the host the URL
was issued against (e.g. `http://localhost:4566`). Better: run
`connector.tools.migrate_reflow_jobs` and retire Reflow Core (see
[MIGRATING_FROM_REFLOW.md](MIGRATING_FROM_REFLOW.md)).

### Legacy: PII decision returns 409 "not waiting for a privacy decision"

Iris runs no PII scan, so only Reflow-era documents can be waiting on a
privacy decision, and only while Reflow Core is still configured. An old
link from a Reflow-era notification returns this once the document has
moved on, and so does any Iris document. Expected.

### "Unexpected deployment_id" on every launch

You re-registered the LTI tool in Canvas. `LTI_DEPLOYMENT_ID` in `.env`
no longer matches what Canvas is sending. Either set it to the new
deployment value (read it from Canvas's tool placement) and migrate
the per-platform Redis records to the recomputed `platform_id`, or
revert to the old deployment in Canvas if still available. See
`OPERATIONS.md` "Common breakage modes" for the migration outline.

### `invalid_scope` on every Canvas write

The Canvas Developer Key is missing scopes the bridge needs (page
create/update, file upload, conversations). Add the four `url:POST|...`
/ `url:PUT|...` scopes in the Canvas API key (not the LTI key), then
force faculty to re-consent — stored OAuth tokens only carry the
scopes the key had at consent time. Wipe `eq-pdf:lti:user-token:*` to
force the consent screen on next launch.

### `/canvas/panorama/...` returns 401 "No LTI session"

Faculty cookie expired or the panorama overlay reached the endpoint
without a `reflow_lti_session` cookie. Have the faculty re-open the
"Accessible Documents" tool from the course navigation to refresh.

### Canvas Page write returns `invalid_scope`

The OAuth token the bridge is using does not carry
`url:POST|/api/v1/courses/:course_id/pages`. Cause is usually that the
Canvas **API** Developer Key (not the LTI key) wasn't toggled with
"Enforce Scopes" + the Pages scope. Fix on the Canvas side, then the
bridge self-heals on the next tick — no manual replay needed because
the bridge keeps `page_failed` jobs in its pollable set.

### `Bridge: job <id> exceeded 180s; moving on`

A single bridge tick on a job exceeded the per-job timeout. Logged but
non-fatal — the next tick picks it up, and figures already uploaded are
kept. If a job hits this repeatedly, inspect Canvas API latency or the size
of figure uploads. (Uploads to Iris run outside this cap: rasterizing a
25-page part can take a minute.)

### `Bridge: job <id> failed to drive; moving on`

A genuine exception in the bridge for one job. Inspect the logged
traceback. The tick continues with subsequent jobs, so a single broken
job never strands the queue.

## Monitoring

The connector exposes Prometheus metrics on `${METRICS_PORT}` (default
`8001`) when `ENABLE_METRICS=true`. Wire it into the Prometheus you already
run. Iris has its own operator views (`GET /v1/stats`, `/v1/quality`), and
the connector records each document's tokens and model calls on the job
(`iris_usage`).

Watch for:

- Time from upload to `awaiting_review`. Iris converts
  `defaults.max_concurrent_runs` documents at once (2 by default) and queues
  the rest, so a bulk day shows up as a growing queue, not as errors.
- `Iris uploads paused` warnings. Occasional 429 pauses on a bulk day are
  normal; repeated pauses for an unreachable Iris are an outage.
- Canvas API 429 / 5xx rate. The connector retries with exponential
  backoff, so individual blips are silent; a sustained climb shows up
  here.
- Worker tick duration. The watcher should finish a tick in seconds even
  with thousands of files (it scans incrementally). A tick longer than
  the configured `CANVAS_POLL_SECONDS` means the next one starts late.

## Rolling back

The connector keeps no irreversible state outside Canvas Pages it
created. To roll back a pilot:

1. Stop the connector (`docker compose down`).
2. Optionally, instruct the bridge to mark every connector-created Canvas
   Page unpublished by running a small one-off against
   `CanvasClient.unpublish_page` for each `canvas_page_url` recorded
   under `eq-pdf:canvas:job:*`.
3. Disable the LTI tool in Canvas (Developer Keys → toggle OFF).

Redis-stored Canvas job state can be retained for audit (default
`CANVAS_JOB_RETENTION_SECONDS=7776000`, i.e. 90 days). Approval audit
records have their own retention (`CANVAS_AUDIT_RETENTION_SECONDS`,
default 5 years).

## Before the first faculty launch — checklist

1. [`OPERATIONS.md`](../OPERATIONS.md#secrets-checklist-before-first-launch)
   — secrets, including `TOKEN_ENCRYPTION_KEY` and `CSRF_SECRET_KEY`.
   Generate with `docker compose run --rm connector python -m connector.tools.generate_keys`.
2. Watch the connector boot logs for `startup secrets audit: OK`. Any
   `CRITICAL` line means a required secret is still missing.
3. Schedule the Redis backup: cron entry calling
   `./scripts/backup-redis.sh` every 6 hours, with `BACKUP_S3_BUCKET`
   set if you want off-host durability. See
   [`OPERATIONS.md`](../OPERATIONS.md#redis-persistence--backups).
4. Decide on `REQUIRE_WCAG_GATE`. Off for the first few days of a
   pilot so faculty can move; flip to `true` once they're comfortable
   with the modal flow (the panorama overlay handles the 409 path
   natively — see the WCAG-gate UI section in the changelog).
5. Verify rate-limit thresholds suit the pilot's expected load —
   defaults are generous for typical faculty workflows but a bulk
   migration day might want a temporary bump. See
   [`OPERATIONS.md`](../OPERATIONS.md#rate-limiting).
