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

- [x] B1 listing client — working against the live visitor API (rate-limited, retrying, challenge-aware)
- [x] C1 schema v1 draft
- [x] C2 normalize/diff/idempotency logic (in-memory; Postgres upsert next)
- [ ] B2 detail fetcher · B5 anti-bot layer · C4 backfill · D1 outbox relay · D3 freshness dashboard

Ground rules: rate limits stay conservative (default 1 req/s), challenge responses are never parsed as data, and the legal/official-access track (tickets F1–F4) runs in parallel from day one.
