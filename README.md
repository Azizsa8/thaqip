# Thaqip (ثاقب)

The bid-winning operating system for Saudi government tenders. See `docs/` for the PRD companions and `docs/phase0-tickets.md` for the Phase 0 engineering breakdown.

## Layout

```
docs/                      Phase 0 tickets, decision records
db/migrations/             Postgres schema (applied by docker-compose on first boot)
services/ingestion/        Etimad/Forsah connectors, normalization, delta loop (Python 3.12, uv)
infra/                     Terraform (ticket A3) — placeholder
```

## Quick start

```bash
docker compose up -d                 # Postgres16+pgvector, Redis, MinIO; schema auto-applies
cd services/ingestion
uv sync --group dev
uv run python -m thaqip_ingestion.main --pages 2      # one fetch→normalize→diff pass, live
uv run pytest                                          # fixture-based tests
```

## Status (Phase 0)

- [x] B1 listing client — live against the visitor API (rate-limited, retrying, challenge-aware)
- [x] C1 schema v1 (11 tables incl. outbox, applied via docker-compose)
- [x] C2 Postgres upsert + typed outbox events — verified live, idempotent on replay
- [x] B2 detail fetcher — TSPD session bootstrap (Playwright) + view-component parsers (relations/dates/attachments/awarding), verified live
- [x] B4 awards harvester — awarded-tenders discovery via `TenderCategory=6` (~238k tenders), bidder/awardee parsing into vendors/offers/awards, `tender.awarded` events; 429-aware pacing
- [x] D1 outbox → Redis Streams relay (`thaqip.events`, at-least-once, consumer groups verified)
- [x] C4 checkpointed backfill — page-walk with per-page checkpoint in ingest_runs, resume verified; full corpus is ~5,800 pages ≈ 2h at 1 req/s
- [x] D3 freshness harness — p50/p95 detection-latency report vs published_at (best observed live: 1m35s); public board ships Phase 1
- [x] Freshness SLO trend — `/api/freshness/trend` and dashboard SVG chart show daily p50/p95 detection latency against a 15-minute SLO
- [x] C3 entity resolution v1 — Arabic normalization (hamza/taa-marbuta/diacritics), agency canonicalization + tender linking (165 agencies from first 480 tenders), vendor dedupe with offer/award repointing
- [x] B5 anti-bot hardening — per-route circuit breakers (`circuit_breaker.py`), global kill switch (`THAQIP_KILL_SWITCH`), 429 Retry-After discipline, WAF cool-off backoff
- [x] C5 reconciliation — nightly census & head-sample sweep with gap detection (`reconcile.py`)
- [x] C6 doc pipeline — MinIO content-hash storage, text extraction (PDF/DOCX/XLSX), text chunking, pgvector embedding readiness (`documents.py`)
- [x] C7 BOQ parser — Arabic column detection (بند، بيان، وحدة، كمية) with confidence scoring & review queue routing (`boq.py`)
- [x] BOQ history drill-down — `/api/boq-items/{id}/similar` finds historically similar BOQ rows and the tender drawer exposes a per-row drill button
- [x] D2 continuous deployment — delta loop containerized in Docker Compose (`poller` service) with automatic client recycling watchdog
- [x] M5 pricing calibration seeding — `pricing-seed` Compose service and `bin/pricing-seed.sh` keep active pursuits stocked with one baseline price hypothesis for later award measurement
- [x] Settings v1 — `/api/settings` plus dashboard tab for gated-feature readiness, alert cadence, calculator defaults, and my-company comparison baseline used by competitor intelligence
- [x] Vendor comparison CTA — `/api/vendors/{id}/compare` compares each competitor with the saved company target profile and returns tactical recommendations
- [x] Agency win heatmap — vendor profiles now expose `agency_matrix` and render a heatmap of wins, participation, and price gap by agency
- [x] Calculator modes v1 — War Room price simulator now returns and renders 8 pricing modes with ready/needs-data states
- [x] Tender award export — tender drawer exports offers/award details as Arabic Excel-friendly CSV
- [x] Compliance evidence capture — War Room matrix rows now persist an evidence note/link and include it in the Arabic CSV export
- [x] Activity price curve — `/api/tenders/{id}/price-curve` powers a drawer chart that toggles between historical awards and offer medians for the tender activity
- [x] Alert history drill-down — notifications API and Alerts tab filter by profile and keyword
- [x] Agency pipeline drill-down — agency profiles switch between newest tenders and upcoming deadlines
- [x] D4 capture-rate audit harness — automated sampling vs live Etimad listing verifying >= 99% capture SLO (`audit.py`)

Key source facts (probed 2026-09-02): listing API is public JSON (~288k tenders); detail/awarding routes need TSPD cookies (one browser bootstrap per session); components take `tenderIdStr` (raw encrypted id); awarding returns 302→/Home/Error for non-awarded tenders and 429s under fast polling — default pacing is 3s between component fetches.

Ground rules: rate limits stay conservative (default 1 req/s), challenge responses are never parsed as data, and the legal/official-access track (tickets F1–F4) runs in parallel from day one.
