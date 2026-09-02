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
- [x] C3 entity resolution v1 — Arabic normalization (hamza/taa-marbuta/diacritics), agency canonicalization + tender linking (165 agencies from first 480 tenders), vendor dedupe with offer/award repointing
- [ ] B5 anti-bot hardening (429 discipline started: Retry-After honored; listing rate-limits after sustained multi-session pulls — plan paced sessions) · C5 reconciliation · C6 doc pipeline · C7 BOQ parser · D2 continuous deployment of the loop

Key source facts (probed 2026-09-02): listing API is public JSON (~288k tenders); detail/awarding routes need TSPD cookies (one browser bootstrap per session); components take `tenderIdStr` (raw encrypted id); awarding returns 302→/Home/Error for non-awarded tenders and 429s under fast polling — default pacing is 3s between component fetches.

Ground rules: rate limits stay conservative (default 1 req/s), challenge responses are never parsed as data, and the legal/official-access track (tickets F1–F4) runs in parallel from day one.
