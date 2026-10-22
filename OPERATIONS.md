# Operations runbook

This document covers what an operator needs to know to keep the
connector running in production — secrets, backups, common breakage
modes, and the recovery procedures we worked out during the CSU East
Bay pilot.

Treat this as the canonical reference. If you fix something not
covered here, add it.

## Secrets checklist (before first launch)

Before exposing the connector to faculty, walk this list. Every empty
or placeholder value is either a 503 in the UI or a security finding.

| Variable | Required? | What breaks if missing |
|---|---|---|
| `IRIS_API_TOKEN` | **Yes** | Must equal Iris's own `IRIS_API_TOKEN` (`server.api_token`). Unset here and set in Iris: every upload 401s. Unset in both: Iris is open and serves every converted document to anyone who can reach it. The startup audit logs `CRITICAL` for all three cases (unset, Iris open, token rejected). |
| `IRIS_API_BASE_URL` | **Yes** (compose has a default) | Nothing converts; uploads pause and retry. Must be a private address: only the connector should be able to reach Iris. |
| `TOKEN_ENCRYPTION_KEY` | **Yes** | OAuth tokens (= instructor impersonation credentials) get encrypted with a hardcoded fallback key. Anyone with the source can decrypt your Redis dump. Generate with `python -m connector.tools.generate_keys`. |
| `CSRF_SECRET_KEY` | **Yes** | CSRF tokens signed with a derivation of the LTI keypair fingerprint — stable but not a secret. Same generator. |
| `LTI_CLIENT_ID` | **Yes** | Every LTI launch fails with `Unexpected audience` — pull from the Canvas Developer Key. |
| `LTI_DEPLOYMENT_ID` | **Yes** | Every launch fails with `Unexpected deployment_id` — pull from Canvas's tool placement. Different per Canvas install of the same key. |
| `LTI_PRIVATE_KEY_PATH` + the actual PEM at that path | **Yes** | JWT signing fails. Generate with `scripts/generate_lti_keys.sh`; mount under `/app/keys`. |
| `CANVAS_OAUTH_CLIENT_ID` + `CANVAS_OAUTH_CLIENT_SECRET` | **Yes** | Per-instructor OAuth consent flow can't start; bridge falls back to LTI service tokens which Canvas Cloud rejects for `/api/v1/...` writes. |
| `CANVAS_ALLOWED_ORIGINS` | **Yes** | The panorama overlay's `fetch` calls from Canvas's origin are blocked by CORS, so the dial loads but nothing works. Include EVERY Canvas host faculty might be on (production, beta, test). |
| `LTI_PUBLIC_URL` | **Yes** | Canvas redirects mismatch; the LTI config endpoint serves `http://` URLs Canvas refuses. Must be HTTPS in prod. |
| `REFLOW_API_BASE_URL` + `REFLOW_API_KEY` | Legacy: only while migrating Reflow-era documents | Unset on a fresh install. See [`docs/MIGRATING_FROM_REFLOW.md`](docs/MIGRATING_FROM_REFLOW.md). |
| `S3_PUBLIC_URL` + `S3_INTERNAL_URL` | Legacy: only with Reflow Core, if S3 hostnames differ inside vs outside Docker | Reflow-era figures and markdown fetch 404. Unused with Iris. |
| `REQUIRE_WCAG_GATE` | Should be `true` in prod | When false, the approve handler runs WCAG checks but does NOT enforce them — faculty can publish a page with `error`-severity findings. |
| `ANTHROPIC_API_KEY` | Only if Translate is visible | Clean 503 on Translate clicks. Leave empty to disable. |
| `AWS_DEFAULT_REGION` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | Only if Audio MP3 is visible | Clean 503 on Audio MP3 clicks. IAM user needs `polly:SynthesizeSpeech`. |

Run `git diff .env.example .env` after every dependency bump to catch
new keys you haven't filled in.

### Generating the cryptographic keys

```bash
docker compose run --rm connector python -m connector.tools.generate_keys
```

Prints `TOKEN_ENCRYPTION_KEY=…` and `CSRF_SECRET_KEY=…` lines using
`secrets.token_urlsafe(32)` (256 bits of entropy each). Paste them
into `.env` and `docker compose restart connector`.

The startup logs include a `startup secrets audit` line — `OK` when
everything's set, `CRITICAL` per missing secret otherwise. Grep for
it after every release to verify a production deploy.

### Key rotation

* **TOKEN_ENCRYPTION_KEY rotation** invalidates every encrypted OAuth
  token in Redis — faculty must re-consent on next launch. Plan a
  maintenance window and post a notice to faculty before rotating.
