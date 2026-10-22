# Moving a deployment from Reflow Core to Equalify Iris

This connector used to convert documents with Equalify Reflow (Reflow Core,
`/api/v1/documents/*`). It now converts with
[Equalify Iris](https://github.com/EqualifyEverything/equalify-iris)
(`/v1/sessions/*`). This is the cutover for an existing deployment: Iris
goes up beside Reflow Core, new conversions move to Iris, documents Reflow
already converted are copied into the connector, then Reflow Core is shut
down.

Nothing faculty already published is lost at any step: Canvas Pages the
bridge wrote belong to Canvas and keep working without either service.

## What changes for faculty

Say these out loud before the switch; four of them change what people see.

1. **The disclaimer is shown again.** Consent version 1.0 told faculty that
   documents are scanned for personal information and that PII is redacted
   before anything reaches the AI provider. Iris runs no such scan, so the
   disclaimer now says so (version 2.0), and everyone acknowledges it again at
   their next launch. Until a course's owner (the instructor whose Canvas
   token the watcher uses) has done so, nothing in that course is converted:
   it waits, and resumes on its own after they accept. Ask the owners of
   active courses to open the tool once after the upgrade. Review the new
   wording in `connector/api/canvas_consent.py` with whoever owns the
   original; it is policy text.
2. **No privacy pause.** Reflow could stop a document and ask "approve or
   deny processing?" when it found PII. Iris documents never stop there.
3. **Images.** Iris describes every image but cannot hand the image back.
   The connector lifts each picture out of the original PDF; when it cannot
   tell which picture a description belongs to, the page shows the
   description as text ("Image (page 4 of the original PDF): ...") instead of
   a broken image. Vector charts drawn as PDF graphics rather than embedded
   pictures always come out as descriptions.
4. **Long PDFs are converted in parts** of up to 25 pages and joined. The
   review screen names the seams so a table or sentence that crosses one can
   be checked.
5. **The review screen lists what Iris itself flagged** — pages it could not
   convert, pages that failed its own accuracy check, issues it left open —
   above the side-by-side view. Documents that did not convert get a "Try
   again" button on the Accessible Documents page.
6. **Word and PowerPoint** files are converted to PDF (LibreOffice) before
   Iris reads them. Layout-heavy slides convert well; complex Word styling
   may not survive exactly.
7. **The tool says Iris instead of Reflow** on its pages, buttons and
   disclaimer. New courses get the drop folder "Iris - Convert to
   Accessible"; a course that already has "Reflow - Convert to Accessible"
   keeps using it, and the Accessible Documents page names whichever one
   the course has.

## Before you start

- **A model provider for Iris.** Iris talks to OpenRouter (or any
  OpenAI-compatible endpoint) or Amazon Bedrock. It does not call the
  Anthropic API directly, so a key that Reflow Core used with Anthropic does
  not plug in as-is. See Iris's `docs/models.md`.
- **Capacity.** Iris needs far less memory than Reflow Core's Docling
  service, but rasterizing PDFs is CPU-bound: two cores work; four are
  comfortable. `defaults.max_concurrent_runs` (default 2) bounds how many
  documents Iris converts at once.

## 1. Run Iris, gated, and not on the public internet

Follow Iris's README (Docker compose works). Generate a token with
`openssl rand -hex 32` and put it in Iris's `.env` as `IRIS_API_TOKEN=...`
(it becomes `server.api_token`); the connector gets the same value in step 2.

Three things matter more than anything else here:

- **Set the token.** Iris has no user accounts. Without the token,
  `GET /v1/sessions` lists every session and any session's HTML is one
  request away — every course document the connector ever converted.
- **Do not publish Iris through the same front door as the connector.** If a
  reverse proxy forwards "everything else" to the port Reflow Core used
  (commonly 8080), Iris would take over that port and be served to the
  internet, demo page included. Only the connector needs to reach Iris, so
  put Iris on a Docker network the connector joins (below) and publish no
  public port. (Iris's compose file publishes `8080:8080`, which is every
  interface.) Remove or narrow the catch-all rule when Reflow Core goes.
