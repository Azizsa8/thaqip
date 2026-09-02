# Thaqip — Phase 0 Engineering Tickets (Weeks 0–6)

Phase goal (from PRD): **prove the data advantage.** 30 consecutive days of ingestion with < 15-min median detection latency, ≥ 99% capture vs. manual audit, awards corpus ≥ 500k offers, BOQ parse success ≥ 85%.

Estimates: S ≤ 1 day · M 2–3 days · L 4–7 days. Deps reference ticket IDs. PRD refs are the M#-# requirement IDs.

**Ground truth already established (2026-09-02 probe):**
- `GET tenders.etimad.sa/Tender/AllSupplierTendersForVisitorAsync?PageSize=&PageNumber=` returns clean JSON, no auth: `{data[], totalCount(=287,896), pageSize, currentPage}`. Field map captured in `services/ingestion/src/thaqip_ingestion/etimad/models.py`.
- Other routes (e.g. `AllTendersForVisitorAsync`) sit behind an F5/TSPD JavaScript challenge → anti-bot resilience is a real, scoped problem (B5), not a hypothetical.
- Tender detail pages are addressed by an opaque encrypted `tenderIdString`, not the numeric id.

---

## EPIC A — Infrastructure & foundations

| ID | Title | Est | Deps |
|----|-------|-----|------|
| A1 | Monorepo bootstrap + CI | S | — |
| A2 | Dev environment (docker-compose) | S | A1 |
| A3 | Cloud landing zone (KSA region) + IaC skeleton | M | — |
| A4 | Config & secrets management | S | A1 |
| A5 | Observability baseline | M | A2 |

- **A1** — Repo (this scaffold), lint (ruff), typecheck (mypy), pytest, GitHub Actions running all three on PR. *AC: green pipeline on main.*
- **A2** — `docker-compose up` gives Postgres 16 + pgvector, Redis, MinIO (S3-compatible, stands in for KSA object storage). Migrations apply on boot. *AC: fresh clone → running stack ≤ 5 min.*
- **A3** — Account/project in a KSA-region cloud (e.g. STC Cloud / Oracle Jeddah / AWS Bahrain as interim — decision doc required, PDPL note), Terraform for network + Postgres + object storage + one worker VM/container service. *AC: staging environment reachable; region decision documented. (PRD: NFR trust/compliance)*
- **A4** — Pydantic-settings config, `.env` for dev, cloud secret store for staging. No secrets in repo. *AC: same image runs dev/staging by env only.*
- **A5** — Structured JSON logs, Prometheus metrics endpoint on every service, Sentry (or GlitchTip) wired, one Grafana dashboard stub. *AC: ingestion loop emits `thaqip_ingest_pages_total`, `thaqip_ingest_latency_seconds` visible in Grafana.*

## EPIC B — Etimad connector

| ID | Title | Est | Deps |
|----|-------|-----|------|
| B1 | Listing client (visitor API) | S | A1 |
| B2 | Tender detail fetcher | M | B1 |
| B3 | Document/attachment downloader | M | B2, C1 |
| B4 | Awards & offers harvester | L | B2, C1 |
| B5 | Anti-bot resilience layer | L | B1 |
| B6 | Forsah connector spike | M | C1 |

- **B1** — Async client for `AllSupplierTendersForVisitorAsync`: pagination, retry w/ exponential backoff + jitter, client-side rate limit (default ≤ 1 req/s, configurable), UA/session hygiene, structured parse into `EtimadTenderRow`. **Scaffolded — see `etimad/client.py`; harden + tests.** *AC: pulls 1,000 pages without error; unit tests with recorded fixtures. (M1-1)*
- **B2** — Resolve `tenderIdString` → details page/API (relations, conditions, dates, quantities links). Map the TSPD-protected routes; document which need a browser context vs plain HTTP. *AC: detail record for 95% of new tenders within one poll cycle.* (M1-1)
- **B3** — Fetch TSDs/attachments/BOQ files → MinIO/S3 with content-hash dedup, size caps, MIME sniff, ClamAV scan hook. *AC: docs for a sampled day stored + retrievable by tender id; dedup verified. (M1-3)*
- **B4** — Harvest award results and per-vendor offer values (historical + ongoing). This feeds the 500k-offer exit criterion and everything in M4/M8. *AC: ≥ 500k offers backfilled into `offers`; daily incremental job. (M1-5)*
- **B5** — Detection of TSPD/challenge responses (never store challenge HTML as data), session pool with rotation, optional headless-browser fallback worker, per-route circuit breakers, alerting on capture-rate drop, and a global kill-switch. Include the M1-7 seam: interface so a future user-authorized (extension-relayed) fetcher can implement the same connector API. *AC: simulated challenge on a route degrades that route only, pages alerting; no data corruption.*
- **B6** — Spike: map Forsah endpoints (listings, bid counts, Q&A), write findings + fixture set; implement listing pull if trivial. *AC: written spike doc + go/no-go for full connector in Phase 1. (M1-2)*

## EPIC C — Data model & pipeline

| ID | Title | Est | Deps |
|----|-------|-----|------|
| C1 | Schema v1 + migrations | M | A2 |
| C2 | Normalizer + upsert with field-diff | M | C1, B1 |
| C3 | Entity resolution v1 (agencies/vendors) | L | C2 |
| C4 | Historical backfill job | M | C2 |
| C5 | Nightly reconciliation | S | C4 |
| C6 | Document text pipeline | L | B3 |
| C7 | BOQ parser v1 | L | C6 |

