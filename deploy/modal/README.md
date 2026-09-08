# Thaqip Modal Runtime

This folder contains the Phase 1 scaffold for running Thaqip's data lanes on Modal.

## Secret

Create one Modal secret named `thaqip-runtime` with the variables needed by the jobs:

```bash
modal secret create thaqip-runtime \
  DATABASE_URL='postgres://...' \
  REDIS_URL='redis://...' \
  TYPESENSE_URL='https://...' \
  TYPESENSE_KEY='...' \
  TELEGRAM_BOT_TOKEN='...'
```

Optional variables:

- `THAQIP_ANTHROPIC_API_KEY`
- `MINIO_ENDPOINT`
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `THAQIP_KILL_SWITCH`

## Deploy

```bash
cd /home/ais04/thaqip
modal deploy deploy/modal/modal_app.py
```

## Scheduled Lanes

- `delta_poller`: every 5 minutes, one Etimad newest-first pass.
- `awards_harvest`: every 6 hours, expands the offers and awards corpus.
- `award_watch`: every 4 hours, checks pursued tenders for newly announced awards and backfills outcomes.
- `forsah_pull`: hourly, tracks Forsah public opportunities and bid intensity.
- `reminders`: hourly, emits pursuit deadline reminders.
- `reconcile`: daily, checks source/corpus gaps and emits gap events.
- `daily_digest`: daily at 07:00 KSA.

## Manual Lanes

```bash
modal run deploy/modal/modal_app.py::backfill --category all --max-pages 250
modal run deploy/modal/modal_app.py::bulk_index
```

## Operating Rules

- Jobs must stay idempotent. Re-running a lane should update counters or heal gaps, not duplicate business records.
- Secrets stay in Modal. Do not hardcode database URLs, API keys, or tokens in this repository.
- Keep Etimad rates conservative. The default crawler policy remains approximately 1 request per second unless a lane has its own stricter pacing.
- Use `THAQIP_KILL_SWITCH=1` to stop crawler behavior without removing the deployed schedules.
- Watch `ingest_runs`, `/api/lanes`, `/api/pricing/accuracy`, and the War Room after deployment. A scheduled function being green in Modal is not enough; the product health source is the database and measured customer outcomes.