- **Set a session retention.** Iris keeps each session's PDF, page images
  and output until `storage.session_retention_days` deletes them, and
  forever while it is blank (the default). The connector keeps its own copy
  of every finished document, and of each approved document's tagged PDF,
  so a short retention costs little. Copy
  `config.example.yaml` to `config.yaml`, set
  `storage.session_retention_days: 30` (or your records policy's number),
  and mount it as Iris's compose file shows.

**Networking.** Attach Iris to a Docker network the connector is also on (a
network the connector already shares with Reflow Core works) and address it
by service name. In Iris's `docker-compose.yml`:

```yaml
services:
  iris:
    ports:
      - "127.0.0.1:8090:8080"   # host-only, for the checks below; or no ports at all
    networks: [default, shared]
networks:
  shared:
    external: true
    name: <the connector's network>
```

and give the connector service the same network in its compose file (or a
`docker-compose.override.yml`) if it is not on it already. Iris's container
port stays 8080 on that network; publishing it on the host's 8090 avoids a
clash with Reflow Core's gateway on 8080 while both run.

Do not point the connector at `host.docker.internal:8090` here: a port
published on `127.0.0.1` is not reachable from other containers through the
host gateway.

Check it from the host:

```bash
curl -s http://127.0.0.1:8090/v1/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/v1/me   # 401 = gated
```

## 2. Upgrade the connector, keeping Reflow configured

This version lives in its own repository,
[equalify-iris-canvas](https://github.com/oshrizak/equalify-iris-canvas), so
it goes into a new folder. Clone it beside your current checkout and bring
your configuration across:

```bash
git clone https://github.com/oshrizak/equalify-iris-canvas.git
cd equalify-iris-canvas
cp -p ../reflow-canvas-lti/.env .
cp -rp ../reflow-canvas-lti/keys .
cp -p ../reflow-canvas-lti/docker-compose.override.yml .   # if you have one
echo "COMPOSE_PROJECT_NAME=reflow-canvas-lti" >> .env
```

Keep that last line. Docker Compose names the Redis volume after the
project (`reflow-canvas-lti_redis-data`) and names the project after the
folder, unless `COMPOSE_PROJECT_NAME` says otherwise. Without it, the
connector starts on a new, empty volume and forgets every job, faculty
consent, instructor token and review decision. If your old folder has
another name, use that name: `docker volume ls` lists the volume as
`<name>_redis-data`.

The image now includes LibreOffice; build with
`--build-arg WITH_LIBREOFFICE=false` to leave it out. Add to `.env`:

```bash
IRIS_API_BASE_URL=http://iris:8080     # Iris's service name on the shared network
IRIS_API_TOKEN=<the same value as Iris's IRIS_API_TOKEN>
```

Leave `REFLOW_API_BASE_URL` and `REFLOW_API_KEY` set for now. Then build,
stop the old stack, and start this one:

```bash
docker compose build
(cd ../reflow-canvas-lti && docker compose down)   # never "down -v": that deletes the volume
docker compose run --rm connector python scripts/preflight.py   # probes Iris from inside the network
docker compose up -d
docker compose logs connector | grep -E 'Iris|startup secrets audit'
```

The startup log names the Iris version and page cap, and logs `CRITICAL` if
Iris is open or rejects the token.

From now on, run Compose only from the new folder: the old one has the same
project name, so a command there acts on these containers. Point the Redis
backup cron job, if you have one, at the new folder; earlier backups stay in
the old folder's `backups/`.

From here, new conversions go to Iris. Documents that were mid-conversion in
Reflow Core keep going there, because the bridge still drives Reflow-era jobs
while `REFLOW_API_BASE_URL` is set.

## 3. Copy Reflow-converted documents into the connector

Before Reflow Core goes, its documents have to be readable without it: the
connector used to fetch Reflow's markdown on every alt-format view. Once
in-flight Reflow jobs have finished (the review screen's "Converting" bucket
is empty of them):

```bash
docker compose exec connector python -m connector.tools.migrate_reflow_jobs --dry-run
docker compose exec connector python -m connector.tools.migrate_reflow_jobs
```

Each finished document's markdown is copied into Redis. Re-run it until it
reports no failures; it skips what it already copied.

Jobs Reflow never finished (still converting, waiting on a PII decision, or
failed) cannot be copied. Hand them to Iris:

```bash
docker compose exec connector python -m connector.tools.migrate_reflow_jobs --requeue-unfinished
```

The watcher converts those files with Iris on its next tick, and the bridge
updates the same Canvas Page when one exists.

## 4. Retire Reflow Core

Remove `REFLOW_API_BASE_URL` and `REFLOW_API_KEY` from `.env`, restart the
connector, and stop the Reflow Core stack. Migrated Reflow documents keep
serving every alt format from the copy; Reflow-era jobs are otherwise left
exactly as they are.

## Rolling back

Roll back before any document converts with Iris if you can. The previous
connector version cannot read job records that carry the new Iris fields; if
you must go back later, those records (`"backend": "iris"` in
`eq-pdf:canvas:job:*`) have to be removed first, and those documents
converted again with Reflow.

The switch itself is two commands, because both folders use the same Redis
volume: `docker compose down` in the new folder, then `docker compose up -d`
in the old one.