* **CSRF_SECRET_KEY rotation** invalidates outstanding CSRF tokens —
  expected; clients re-fetch on the next state-changing call. Safe to
  do in-place.

## Redis persistence + backups

### What's in Redis (= what we lose if it goes away)

Every state-bearing piece of the connector lives in Redis:

* `eq-pdf:canvas:job:<id>` — per-job state (status, page URL, VeraPDF
  audit, figure URLs, instructor user id, Iris session ids, Iris's report
  and token usage). Driving record for the dial.
* `eq-pdf:canvas:iris-body:<id>` — **the converted document itself** for
  every Iris job. Iris keeps its own copy only until its session retention
  deletes it, so after that this is the only copy outside the Canvas Page.
* `eq-pdf:canvas:legacy-markdown:<id>` — migrated copies of Reflow-era
  documents (see `migrate_reflow_jobs`).
* `eq-pdf:canvas:course:<id>:pending` — accessibility + PII review queue.
* `eq-pdf:canvas:course:<id>:processed` — idempotency marker so the
  watcher doesn't re-submit known files.
* `eq-pdf:lti:platform:<id>` — registered Canvas LTI tools.
* `eq-pdf:lti:user-token:<platform>:<user>` — instructor OAuth tokens.
* `eq-pdf:canvas:approval:audit` — append-only approval/PII audit log.
  **Legally sensitive** for accessibility complaints; retain per your
  records policy.

Losing Redis means losing every faculty consent, every approval, the
entire audit trail, and the converted documents behind every alt format.
Iris can convert the source documents again (at model cost), and
published Canvas Pages survive, but nothing can restore decisions.

### Persistence is now ON by default

`docker-compose.yml` runs Redis with:

* `--appendonly yes --appendfsync everysec` — every write durable
  within ~1 second of the request.
* Three RDB save points (default Redis snapshotting).
* Data dir mounted at the named volume `redis-data`.

`docker compose down` keeps the volume. **`docker compose down -v` wipes
everything**, including the audit log. Never run that on production
without a verified backup.

### Backup

Automated via the bundled script — schedule it on the host:

```bash
./scripts/backup-redis.sh
```

The script triggers a fresh `BGSAVE`, polls `LASTSAVE` until the
snapshot lands, `docker cp`s the new `dump.rdb` into `./backups/`
with a UTC timestamp in the filename, and prunes anything in that
directory older than `BACKUP_RETENTION_DAYS` (default 14).

For real off-host durability set `BACKUP_S3_BUCKET` in the
environment and have the AWS CLI on PATH with credentials:

```bash
export BACKUP_S3_BUCKET=s3://my-bucket/reflow-redis
./scripts/backup-redis.sh
```