- **C1** — Tables: `tenders`, `agencies`, `activities`, `documents`, `offers`, `awards`, `vendors`, `ingest_events` (outbox), `ingest_runs`. Arabic-safe collation, Hijri dates stored alongside Gregorian, raw JSON retained in `payload jsonb`. **Scaffolded — see `db/migrations/0001_init.sql`; review + extend.** *AC: migrations idempotent; ERD in docs. (M1-4 groundwork)*
- **C2** — Map `EtimadTenderRow` → canonical `tenders` upsert; field-level diff on update emits typed events (`tender.created`, `tender.updated{changed_fields}`, `tender.awarded`, `tender.cancelled`, `tender.extended`). *AC: replaying the same page twice emits zero events (idempotent). (M1-1)*
- **C3** — Canonicalize agency names (orthography variants, تشكيل/hamza normalization) and vendor names; stable IDs; merge tooling with audit trail. *AC: top-200 agencies resolve to unique canonical rows; ≥ 98% precision on a 500-row labeled sample. (M1-4)*
- **C4** — Checkpointed walk of all ~288k listed tenders (+ details + awards via B2/B4), resumable, rate-limit aware, progress metrics. *AC: full corpus loaded; re-run resumes not restarts. (M1-5)*
- **C5** — Nightly sweep comparing corpus vs. source listing counts per status/activity; discrepancies queued for refetch + alert. *AC: report artifact per night; auto-heal on sampled deletions. (M1-1)*
- **C6** — For each stored document: text extraction (PDF/DOCX/XLSX), Arabic OCR fallback (Tesseract-ara or cloud OCR — decision doc), chunking, embeddings into pgvector. *AC: ≥ 90% of sampled TSDs yield searchable text; embedding lookup returns sane neighbors. (M1-3)*
- **C7** — Parse BOQ XLSX/PDF into `boq_items(tender_id, item_no, description, unit, qty, confidence)`. *AC: ≥ 85% of sampled BOQ files parse with per-file confidence score; failures land in review queue. (M1-3, exit criterion)*

## EPIC D — Eventing, delta & freshness

| ID | Title | Est | Deps |
|----|-------|-----|------|
| D1 | Outbox → Redis Streams event bus | M | C1 |
| D2 | Delta poll loop (≤ 5-min granularity) | M | B1, C2 |
| D3 | Freshness metrics + internal dashboard | M | D2, A5 |
| D4 | Capture-rate audit harness | M | C4 |

- **D1** — Transactional outbox in Postgres, relay to Redis Streams, consumer-group conventions, event schema registry doc. *AC: at-least-once delivery demonstrated; no event lost across relay restart.*
- **D2** — Scheduler polling newest-first listing pages every ≤ 5 min (newest N pages fast-lane, deeper pages slow-lane), driving C2 upserts. **Loop skeleton scaffolded — `main.py`.** *AC: staged new tender appears as `tender.created` within 15 min end-to-end. (M1-1, phase exit)*
- **D3** — Per-tender `detected_at - published_at` recorded; median/p95 panels; 30-day SLO tracking toward the public freshness board (M1-6 ships Phase 1, internal now). *AC: dashboard shows live median latency.*
- **D4** — Weekly script: random 200 tenders from source UI vs. corpus; capture-rate report. *AC: ≥ 99% capture on two consecutive audits (phase exit).* 

## EPIC E — Internal browse & search

| ID | Title | Est | Deps |
|----|-------|-----|------|
| E1 | Internal tender browser (web) | M | C2 |
| E2 | Search index + indexer | M | C6 |

- **E1** — Minimal internal Nuxt/Next app: tender table (filters: status/activity/region/agency/dates), tender detail w/ documents, freshness column. Auth: simple SSO/basic. *AC: team can answer "what closed today in IT in Riyadh" without SQL.*
- **E2** — Typesense/OpenSearch index over tenders + extracted doc text; indexer consumes events. *AC: Arabic keyword search returns tender by a phrase that appears only inside its TSD. (M2-2 groundwork)*

## EPIC F — Company, legal & naming (tracked, non-engineering)

| ID | Title | Est | Deps |
|----|-------|-----|------|
| F1 | KSA entity formation started | — | — |
| F2 | Legal review: ingestion & document-serving posture | — | — |
| F3 | Thaqip trademark (SAIP) + domains | — | — |
| F4 | Hosting-region decision record | — | A3 |

- **F3 note (checked 2026-09-02):** `thaqip.com` already has DNS (verify ownership/for-sale status manually); `thaqip.sa`, `thaqip.io`, `thaqip.ai` show no DNS — likely available. Register .sa via SaudiNIC-accredited registrar; SAIP trademark search for ثاقب in classes 9/35/42.
- **F2 scope:** KSA lawyer opinion on harvesting public Etimad data, storing/serving government tender documents to subscribers, and ToS exposure; informs B5 kill-switch policy and the official-access track.

---

## Suggested sequencing (2 backend/data + 1 full-stack)

| Week | BE-1 | BE-2 | FS |
|------|------|------|-----|
| 1 | A1, A4, B1 harden | C1, A2 | A5, CI |
| 2 | B2 | C2, D1 | E1 start |
| 3 | B5 | C4 backfill | E1, D3 dashboard |
| 4 | B4 awards | C3 entity res | E2 search |
| 5 | B4 cont., B6 spike | C6 doc pipeline | E1/E2 polish, D4 |
| 6 | C7 BOQ parser | C5, hardening | freshness SLO review |

Phase-exit review at end of week 6 against the four exit criteria; the 30-day latency clock starts as soon as D2 is stable (target: week 3), overlapping Phase 1 work.
