# Deployment

The connector ships as a single Python service plus a Redis dependency. The
intended deployment shape is the same image (`Dockerfile`) running anywhere
that can reach:

- [Equalify Iris](https://github.com/EqualifyEverything/equalify-iris)
  (`IRIS_API_BASE_URL`), the conversion service, on a private address.
- Canvas (`CANVAS_API_URL` + per-instructor OAuth endpoints).
- A Redis the connector owns.

Upgrading a deployment that converted with Reflow Core? Follow
[`MIGRATING_FROM_REFLOW.md`](MIGRATING_FROM_REFLOW.md) instead of this
page's first-deploy steps.

## Iris first

Stand up Iris before the connector (its README covers Docker compose), with
two settings that are not optional for course documents:

- **`IRIS_API_TOKEN` set in Iris's `.env`** (`openssl rand -hex 32`). Iris
  has no user accounts; an open Iris lists every session it holds and
  serves any of them to whoever asks. The connector sends the same value.
- **Not reachable from the internet.** Only the connector calls Iris. Put
  it on a Docker network the connector joins and address it by service name
  (`http://iris:8080`); publish at most a `127.0.0.1` port for checks from the
  host. Make sure no reverse-proxy rule (a catch-all included) forwards to it.
- **A session retention.** Iris keeps every uploaded PDF, its page images
  and the output until `storage.session_retention_days` deletes them, and
  forever while it is blank (the default). The connector stores its own copy
  of each finished document, so 30 days or less costs nothing.

Iris also needs a model provider (OpenRouter or another OpenAI-compatible
endpoint, or Amazon Bedrock; see Iris's `docs/models.md`) and two to four
CPU cores for rasterizing PDFs.

## First deploy, start to finish

```bash
git clone https://github.com/oshrizak/equalify-iris-canvas.git
cd equalify-iris-canvas

cp .env.example .env                    # fill in — see Environment below (IRIS_API_TOKEN!)
./scripts/generate_lti_keys.sh          # LTI 1.3 key pair into keys/
docker compose run --rm connector python -m connector.tools.generate_keys >> .env

# Catch configuration mistakes before they become runtime mysteries.
# Also asks Iris whether it is reachable, gated, and accepts our token.
docker compose run --rm connector python scripts/preflight.py

# Register the tool in Canvas. Creates the LTI Developer Key from the
# tool's own /lti/config.json, enables it, binds it to the account and
# deploys it, then prints LTI_CLIENT_ID and LTI_DEPLOYMENT_ID for .env.
# Needs a Canvas admin token with manage_developer_keys; revoke it after.
export CANVAS_BASE_URL="https://school.instructure.com"
export CANVAS_ADMIN_TOKEN="<admin token>"
export CANVAS_ACCOUNT_ID="1"
export CANVAS_COURSE_ID="<a course to deploy into>"
export TOOL_BASE_URL="https://reflow.example.edu"
docker compose run --rm connector python scripts/provision_canvas.py --apply

docker compose up -d
docker compose run --rm connector python scripts/preflight.py   # re-verify
```

Canvas has no API for minting the second, non-LTI **API Key** used for
per-instructor OAuth, and its secret is shown exactly once — so
`CANVAS_OAUTH_CLIENT_ID` / `CANVAS_OAUTH_CLIENT_SECRET` stay a manual step.
The provisioning script prints the URL and redirect URI to use.
Full walkthrough: [`CANVAS_SETUP.md`](CANVAS_SETUP.md).

## Image

```bash
docker build -t equalify-iris-canvas:0.1.0 .
```

The default `CMD` is `uvicorn connector.main:app --host 0.0.0.0 --port 8000`.
Health is checked via `GET /health`.

System packages installed (for alt-format generators):
- `tesseract-ocr` — OCR'd PDF alt format
- `ghostscript`, `qpdf`, `unpaper` — `ocrmypdf` dependencies
- `poppler-utils` — `pdf2image`

If you don't ship the OCR'd PDF alt format you can drop these to slim the
image — `ocrmypdf` and `pdf2image` Python wheels will still install but
runtime calls will fail with a clear error.

LibreOffice (Writer and Impress, plus Calibri/Cambria-compatible fonts) is
installed so Word and PowerPoint files can be turned into PDFs before they
go to Iris, which reads PDFs only. It is the largest layer in the image.
Build with `--build-arg WITH_LIBREOFFICE=false` to leave it out; the watcher
then skips Office files, logs why once, and picks them up if LibreOffice is
added later.

## Environment

`.env.example` is the source of truth. The minimum production set:

```
IRIS_API_BASE_URL=http://iris:8080       # private address (shared Docker network here)
IRIS_API_TOKEN=<from secret manager>      # equals Iris's own IRIS_API_TOKEN

LTI_ENABLED=true
LTI_PUBLIC_URL=https://equalify-iris-canvas.your-org.edu
LTI_ISSUER=https://canvas.instructure.com
LTI_CLIENT_ID=...
LTI_DEPLOYMENT_ID=...
LTI_AUTH_LOGIN_URL=...
LTI_AUTH_TOKEN_URL=...
LTI_JWKS_URL=...

CANVAS_API_URL=https://canvas.instructure.com
CANVAS_OAUTH_CLIENT_ID=...
CANVAS_OAUTH_CLIENT_SECRET=<from secret manager>
CANVAS_ALLOWED_ORIGINS=https://canvas.instructure.com

# Production-required security keys — generate with
#   docker compose run --rm connector python -m connector.tools.generate_keys
TOKEN_ENCRYPTION_KEY=<from secret manager>
CSRF_SECRET_KEY=<from secret manager>

# Production-recommended: enforce the WCAG publication gate
REQUIRE_WCAG_GATE=true

REDIS_URL=redis://<host>:6379/0
LOG_LEVEL=INFO
ENVIRONMENT=production
```

Optional, only when the matching alt-format is enabled:

```
# Audio (MP3) — Amazon Polly
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=<from secret manager>

# Translate — Anthropic Claude
ANTHROPIC_API_KEY=<from secret manager>
```

Iris tuning, all with working defaults (see `.env.example`):

```
IRIS_POLL_SECONDS=30                     # bridge poll interval
IRIS_MAX_DOCUMENT_PAGES=300              # longest document converted automatically
IRIS_ESTIMATED_COST_PER_PAGE_CENTS=11    # reserved against each course's monthly cap
IRIS_PRICE_INPUT_PER_MTOK_USD=0          # with the OUTPUT/CACHE_* prices: record real cost per job
IRIS_CLOSE_SESSIONS_ON_APPROVE=false     # see IRIS_API.md before turning on
```

For multi-tenant deployments, also set:

```
MULTI_TENANT_WATCHER=true
CANVAS_TENANT=<short slug, e.g. "csueb" or "uchicago">
```

## Secrets

- `LTI_PRIVATE_KEY_PATH` — mount the RSA private key as a read-only
  volume. Never bake it into the image.
- `IRIS_API_TOKEN`, `CANVAS_OAUTH_CLIENT_SECRET`, `CANVAS_API_TOKEN` (if
  used) — pull from your secret manager into env at runtime. The connector
  logs `CRITICAL` at startup if `IRIS_API_TOKEN` is unset, if Iris answers
  without it, or if Iris rejects it.
- `TOKEN_ENCRYPTION_KEY`, `CSRF_SECRET_KEY` — same pattern. Required for
  production. The connector logs `CRITICAL` per missing key at startup
  via its boot-time secrets audit; grep `startup secrets audit:` after
  every deploy to verify cleanliness. See
  [`OPERATIONS.md`](../OPERATIONS.md#secrets-checklist-before-first-launch)
  for the full checklist and key-rotation procedure.

## Persistence + backups

`docker-compose.yml` runs Redis with AOF + RDB persistence on a named
volume (`redis-data`). The volume survives `docker compose down`;
`docker compose down -v` wipes it.

Schedule the bundled backup script from host cron for off-host
durability:

```
0 */6 * * * cd /path/to/equalify-iris-canvas && BACKUP_S3_BUCKET=s3://… ./scripts/backup-redis.sh
```

See [`OPERATIONS.md`](../OPERATIONS.md#redis-persistence--backups) for
the full backup/restore procedure.

## Network

- Inbound (from Canvas) — TCP 443 on the load balancer, terminated to TCP
  8000 on the container. HTTPS only; Canvas validates the redirect URL.
- Inbound (Theme Editor `/panorama.js`) — same path.
- Outbound to Iris — its HTTP port (8080 by default) on a private network.
  Iris must not be reachable from anywhere else.
- Outbound to Canvas (REST + OAuth) — TCP 443 to the Canvas host.
- Legacy, only while migrating off Reflow Core: Reflow Core and its S3 /
  floci (presigned URLs).

## Scaling

The connector is stateless (Redis owns state). Scale horizontally behind
a load balancer — each replica runs its own watcher + bridge workers, so
add a coordination strategy if you grow beyond one replica:

- **Simplest** — run only one replica. The watcher tick is cheap, and
  uploads to Iris are paced to Iris's per-caller rate limit, which every
  replica would share.
- **Stronger** — set `DISABLE_WORKERS=true` on all-but-one replica so
  HTTP scales horizontally while workers stay singletons. *(Not yet a
  setting — file an issue if you need this.)*

## Health checks

- `GET /health` — liveness. Returns 200 once FastAPI startup completes.
  It does not call Iris. The startup log does once (`Iris <version> at
  <url> (pages per session: 25, ...)`, or a warning), and so does
  `scripts/preflight.py`.
- `GET /lti/healthz` — LTI-specific readiness; reports whether the
  module is enabled.

## Logging

`LOG_FORMAT=json` (default in production; ENVIRONMENT must be unset or
set to a non-`dev` value) emits one JSON object per log line, ready for
CloudWatch Insights, Loki, Datadog, etc. `LOG_FORMAT=text` is the
default for `ENVIRONMENT=dev`.

Context vars carried through async hops: `request_id`, `user_id`,
`course_id`, `tenant`.

## Metrics

Prometheus metrics on `${METRICS_PORT}` (default `8001`) when
`ENABLE_METRICS=true`. See [PILOT_RUNBOOK.md](PILOT_RUNBOOK.md) for
the dashboards that matter.