Cron entry (every 6 hours, ample for the connector's write rate):

```cron
0 */6 * * * cd /path/to/equalify-iris-canvas && ./scripts/backup-redis.sh >> backups/backup.log 2>&1
```

Or as a systemd timer if you prefer a structured unit. Pair the S3
bucket with a 30-day lifecycle policy → Glacier transition so long-
term cost stays bounded.

The `redis-data` named volume on its own only survives container
restarts, not host failures — never skip the off-host upload in
production.

Manual one-shot (without the script) if you need to investigate:

```bash
docker compose exec redis redis-cli BGSAVE
# Wait for "Background saving terminated with success" in `docker compose logs redis`
docker cp $(docker compose ps -q redis):/data/dump.rdb ./backups/redis-$(date +%Y%m%dT%H%M%S).rdb
```

### Restore

1. Stop the connector and Redis: `docker compose down` (NOT `down -v`).
2. Copy the backup `dump.rdb` (or AOF directory) back into the
   `redis-data` volume.
3. `docker compose up -d redis` and watch the logs — Redis prints
   `DB loaded from append only file` (or the equivalent for RDB) on
   successful restore.
4. Bring the connector back up: `docker compose up -d connector`.

## Data flow (where student content goes)

Document this and pair it with your institutional privacy notice. Every
external service that sees document content is listed below.

| Service | What sees it | When | Region (default) | DPA |
|---|---|---|---|---|
| **Equalify Iris** (self-hosted) | Full PDF bytes (cut into parts of up to 25 pages), the page images it renders, the converted HTML, Word/PowerPoint files after LibreOffice turns them into PDFs | Always (every submission) | Wherever you deploy Iris | None beyond your own hosting if you run it. Iris keeps each session (PDF, page images, output) until `storage.session_retention_days` deletes it, and forever if that is blank: set it. With `IRIS_CLOSE_SESSIONS_ON_APPROVE=true`, closing also copies one page image per agent into Iris's regression fixtures, outside retention. |
| **Iris's model provider** (OpenRouter, another OpenAI-compatible endpoint, or Amazon Bedrock, as configured in Iris) | Page images, the text Iris extracts, and the prompts of every Iris agent | Every conversion | The provider's (OpenRouter forwards to the underlying model's provider) | Your agreement with that provider. Confirm its retention and no-training terms match what the faculty disclaimer says. |
| **Reflow Core** (legacy, only while migrating) | Full PDF bytes; markdown extraction output; figure rasters in S3 | Reflow-era documents only, and only while `REFLOW_API_BASE_URL` is set | Wherever your Reflow Core deploys | As before the switch. |
| **Anthropic Claude** | Plain-text body of the canonical accessible HTML, with LaTeX/`\ce{}` spans preserved | Only when faculty clicks **Translate** and `ANTHROPIC_API_KEY` is set | `https://api.anthropic.com` (US) | Anthropic's standard DPA covers this. |
| **AWS Polly** | Plain-text body of the canonical accessible HTML | Only when faculty clicks **Audio (MP3)** and AWS credentials are set | `AWS_DEFAULT_REGION` (you choose) | AWS standard DPA. |
| **Canvas** | Generated accessible HTML (as a Canvas Page), figure raster files (in a course folder) | Every bridge tick that successfully publishes | Wherever your Canvas Cloud / self-host lives | Already in place if you're using Canvas at all. |
| **Cloudflare** (if tunnel) | Encrypted in transit between your edge and Canvas; sees TLS-terminated request metadata | Whenever Canvas reaches the connector | Cloudflare edge | Your Cloudflare account DPA. |

The connector itself stores in Redis: instructor OAuth tokens
(encrypted at rest), approval audit log records, document state, the
converted document of every Iris job, and a short-lived cache of PDF
bytes during figure-proxy responses. On the `connector-data` volume it
keeps each approved document's tagged PDF from Iris (the original document
with tags); deleting, purging or erasing a job deletes it too. Word and PowerPoint files are turned
into PDFs inside the connector's own container (LibreOffice); no external
service sees them before Iris does.

Iris runs **no PII scan** (Reflow Core did, and could pause a document
for a privacy decision). The faculty disclaimer (consent version 2.0) says
so; documents with sensitive personal data must not go into watched
folders. Nothing reaches Iris on behalf of someone who has not accepted it:
background conversion in a course waits until the course owner (the
instructor whose Canvas token the watcher uses) accepts version 2.0, and
"Try again" requires the instructor clicking it to have accepted it.

To leave **Translate** and **Audio MP3** off entirely, simply don't set
`ANTHROPIC_API_KEY` and `AWS_ACCESS_KEY_ID`. The menu shows them as under
development and their URLs answer 404 with the same words — no document
content ever leaves the connector.

## Operator tools (`python -m connector.tools.*`)

Every state-touching CLI under ``connector/tools/`` writes a structured
``operator_action`` line into the same audit log faculty actions go
into (``reflow.canvas.audit``). The line carries ``os_user`` + ``host``
+ ``pid`` + whatever extra fields the tool reports. Grep
``operator_action`` to see every admin-level change made out-of-band.

| Tool | What it does | When you'd run it |
|---|---|---|
| ``generate_keys`` | Mints ``TOKEN_ENCRYPTION_KEY`` + ``CSRF_SECRET_KEY`` (32 bytes urlsafe each). Prints to stdout. | Before first launch; on any planned key rotation. |
| ``migrate_reflow_jobs [--dry-run] [--course-id <c>] [--requeue-unfinished]`` | Copies the markdown of every finished Reflow-era job into Redis so it keeps serving without Reflow Core; with ``--requeue-unfinished``, hands jobs Reflow never finished to Iris. Skips what it already copied. | Once, while switching from Reflow Core to Iris, before Reflow Core is shut down. See [`docs/MIGRATING_FROM_REFLOW.md`](docs/MIGRATING_FROM_REFLOW.md). |
| ``retry_failed_jobs [--dry-run] [--course-id <c>] [--since YYYY-MM-DD]`` | Sends documents that did not convert back to Iris: only the failed or lost parts of a conversion that failed part-way (their cost is reserved again), or the whole file when it never started. Skips courses whose owner has not accepted the current disclaimer, and lists them. | After fixing an Iris-side problem (model provider key, outage) that failed many documents. Faculty can do the same per document with "Try again" on the Accessible Documents page. |
| ``reprocess_figures --course-id <c>`` / ``--job-id <j>`` | Legacy (Reflow-era jobs, Reflow Core still configured): backfills PDF-extracted figures over Reflow's gridded copies; rewrites the Canvas Page to point at the refreshed file URLs. Idempotent. Iris jobs are skipped; their figures are lifted from the PDF from the start. | To fix specific Reflow-era jobs whose figures look wrong. |
| ``erase_user --user-id <uid>`` | Per-user data erasure for GDPR Article 17 / FERPA amendment. Deletes OAuth tokens, sessions, consent records, jobs; pseudonymises audit log rows. Defaults to dry-run; ``--commit`` applies. | When you receive an erasure request. |

The erasure tool is the right answer to a data-subject erasure
request against the connector's local state, including the converted
documents of the user's jobs. **Iris stores its own copies** (each
session's PDF, page images and output, plus any regression fixtures)
until its session retention deletes them; satisfying the request fully
before then means deleting the matching `data/sessions/<id>/` folders on
the Iris host (the session ids are in each job's `iris_parts`). For
Reflow-era jobs the same applies to Reflow Core while it exists. Document
the path in your institution's data-subject-rights procedure.

## Locking down the host file system

`.env` is a plaintext file on the host. Restrict it:

```bash
chmod 600 .env
chown root:root .env       # or whichever user runs docker compose
ls -l .env                 # expect: -rw------- 1 root root
```

The LTI private key (`keys/lti_private.pem`) wants the same treatment:

```bash
chmod 600 keys/lti_private.pem
```

The `redis-data` and `backups/` directories should be owned by the same
user that runs `docker compose`. A `umask 077` in the operator's shell
profile makes this automatic for any new files.

For a production deployment, replace `.env` file storage with a managed
secrets store (AWS Secrets Manager, HashiCorp Vault, Doppler, etc.) and
inject values via env vars at container start. The connector itself
doesn't care where the env comes from — only that it's set. Rotate
secrets per the policy in the "Key rotation" section above.

## Rate limiting

Every state-changing POST/PUT is rate-limited per ``(endpoint, user_id)``
via a Redis-backed fixed-window counter. The limits are picked well
above legitimate faculty workflow but well below abuse:

| Endpoint | Bucket | Limit | Window |
|---|---|---|---|
| ``POST /canvas/panorama/approve/{job}`` | ``approve`` | 30 | 60s |
| ``POST /canvas/panorama/reject/{job}`` | ``reject`` | 30 | 60s |
| ``POST /canvas/panorama/request-edits/{job}`` | ``request_edits`` | 30 | 60s |
| ``POST /canvas/panorama/unpublish/{job}`` | ``unpublish`` | 30 | 60s |
| ``POST /canvas/panorama/pii-decision/{job}`` | ``pii_decision`` | 10 | 60s |
| ``POST /canvas/panorama/convert/{file}`` | ``convert`` | 30 | 60s |
| ``POST /canvas/panorama/approve/_bulk`` | ``approve_bulk`` | 5 | 60s |
| ``PUT  /canvas/panorama/edit/{job}`` | ``edit`` | 60 | 60s |
| ``POST /canvas/review/{job}/approve`` | ``review_approve`` | 30 | 60s |
| ``POST /canvas/review/{job}/reject`` | ``review_reject`` | 30 | 60s |

When the limit is exceeded the response is **429 Too Many Requests**
with a ``Retry-After`` header set to the seconds until the next window
opens. The limiter logs a ``WARNING`` line every time it fires:

```
rate limit exceeded: bucket=approve actor=<user_id> count=31 limit=30 window=60s
```

Counters live at ``eq-pdf:rl:<bucket>:<actor>:<window_id>`` and
auto-expire 5 seconds after the window closes, so the namespace stays
tidy without a sweeper.

To raise a specific limit (e.g. course migration day with bulk
approvals), grep the relevant ``enforce_rate_limit`` call in
``connector/api/canvas_panorama.py`` or ``canvas_review.py`` and bump
the ``limit=`` value, then ``docker compose restart connector``.

## Common breakage modes (from CSUEB pilot)

### "Unexpected deployment_id" at every launch

You re-registered the LTI tool in Canvas. The `LTI_DEPLOYMENT_ID` env
var no longer matches the new deployment. Either:

* Update `.env`'s `LTI_DEPLOYMENT_ID` to the new value (read it from
  Canvas's tool placement) and migrate Redis data from the old
  platform_id to the new one (compute via
  `connector.lti.platform.compute_platform_id`). Migration script
  pattern lived in this session's chat — distill into a CLI under
  `connector/tools/` if you re-encounter this.
* Or revert to the old deployment if it still exists in Canvas.

### `invalid_scope` on every Canvas write

The Canvas Developer Key is missing scopes the bridge needs:

* `url:POST|/api/v1/courses/:course_id/pages`
* `url:PUT|/api/v1/courses/:course_id/pages/:url_or_id`
* `url:POST|/api/v1/courses/:course_id/files`
* `url:POST|/api/v1/conversations`

Add them in Canvas → Developer Keys → Edit → Enforce scopes, then
**force faculty to re-consent** (their stored OAuth tokens have only
the scopes from their consent time, not the current key state). Wipe
`eq-pdf:lti:user-token:*` to force the OIDC flow to redirect through
the consent screen on the next launch.

### "Failed to fetch" on PII Approve (legacy, Reflow-era documents)

Only Reflow-era documents can be waiting on a privacy decision; Iris
documents answer 409 "not waiting for a privacy decision". Reflow Core's PII gate is approval-token-based: `POST
/api/v1/approval/{approval_token}/decision`. The connector now hits
that route (see `submit_pii_decision` in `connector/canvas/reflow_client.py`).
If you see 405 from Core again, Core's surface drifted — confirm via
`curl <core>/openapi.json | grep approval`.

### File processed but no dial on the row

The panorama overlay reads `scored_files` keyed by FILENAME. If the
filename in Canvas doesn't match what the job recorded (e.g., faculty
renamed the file), the dial won't attach. Check
`docker compose exec redis redis-cli get eq-pdf:canvas:job:<id>` for
the stored `canvas_file_name` and compare to what the panorama
overlay sees on the page.

### Searchable PDF takes a long time

The first request for a document asks Iris to tag it, part by part: a few
seconds for a short born-digital PDF, a minute or more for 25 scanned
pages (Iris runs Tesseract to place the words). Approving a document makes
it in the background, so students normally get the kept copy at once. The
menu says "Preparing Searchable PDF…" while it waits and announces the
download or the reason it failed. The response header `X-Accessible-Pdf`
says which PDF was served: `iris-tagged`, or the connector's own
(`connector-edited`, `connector-html`, `connector-ocr`); the log line
"Searchable PDF for <job> is the connector's own: <reason>" says why Iris's
was not.

### Audit log too big

The bridge worker calls `purge_old_canvas_records` on a slow timer
with `CANVAS_AUDIT_RETENTION_SECONDS` as the cutoff. Default 5 years.
Tune up or down per your records policy. Setting it to 0 disables
the audit purge entirely (recommended unless retention is a legal
problem).

## Health + observability

* Container HEALTHCHECK hits `GET /health` every 30s. A failing
  health check restarts the container under `restart: unless-stopped`.
* No Prometheus `/metrics` endpoint yet (dependency is in
  `pyproject.toml` but the route isn't mounted). When you add it,
  expose it on a separate port so the public Cloudflare tunnel
  doesn't see it.
* No structured logs yet. Use `docker compose logs --since=10m
  connector | grep -E "(WARNING|ERROR)"` for fast triage.

## What still wants attention before scaling beyond one course

* **The overlay's listing endpoints are public.** `GET
  /canvas/panorama/scored_files`, `/score` and `/score_by_job` answer
  without a session for any `course_id`: file names, job ids, states and
  scores of every converted document in the course. The overlay calls them
  with `credentials: "omit"` because it runs for every Canvas user,
  students included. Document contents are not exposed (drafts need an
  instructor session; published versions are meant to be seen), but names
  can be sensitive. The fix is to return only published entries to
  anonymous callers and have the overlay send credentials for instructors.

* **OAuth tokens are stored in plaintext.** Encrypt at rest with
  Fernet keyed off an env secret.
* **No rate limiting on the state-changing POSTs.** A misbehaving
  browser extension can hit `/approve` or `/unpublish` thousands of
  times in a tight loop with valid CSRF.
* **No integration tests for the user-facing flows.** Unit tests
  exist for the pure logic; the LTI/CSRF/handler path has none.
* **Single uvicorn worker.** Fine for a single course at CSU East Bay
  pilot; not fine for a campus-wide rollout. Add `--workers N` and
  validate the in-process worker (watcher + bridge) is `worker 0`
  only (or move it to a separate process).
