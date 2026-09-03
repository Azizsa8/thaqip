# B6 Spike — Forsah (فُرصة) Connector

**Date:** 2026-09-03 · **Verdict: GO — promoted to a working listing connector in the same spike.**

## What Forsah is
Private/partner-sector opportunities platform at `forsah.sa` (a 910ths/"تسعمئة وعشرة أعشار" product —
React SPA, GTM/LinkedIn/Facebook pixels). Complements Etimad: RFQs and tenders from
non-government buyers.

## Access — the big finding
`https://forsah-api.910ths.sa/api/v1` is **public JSON: no auth, no bot protection, no rate
limiting observed** (modest pacing still applied: 1.5s between pages).

| Endpoint | Returns |
|---|---|
| `GET /opportunities?page=&size=` | paginated listing — 25,511 listed at spike time |
| `GET /opportunities/count` | `{count: 29,675, totalAwardedValue: 6.71B SAR, totalPublishedValue: 10.45B SAR}` |
| `GET /inquiries?...` | Q&A threads (P1 follow-up — powers the "استفسارات وردود" feature) |
| `/opportunities/preview/{id}` etc. | detail routes referenced in the app bundle, untested |

## Schema highlights (fixture: `tests/fixtures/forsah_page.json`)
- **Competition intensity per opportunity — free:** `bidsCount`, `submittedBidsCount`,
  `submittedExternalBidsCount`, `draftBidsCount`, `viewCount`. This is the exact data the
  incumbent markets as a premium exclusive.
- UUID ids (`schema migration 0002` added `tenders.source_uid` + intensity columns).
- Bilingual categories with hierarchy, delivery cities, value-range bands, type
  (RFQ/شراء مباشر vs tender), `publishDate` / `dueDate` / `extendedDueDate` / `awardDate`,
  `statusKey` (open/awarded/closed…), `multipleAwarding`.

## Implemented now (`forsah.py`)
Listing pull → normalize → upsert into `tenders` with `source='forsah'`, content-hash
idempotency, typed outbox events (`tender.created/updated/awarded`), intensity counters stored.
Verified live: 36 rows created, replay produced 0 events. Hourly cron: `bin/forsah-pull.sh`.

## Follow-ups (Phase 1 backlog)
1. Detail + inquiries endpoints → Q&A tracking events (notification feature parity).
2. `submitted_bids_count` deltas → "competition rising" alerts (cheap, differentiating).
3. Console: source badge + intensity column for Forsah rows; source filter.
4. Awarded Forsah opportunities carry `awardDate` — check whether winner identity is exposed
   on any public detail route.
