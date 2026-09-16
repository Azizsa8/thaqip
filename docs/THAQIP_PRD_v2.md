# Thaqip (ثاقب) — Production PRD v2

**Status:** implementation blueprint and production-readiness audit. Written 16 Sep 2026 against commit `399a252` plus uncommitted Day-2 work on the laptop.
**Audience:** engineers and coding agents building, hardening and validating the system.
**Rule of this document:** nothing counts as done because a screen exists. Every claim below is labelled **VERIFIED** (observed in code, database or a passing test on 14–16 Sep 2026), **UNVERIFIED** (exists but not proven), or **NOT BUILT**.

---

## 1. Executive Summary

Thaqip is an Arabic-first intelligence platform over Saudi public procurement (Etimad, and Forsah for SME tenders). It ingests tenders, offers and awards, alerts customers, gives them a bid war-room, competitor/agency history, analytics dashboards, and (eventually) evidence-gated price ranges.

**What is genuinely real today (VERIFIED):** continuous Etimad/Forsah ingestion (2,367 tenders, 1,928 published offers, 470 awards, 946 vendors, 204 agencies), Telegram alerting pipeline, scrypt/session authentication with tenant isolation proven by a security battery, an analytics stack (Superset 6.1 over a read-only schema enforced by Postgres grants), a blind daily prediction clock with an honest scorecard (2 scored outcomes of a 150 minimum), GASTAT price indices, nightly backups with a passing restore drill, a production compose file rehearsed locally, 1,123 + 24 automated tests.

**What only appears to work or could mislead (VERIFIED problems):**
1. The **legacy price simulator** (`POST /api/pursuits/{pid}/simulate-price`, still wired to the War Room UI) computes win probability from a hardcoded logistic curve (`1/(1+e^{4(ratio-0.95)})`) and "optimal price" from fixed multipliers (0.96/0.92/0.86). It is a heuristic presented as a simulation. It must be removed from the UI or labelled experimental with no recommendation.
2. `pricing_seed` writes a **fabricated 100,000 SAR "baseline_default" hypothesis** when no evidence exists, and the accuracy scorer accepts `baseline_*` rows. Four such rows exist. This is a fabricated value on a scoring path.
3. The alerts UI offers an **email channel marked "قريباً"** that creates rows which sit `pending` forever (no worker). Functional-looking UI hiding unbuilt logic.
4. **56 Telegram notifications failed** (HTTP 400, non-numeric chat id `dev-console`) and are shown as counts in the UI without a remediation path.
5. The public **Netlify demo** (thaqip-demo.netlify.app) is a 1.1 MB static snapshot behind a client-side SHA-256 password gate. It is neither secure nor fresh and must be taken down before launch.
6. `source_lineage` is **empty**, so `/api/internal/lineage/*` cannot evidence provenance; `awards.awarded_at` is **NULL for every award**; Forsah tenders carry agency `"partner"` on console pages.
7. **No customer account lifecycle** exists in the running system: one admin, no invite, no password reset, no self-service. Migration 0021 is applied and `auth.py` is half-edited (UNVERIFIED, uncommitted).
8. No security headers, no global rate limiting, no external uptime monitor, no error tracker, no metrics.

**Verdict:** not production-ready. Ready for *internal testing* today; ready for *limited beta* once the P0 list in §29 is cleared (accounts, simulator removal, fabricated-baseline fix, demo takedown, hosting off the laptop, monitoring, legal pages). Public self-serve production is a separate gate.

---

## 2. Product Goals

1. Detect every new/changed Etimad and Forsah tender within 15 minutes (p50) and notify matching customers.
2. Give a contractor or supplier, in Arabic, a defensible answer to: which tenders fit me, who will compete, what did this agency pay before, when is the deadline.
3. Never present an inferred number as an observed fact. Every prediction carries provenance, evidence tier and a refusal path.
4. Prove price-prediction accuracy with blind, pre-award predictions before showing any accuracy claim.
5. Operate safely for paying customers: tenant isolation enforced in the database, backups that restore, secrets never in the repo.

Non-goals for the beta: open self-signup, payments, mobile apps, integrations with ERP systems, bid submission to Etimad.

---

## 3. Scope

### 3.1 Feature status ledger

| # | Feature | Status | Evidence |
|---|---|---|---|
| F1 | Etimad listing poller (5-min) | **Implemented** | `ingest_runs` connector `etimad.listing`, last run today |
| F2 | Forsah hourly pull | **Implemented** | cron `forsah-pull.sh`, 104 rows |
| F3 | Awards harvest, fresh lane | **Implemented** | `awards_harvest --mode fresh`, 470 awards |
| F4 | Awards historical backfill | **Implemented (running)** | connector `etimad.awards_backfill`, cursor page ≥21, WAF cool-offs kept |
| F5 | Outbox → Redis relay → alerts | **Implemented** | 488 log notifications sent |
| F6 | Telegram delivery + /start linking | **Implemented, needs token** | `telegram.py`, 4 tests; bot token not set (`configured:false`) |
| F7 | Email alert channel | **Appears implemented, is not** | UI option; rows stay `pending`; no worker |
| F8 | Typesense search + indexer | **Implemented** | 503 when index down (honest) |
| F9 | Console auth (scrypt, sessions, throttle, audit) | **Implemented** | 24 console tests; security battery |
| F10 | Tenant isolation | **Implemented** | `test_battery_security.py` (forged headers, IDOR, SQL-shaped slugs) |
| F11 | Customer accounts (invite, reset, must-change, platform admin, per-tenant prefs) | **Partially implemented, UNVERIFIED** | 0021 applied; `auth.py` edited; no endpoints; not committed |
| F12 | Dashboard KPIs / market heat / award bands | **Implemented** | SQL-backed; verified live |
| F13 | Discovery list, filters, follow, CSV export | **Implemented** | routes exist; e2e battery |
| F14 | War Room (pursuits, stages, compliance matrix, outcomes) | **Implemented** | 12 pursuits, 133 compliance items, 1 outcome |
| F15 | Legacy price simulator (`simulate-price`) | **Implemented but heuristic — must not ship as is** | hardcoded logistic + multipliers |
| F16 | P2W engine (similarity, market quantiles, evidence tiers, competitor bands, Monte Carlo, optimizer) | **Implemented, gated** | 178 unit tests; readiness NO_GO L1 33/100 |
| F17 | Prediction clock + blind scorecard | **Implemented** | 845 tenders tracked, 2 scored, min 150 |
| F18 | GASTAT price indices + CPI restatement | **Implemented** | 1,052 rows, model `p2w-0.2.0` |
| F19 | Pricing-seed baselines | **Implemented, contains fabricated default** | `baseline_default = 100,000` |
| F20 | Superset analytics (22 charts, drill, themes) | **Implemented** | 22/22 render; role isolation tests |
| F21 | Document ingestion, BoQ parsing, LLM compliance extraction | **Partially implemented** | 1 document, 10 BoQ rows, no upload API, virus-scan hook never flips, LLM needs key |
| F22 | Source lineage endpoint | **Appears implemented, empty** | `source_lineage` has 0 rows |
| F23 | Readiness endpoint | **Implemented** | measured counts |
| F24 | Backups + restore drill | **Implemented** | nightly cron, drill passed 14 tables |
| F25 | Production compose, secrets, tunnel, provisioning | **Implemented, not deployed** | rehearsed locally; no VM yet |
| F26 | Public Netlify demo | **Implemented, unsafe for launch** | client-side gate, stale snapshot |
| F27 | Billing / subscriptions | **NOT BUILT** | — |
| F28 | Security headers, global rate limits, WAF rules | **NOT BUILT** (login throttle only) | — |
| F29 | Uptime, error tracking, metrics | **NOT BUILT** (ops lanes only) | — |
| F30 | Terms, privacy, data notice | **NOT BUILT** | — |
| F31 | BoQ upload + automatic review (contracting focus) | **NOT BUILT** (design agreed 16 Sep) | — |

---

## 4. User Personas

| Persona | Who | Needs | Risk if we get it wrong |
|---|---|---|---|
| **Bid manager (contractor)** | Runs 5–30 bids/month for a mechanical/civil contractor | Fit-for-me tenders fast, agency history, item-level price sanity | Loses a bid on a wrong price hint → blames the platform |
| **Sales lead (supplier)** | Sells equipment/services to government | Alerts by activity/agency, competitor win rates | Misses a tender because an alert silently failed |
| **Consultant** | Follows tenders for several clients | Multiple profiles, exports | Cross-client data leak = business-ending |
| **Platform operator (Thaqip)** | Runs the service | Pipeline health, backups, tenant admin, kill switches | Blind outage or silent data corruption |

---

## 5. User Journeys

**J1 – Invitation to first alert (beta):** operator creates tenant + user → one-time password sent out-of-band → user logs in behind Cloudflare Access → forced password change → links Telegram via deep link → creates an alert profile → receives first Telegram message on a matching tender. *Verification:* `notifications.status='sent'` with a Telegram message id, `auth_events` shows `password_changed`.

**J2 – Evaluate a tender:** open tender → observed comparables (awards with source links) → market range if the evidence tier permits, else an explicit refusal → add to War Room → compliance matrix → record outcome after award.

**J3 – Contracting BoQ review (planned F31):** upload priced BoQ → deterministic checks (arithmetic, inversions, concentration, text-numbers) → per-line comparison only where ≥5 independent contributors exist → private storage per tenant.

**J4 – Operator day:** dashboard ops summary → lanes stale? → backup lane green? → prediction clock progress → tenant admin.

---

## 6. Functional Requirements

FR-1 Ingestion latency p50 < 15 min from Etimad publish to `tenders.detected_at` (measured by `/api/freshness/trend`).
FR-2 Every notification row must end in `sent` or `failed` with a provider reference or error; `pending` older than 1 h is an incident.
FR-3 Every private table read is tenant-filtered (enforced by static test `test_console_reads_of_private_tables_are_tenant_filtered`).
FR-4 Every prediction object carries `kind`, `model_version`, `evidence_tier`, `confidence_score`, `is_suppressed`, `suppression_reason`; suppressed ⇒ no numbers.
FR-5 No accuracy percentage is displayed with fewer than 150 blind, scorable outcomes.
FR-6 Accounts: invite, forced password change, reset, deactivate user, deactivate tenant, platform-admin-only routes.
FR-7 Exports (CSV) contain only the caller's tenant data plus shared corpus.
FR-8 BoQ upload accepts .xlsx ≤ 10 MB, parses ≥ 95% of rows of the reference file, stores privately, never enters shared benchmarks without consent flag.

---

## 7. Non-Functional Requirements

| Area | Requirement | Current |
|---|---|---|
| Availability | 99.5%/month for app + API (beta) | unmeasured; laptop |
| Latency | p95 API < 800 ms at 50 concurrent users | unmeasured |
| Data freshness | listing ≤ 15 min, awards ≤ 6 h, indices ≤ 24 h | listing OK; awards OK; indices OK |
| Backup | nightly, restore-tested weekly, off-machine | nightly local; drill weekly; **not off-machine** |
| Security | TLS, Secure/HttpOnly cookies, throttled login, tenant isolation in DB, no secrets in repo | all except TLS (needs deploy) |
| Privacy | user data hosted in KSA, minimal PII | planned (Oracle Riyadh) |
| Accessibility | RTL, keyboard focus, contrast AA | RTL yes; contrast partially reviewed |

---

## 8. System Architecture

```
Etimad/Forsah ─► poller/forsah/awards (Python, httpx+Playwright) ─► Postgres (pgvector)
                                                  │ outbox(ingest_events)
                                                  ▼
                                   relay ─► Redis stream ─► alerts (Telegram) / indexer (Typesense)
prediction-clock / pricing-seed / ops-health ─► Postgres
console (FastAPI + single-page Arabic UI) ─► Postgres, Typesense, Telegram
superset (read-only role superset_ro) ─► analytics schema views
cloudflared (prod) ─► console:8080, superset:8088   (no inbound ports; Access policies)
host cron: awards-harvest, awards-backfill, reconcile, digest, reminders, award-watch, backup, restore-drill
```

**Weaknesses (§15 expands):** single Postgres, single host, scrapers depend on Etimad WAF behaviour, console is a 4,000-line module + 2,900-line HTML file, no queue for long jobs, no metrics.

---

## 9. Data Architecture

Source of truth per entity:

| Data | Source of truth | Freshness | Validation | If unavailable |
|---|---|---|---|---|
| Tenders | Etimad listing/detail JSON (`payload` kept) | 5-min poll | schema via pydantic, content_hash delta | last snapshot served with `detected_at` shown |
| Offers/awards | Etimad awarding component HTML, parsed | 6 h fresh lane; backfill hourly | Decimal parse, winner ∈ bidders | none created; tender shows "no award seen" |
| Award first-seen | `tender_award_first_seen` written once | on harvest | immutable (tested) | scoring blocked for that tender |
| Price indices | GASTAT via DataSaudi API | daily | `cpi.general` non-empty else abort refresh | model falls back to flat 2% per comparable, factor recorded |
| Predictions | computed, stored with `model_version`, seed, snapshot id | daily blind snapshot | contract invariants (p10≤p50≤p90, suppressed⇒NULLs) | refusal object |
| Users/sessions | Postgres | live | scrypt hash, sha256 token | 401 |
| Search index | Typesense fed by indexer | seconds | — | 503 (no silent DB fallback) |

Prohibited: any UI number not traceable to one of these rows.

---

## 10. Feature-by-Feature Requirements

Format per feature: purpose · behaviour · inputs/outputs · rules · implementation · failure/loading/empty states · verification.

### F1–F4 Ingestion (Etimad listing, Forsah, awards fresh/backfill)
- **Behaviour:** poll listing pages, upsert `tenders` by `(source, source_tender_id)`, emit `ingest_events` on create/update/extend/award; harvest awarding component per awarded tender; keep one scraper at a time (`var/etimad_scrape.lock`).
- **Rules:** rate ≤ 1 req/s listing, ≤ 0.5 req/s awarding; 429/400 ⇒ back off, record `cooldown`, keep cursor (`last_page` = last completed page, tested).
- **Failure:** WAF cool-off ends the session with `ok=true, cooldown=true`; network error ⇒ `ok=false` with error text; ops lane turns red after `expected_minutes`.
- **Logging/monitoring:** `ingest_runs` row per run; freshness p50/p95 endpoint.
- **Verification:** `SELECT max(detected_at)` within 15 min; `etimad.awards_backfill.checkpoint.last_page` strictly increasing across sessions.
- **Gaps:** `awarded_at` never parsed (NULL everywhere) — P1; Forsah `agency_name_raw='partner'` — P1 (task filed).

### F5–F7 Alerts
- **Behaviour:** relay batches `ingest_events` to Redis stream; alerts worker matches profiles (keywords/activities/agencies/sources/event types), renders Arabic, sends via channel, writes `notifications`.
- **Rules:** backfilled awards (`data.backfill=true`) never notify (tested); digest intervals queue as `pending` with reason.
- **Failure:** Telegram 400/403 ⇒ `failed` with provider text; token missing ⇒ `pending` with reason. **Required:** a retry policy (3 attempts, exponential) and a UI surface listing failed deliveries per profile — NOT BUILT.
- **Email channel:** remove the option from the UI until an SMTP/Resend worker exists (P0 for "no fake UI"). When built: Resend API, DKIM on domain, bounce handling, `provider_message_id` stored.
- **Verification:** for each `sent` row, `provider_message_id` present (column NOT BUILT — add), and a synthetic tender event produces a real Telegram message in a test chat.

### F8 Search
- Typesense collection `tenders`; console proxies queries; 503 on outage. **Required:** index rebuild job on schedule and a staleness check (index count vs DB count, alert if drift > 2%) — NOT BUILT.

### F9–F11 Authentication and accounts
- **Implemented (VERIFIED):** `POST /api/auth/login` (scrypt N=2^15, 96 MiB cap), httpOnly SameSite=Lax cookie, Secure forced behind tunnel, sessions revocable, `auth_events`, throttle 8/user-ip and 30/ip per 15 min, `CF-Connecting-IP` trusted only with `THAQIP_TRUSTED_PROXY=cloudflare`, service bearer token for workers/tests, middleware gate on every non-public path including `/docs`.
- **Partial (UNVERIFIED, uncommitted):** 0021 adds `platform_admin`, `must_change_password`, tenant `active`, per-tenant `user_calculator_prefs`; `auth.py` gained `one_time_password()`, `password_problem()`, `revoke_user_sessions()`, principal now includes `platform_admin`.
- **Must implement:**
  - `POST /api/admin/tenants` (platform admin) → create tenant + first user, returns one-time password once; logs `account_created`.
  - `POST /api/admin/users`, `PATCH /api/admin/users/{id}` (deactivate/reactivate/role), `POST /api/admin/users/{id}/reset-password` (new OTP, `must_change_password=true`, revoke sessions).
  - `POST /api/auth/change-password` (current+new; policy; revokes other sessions; clears flag).
  - Middleware: when `must_change_password`, allow only `/api/auth/me`, `/api/auth/change-password`, `/api/auth/logout`; UI shows change-password screen.
  - `/api/settings` PATCH must use `(tenant_id)` PK and delete the 409 branch; the static-audit exemption for `user_calculator_prefs` must be removed.
  - Deactivated tenant ⇒ all its sessions fail (`session_principal` already joins `t.active`, UNVERIFIED).
- **Tests required:** invite→login→forced change→access; reset revokes sessions; deactivated tenant 401; non-platform-admin gets 403 on admin routes; two tenants save different prefs.

### F12–F14 Dashboard, discovery, War Room
- All numbers SQL-derived (VERIFIED for dashboard endpoints). **Required:** remove `dev-console` default target; disable the legacy `/api/tenders/{id}/price-curve` if it uses the heuristic (UNVERIFIED — inspect); War Room simulator button must call the P2W scenario endpoint, not `simulate-price`.
- Empty states exist in UI for most lists (VERIFIED visually for alerts/dashboard; UNVERIFIED for vendors/agencies when filters return nothing).

### F15 Legacy simulator — **PRODUCTION-DANGEROUS**
- `simulate_price` derives `win_prob` from a logistic curve with constants 4.0 and 0.95 and "optimal price" from `median_award × {0.96,0.92,0.86}`. No calibration data, no evidence gate, presented as "Dynamic Win-Probability Simulator".
- **Decision:** remove endpoint and UI call before beta, or return 410 like `competitor-prices`. If kept for internal use, label `kind: "heuristic"` and never show a recommended price.

### F16–F17 P2W engine and prediction clock
- Contracts enforce suppression invariants; evidence tiers A–D; `calibrated=False` on participation estimates by design; optimizer refuses infeasible margins (tested).
- Clock: daily blind snapshot per open Etimad tender; scorecard view applies blindness = before `min(offers_opening_date, award first seen)`; multi-awardee excluded; refusals recorded not scored (8 tests).
- **Required fixes:** exclude `baseline_default` from `pricing_accuracy` (or stop writing it); readiness report update; a public statement of accuracy only when `scored ≥ 150`.
- **Model training (Modal, investor item 4):** must be evaluated on the blind scorecard only; no metric computed on data visible after `as_of`; model version bump per training run; rollback = pin previous `MODEL_VERSION`.

### F18 GASTAT indices — implemented; add unit test that refresh aborts when `cpi.general` empty (exists in code, test UNVERIFIED).

### F19 Pricing-seed — restrict to `baseline_activity_history` and `baseline_booklet_price`; delete the 100,000 default path and its 4 rows.

### F20 Superset — implemented; keep behind Access admins-only; disable public exposure; add a weekly `verify.py` run in cron.

### F21 Documents/BoQ/LLM — currently script-only. For F31: `POST /api/boq/upload` (multipart, ≤10 MB, xlsx only, magic-byte check, stored in MinIO under tenant prefix, `scan_status` must be `clean` or the file is quarantined — implement ClamAV container or drop the hook and state files are not scanned), parser `parse_boq_xlsx` (exists), deterministic review rules (arithmetic, unit-price rounding, numbers-as-text, duplicate lines, inversions by capacity keyword, top-10 concentration), results stored in `boq_reviews(tenant_id, document_id, findings jsonb)`. Benchmarks only when `contributor_tenants ≥ 5`.

### F22 Lineage — either populate `source_lineage` from ingestion (tender_id, offer_id, source URL, fetched_at, content hash) or remove the endpoint. Empty provenance endpoints are a misleading feature.

### F24–F25 Backups, deployment — implemented; **off-machine copy** (rclone to Oracle Object Storage) is P0 for beta.

### F26 Demo — take down or replace with a static marketing page; delete `deploy/netlify/data/db.json` from the repo history? (Contains public tender data only — acceptable to keep in history; remove from deploy.)

### F27 Billing — out of beta scope; when built: Moyasar/Tap, ZATCA e-invoicing, server-side webhook verification, idempotent order updates, never mark paid from client.

---

## 11. Existing Feature Audit (authenticity)

| Feature | Classification | Evidence / what makes it look more complete than it is |
|---|---|---|
| Dashboard KPIs | Fully functional | SQL; matches DB counts |
| "دقة توقع السعر" card | Functional, honest | shows 0/10 and clock progress; no % below floor |
| Legacy simulate-price | **Returning predetermined results** | hardcoded curve; UI presents as simulation |
| pricing_seed default | **Fabricated value** | 100,000 SAR with no evidence; enters `baseline_*` scoring |
| Email alert option | **Coming-soon behind functional UI** | rows `pending` forever |
| Telegram failed rows | Silent failure surfaced only as counts | 56 failures, no retry/UI |
| Lineage endpoint | **Placeholder-based** (empty table) | returns nothing useful |
| Netlify demo gate | **Simulated authentication** | client-side hash |
| Superset | Fully functional | DB-enforced read-only |
| Prediction clock | Fully functional | scorecard tests |
| Readiness endpoint | Functional | measured |
| Accounts (0021) | Partially functional, UNVERIFIED | no endpoints |
| Documents/LLM | Partially functional | no API; key absent; scan hook inert |
| Forsah agency | Misleading data | `"partner"` displayed as an agency |
| awards.awarded_at | Missing data | NULL everywhere; UI must not show award dates |

---

## 12. Integration Requirements

| Integration | Auth | Failure handling | Verification |
|---|---|---|---|
| Etimad (scrape) | Playwright session cookies | WAF cool-off, circuit breaker, checkpoints | run rows; sample audit job (`audit.py`) |
| Forsah API | none | HTTP errors logged | row counts |
| DataSaudi (GASTAT) | none | abort on empty CPI | `price_indices` latest month |
| Telegram Bot API | token in `var/telegram.env` | 400/403 ⇒ failed; retries NOT BUILT | test message endpoint |
| Cloudflare Tunnel/Access | tunnel token | container restart | `cloudflared` metrics :2000 |
| Typesense | API key | 503 | collection count |
| Resend (email) — NOT BUILT | API key | bounce webhook | message id |
| Modal (training) — NOT BUILT | token | job failure ⇒ no model version change | blind-scorecard eval report |

---

## 13. Security and Privacy Requirements

- Server-side authorization on every route (VERIFIED via middleware); platform-admin routes must additionally check `platform_admin` (NOT BUILT).
- Security headers (NOT BUILT): `Content-Security-Policy` (self + cdn.tailwindcss.com + fonts), `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Content-Type-Options`, HSTS at Cloudflare.
- Rate limiting (NOT BUILT beyond login): Cloudflare rule 300 req/min/IP on `/api/*`; app-level 60 req/min on exports and scenario creation.
- CSRF: JSON-only bodies + SameSite=Lax; add `Origin` check on state-changing routes (P1).
- Uploads (F31): size, MIME sniff, xlsx-only, no macros, stored outside web root, tenant-prefixed keys.
- Secrets: `.env`, `var/*.env` gitignored; history scanned clean (VERIFIED). Rotate the service token and admin password on the production host (provision script does).
- Privacy (PDPL): host in KSA; collect username, email, IP in auth events; retention 180 days for auth events (job NOT BUILT); privacy policy NOT BUILT.
- Tenant isolation: keep the static SQL audit test; add runtime test with two real tenants on prod after deploy.

---

## 14. AI/Data Accuracy Requirements

- **Grounding:** market ranges only from awarded comparables with similarity ≥ threshold; competitor bands only at tier A/B; every factor lists `evidence_ref`.
- **Refusal:** tier D or empty sample ⇒ `is_suppressed=true` with reason; UI shows the refusal text, never a placeholder number.
- **Confidence:** `confidence_score` is evidence-derived and explicitly "not a win probability" (`confidence_is_not_win_probability: true`).
- **Accuracy claims:** only from `prediction_scorecard` with `scored ≥ 150`; published as interval hit rate + median APE with model version.
- **LLM extraction (compliance):** output must cite the source chunk; unmatched items flagged `needs_review`; never auto-mark compliance as met.
- **Prohibited claims:** "predicted competitor price", "guaranteed win", any accuracy % below floor, any number from `baseline_default`.
- **Human review:** BoQ catalogue matching (F31) requires confirmation before a line enters benchmarks.

---

## 15. Failure Handling

| Failure | Behaviour required | Current |
|---|---|---|
| Postgres down | console 503 JSON; workers restart (`depends_on` restart) | compose healthchecks VERIFIED |
| Redis down | relay retries; alerts pause; no event loss (outbox) | outbox VERIFIED; retry UNVERIFIED |
| Etimad WAF | cool-off recorded; cursor kept | VERIFIED |
| Telegram 4xx | failed row; retry ×3; operator alert | retry NOT BUILT |
| Typesense down | 503 on search; rest of app works | VERIFIED |
| Disk > 85% | alert; log rotation | rotation VERIFIED; alert NOT BUILT |
| Backup failed | ops lane red; Telegram alert | lane VERIFIED; alert NOT BUILT |
| Reboot | systemd unit brings stack up healthy | unit written; UNVERIFIED on a server |

---

## 16. Testing Strategy

Existing: 1,123 ingestion tests (unit, contracts, live-API batteries, security battery, Playwright e2e), 24 console tests, Superset browser verifier. Live batteries fail (not skip) on a rejected credential.

Required additions: accounts lifecycle; alert retry; fake-functionality suite (§18); load test (k6, 50 VU, 15 min); chaos (kill postgres, fill disk); prod smoke run against the VM.

---

## 17. Feature Testing Matrix (representative; IDs are canonical)

| ID | Feature | Type | Preconditions | Steps | Pass criteria | Severity | Auto |
|---|---|---|---|---|---|---|---|
| T-ING-01 | Listing freshness | integration | stack up | wait 20 min; query trend | p50 < 15 min | P1 | yes (exists) |
| T-ING-02 | Backfill cursor | unit | fake pool | cool-off after 2 pages | checkpoint = last completed | P1 | yes (exists) |
| T-ALR-01 | Backfill never alerts | unit | event with backfill=true | handle | 0 notifications | P0 | yes (exists) |
| T-ALR-02 | Telegram real send | e2e | token + linked chat | trigger synthetic event | row `sent` with provider id | P0 | manual→auto |
| T-ALR-03 | Email option absent | ui | — | open alerts view | no email option | P0 | yes (add) |
| T-AUTH-01 | Anonymous gate | live | — | GET 12 paths | all 401 | P0 | yes (exists) |
| T-AUTH-02 | Throttle per visitor | live | proxy mode | 9 fails from A, 1 from B | A→429, B→401 | P1 | yes (exists staging) |
| T-AUTH-03 | Invite → forced change | e2e | admin | create user, login OTP, hit /api/tenders | 403 until changed, then 200 | P0 | add |
| T-AUTH-04 | Reset revokes sessions | integration | user logged in | reset | old cookie 401 | P0 | add |
| T-TEN-01 | Forged header | live | two tenants | 13 header variants | never unlocks canary | P0 | yes (exists) |
| T-TEN-02 | Prefs per tenant | integration | two tenants | PATCH settings both | each reads own | P1 | add |
| T-P2W-01 | Suppression invariant | unit | — | build suppressed prediction | NULL quantiles | P0 | yes (exists) |
| T-P2W-02 | No accuracy below floor | live | scored < 150 | GET accuracy | all % null | P0 | yes (exists) |
| T-P2W-03 | Blind rule | integration | fixtures | 6 cases | as specified | P0 | yes (exists) |
| T-P2W-04 | No baseline_default scored | integration | seed row | GET accuracy | excluded | P0 | add |
| T-SIM-01 | Legacy simulator gone | live | — | POST simulate-price | 410 | P0 | add |
| T-BAK-01 | Restore drill | ops | backup | run drill | 14 tables match | P0 | yes (script) |
| T-DEP-01 | Prod config | unit | docker | render | volumes, no ports, secrets required | P0 | yes (exists) |
| T-SEC-01 | Headers | live | prod | GET / | CSP, XFO present | P1 | add |
| T-SEC-02 | superset_ro isolation | live | db | SELECT public.* | denied | P0 | yes (exists) |
| T-BOQ-01 | Parse reference file | unit | TXC xlsx | parse | 93 rows, total 28,669,108 ±1 | P1 | add |
| T-BOQ-02 | Text-number cells | unit | file | parse | 63 text cells parsed | P1 | add |
| T-LOAD-01 | 50 VU | perf | prod | k6 15 min | p95 < 800 ms, 0 5xx | P1 | add |
| T-CHAOS-01 | Reboot | ops | VM | reboot | all healthy < 5 min | P0 | manual |

---

## 18. Fake Functionality Detection Suite

| Check | How to verify genuinely |
|---|---|
| UI number vs DB | For each dashboard KPI, fetch `/api/dashboard` and run the equivalent SQL; assert equality (add `tests/test_dashboard_truth.py`) |
| Static API responses | Call each GET twice after inserting a row through the DB; response must change |
| Random/hardcoded stats | grep for `random`, numeric literals in response builders; allowlist only documented constants (P2W tunables) |
| Buttons without backend | Playwright: click every `<button>` with `onclick`, assert a network request or a documented pure-UI action |
| Optimistic success | For POSTs, assert UI success only after 2xx (audit `index.html` fetch handlers) |
| Fake auth | Cookie replay after logout ⇒ 401 (exists) |
| Fake notifications | Every `sent` must have provider id (add column + test) |
| Demo data leakage | prod DB must have no tenant `redteam`; no `dev-console` targets; no `battery-*` scenarios (add prod smoke test) |
| AI output as fact | every prediction JSON has `kind: predicted`; observed entries never share values with predicted (exists) |
| Silent fallback | search returns 503 not DB results (exists); CPI fallback recorded in factor (exists) |
| Hidden placeholders | grep "قريباً|coming soon" in UI must be empty |

---

## 19. Performance and Load Testing

Targets (beta): 50 concurrent users, 20 req/s sustained, p95 < 800 ms for list/dashboard, < 3 s for scenario curve (Monte Carlo); DB pool 20 (currently 5 — raise); uvicorn 2 workers. Load test with k6 against the VM through the tunnel; record CPU/RAM (2 OCPU/12 GB budget: stack ~0.9 GB + Superset 0.5 GB).

---

## 20. Security Testing

Existing: anonymous gate, forged headers, IDOR, SQL-shaped slugs, bad bearer, forged cookie, enumeration-safe login, superset_ro isolation. Add: CSRF origin check, XSS via tender names in UI (Etimad-sourced text rendered with `esc()` — audit all `innerHTML` sites), upload abuse (F31), rate-limit bypass via header spoofing when not behind tunnel, dependency audit (`uv` lock + `pip-audit`), external port scan of the VM.

---

## 21. Trade-Off Analysis (selected)

| Decision | Alternative | Why chosen | Consequence |
|---|---|---|---|
| Scrape Etimad | licensed feed | no feed exists | WAF risk, legal exposure, weeks-long backfill; pursue official access |
| Heuristic P2W with hard gates | ML model now | 470 awards is too little data | conservative refusals; accuracy unproven |
| Flat 2% fallback → CPI | sector index | CCI starts 2025-06 | small effect today; recorded per comparable |
| Single Postgres | managed DB | cost 0 | single point of failure; mitigated by tested backups |
| Cloudflare Access invite list | in-app signup | 3-day deadline | 50-user cap; second login step |
| Custom auth | managed IdP | no budget, KSA hosting | must maintain throttle/reset ourselves |
| Sync scenario computation | queue | simplicity | p95 risk under load; add timeout 20 s |

---

## 22. PRODUCTION-DANGEROUS SHORTCUTS

| Shortcut | Why used | Risk | Dev | Staging | Prod | Replace by | Detect |
|---|---|---|---|---|---|---|---|
| `baseline_default=100000` | keep seeding loop alive | fabricated hypothesis scored | ok | no | **no** | skip seeding without evidence | SQL `basis='baseline_default'` |
| Hardcoded logistic win prob | early demo | misleading recommendation | ok | no | **no** | P2W scenario endpoint | grep `math.exp(4.0` |
| Email "قريباً" option | roadmap visibility | fake feature | no | no | **no** | remove until worker | UI grep |
| `dev-console` alert target | dev testing | 400s from Telegram | ok | no | **no** | linked chat default | `target !~ '^-?[0-9]+$'` |
| Client-side demo gate | shareable demo | fake security | ok | no | **no** | Access-protected staging | site up? |
| Laptop hosting | free | data loss, exposure | ok | no | **no** | VM + tunnel | — |
| Local-only backups | speed | fire/theft loses all | ok | no | **no** | rclone off-site | lane check |

---

## 23. Risk Register

| ID | Risk | P | S | Impact | Detection | Mitigation | Owner | Blocking |
|---|---|---|---|---|---|---|---|---|
| R1 | Etimad blocks scraper | H | H | freshness stops | lanes red | rate limits, backoff, official access request | eng | no |
| R2 | Legal challenge on data reuse | M | H | shutdown | — | legal review (unfunded), public-data notice | owner | before paid |
| R3 | Wrong price hint → customer loss | M | H | reputation | complaints | hide/label; blind scorecard | eng | yes (P0) |
| R4 | Cross-tenant leak | L | Critical | business-ending | security battery | DB-level filters, tests, Access | eng | yes |
| R5 | Oracle capacity/reclaim | H | M | outage | uptime monitor | PAYG, fallback host | owner | yes |
| R6 | Backup unrestorable | L | Critical | data loss | weekly drill | drill + off-site | eng | yes |
| R7 | Telegram failures unnoticed | H | M | missed alerts | failed rows | retry + UI + alert | eng | P1 |
| R8 | PDPL breach | M | H | fines | — | KSA hosting, policy, minimal PII | owner | before public |
| R9 | Fabricated values in scoring | H (exists) | M | false accuracy | SQL check | fix F19 | eng | yes |
| R10 | Console monolith regressions | M | M | outages | tests | keep batteries; split module | eng | no |

---

## 24. Observability Requirements

Logs: JSON logs per container, rotated (VERIFIED). Metrics (NOT BUILT): request latency histogram, 5xx rate, worker run durations, queue length (`XLEN thaqip.events`), notifications by status, prediction clock counts. Traces: not required for beta. Error reporting: Sentry free tier. Uptime: external 1-min check on `/api/health` via tunnel. Alerts (Telegram to operator): any lane stale, backup failed, `notifications.failed` > 5/h, disk > 85%, 5xx > 1%/5 min, login throttle > 20 denials/h.

---

## 25. Implementation Roadmap

**Phase A – Truthfulness (P0, 1–2 days):** remove email option; 410 legacy simulator + switch UI to P2W; stop `baseline_default` and exclude from scoring; fix `dev-console` targets; take down Netlify demo; hide award dates.
**Phase B – Accounts (P0, 2 days):** finish F11 endpoints + middleware + UI + tests; commit.
**Phase C – Hosting (P0, 1 day after owner accounts):** provision VM, tunnel, Access, off-site backups, reboot test, prod smoke tests.
**Phase D – Operability (P0/P1, 1 day):** uptime, Sentry, Telegram operator alerts, notification retry, security headers, rate limits.
**Phase E – Legal (P0 text, review unfunded):** Terms, privacy, data notice, prediction disclaimer.
**Phase F – Contracting BoQ (P1, 1–2 weeks):** upload, review rules, private storage, catalogue, consented benchmarks.
**Phase G – Data quality (P1):** awarded_at parsing, Forsah agency, lineage population, awards backfill monitoring.
**Phase H – Model training (P2, after ≥150 scored):** Modal pipeline evaluated on blind scorecard only.

---

## 26. Definition of Done (per feature)

Frontend + backend + DB + API implemented; real integration; auth and authz enforced server-side; input/output validation; error, loading, empty, timeout states; logs and a lane/metric; automated unit + integration + (where user-facing) e2e + negative tests passing; security battery unchanged or extended; docs updated; no placeholder text, no dev defaults, no fake success; rollback documented (migration down-path or feature flag).

---

## 27. Release Gates

- **READY FOR DEVELOPMENT:** repo cloned, `docker compose up` healthy, tests pass. ✔
- **READY FOR INTERNAL TESTING:** all Phase A items merged; accounts endpoints exist; test suites green. ✖ (Phase A/B pending)
- **READY FOR STAGING:** deployed on VM behind tunnel with prod compose; restore drill on VM passed; external port scan shows 22 only. ✖
- **READY FOR LIMITED BETA:** all P0 in §29 closed; uptime + error tracking live; legal pages published; 2-tenant isolation test on prod passed; load test passed. ✖
- **READY FOR PUBLIC PRODUCTION:** self-signup with verification, billing with server-verified webhooks, legal review completed, 30 days of beta with < 0.5% 5xx and no P0 incidents. ✖

---

## 28. Production Readiness Checklist

- [ ] Phase A truthfulness fixes merged and tested
- [ ] Accounts lifecycle (invite/reset/must-change/deactivate) with tests
- [ ] VM + tunnel + Access; no inbound ports; HTTPS A
- [ ] Off-site backups + drill on VM
- [ ] Uptime, Sentry, operator alerts
- [ ] Notification retry + failed-delivery UI
- [ ] Security headers + rate limits + CSRF origin check
- [ ] Terms/Privacy/Data notice live in Arabic
- [ ] Netlify demo removed
- [ ] Prod smoke tests (no demo tenants, no dev targets)
- [ ] Load test passed
- [ ] Reboot test passed

---

## 29. Production Blockers (P0)

1. Legacy simulator reachable from UI.
2. `baseline_default` fabricated hypotheses on the scoring path.
3. Email channel visible without a worker.
4. No customer account lifecycle.
5. Hosting on a laptop; backups local only.
6. No uptime/error monitoring or operator alerts.
7. No legal pages.
8. Public demo with client-side gate.
9. Telegram failed deliveries without retry or visibility.

P1 (fix immediately after): security headers/rate limits; awarded_at; Forsah agency label; lineage; DB pool size; notification provider ids.

---

## 30. Final Production Audit

**A. Works for real:** ingestion, awards harvest/backfill, outbox/relay/alerts (Telegram path when token set), auth/sessions/throttle, tenant isolation, dashboard/discovery/war room CRUD, P2W gates and refusals, prediction clock + scorecard, GASTAT indices, Superset, backups + drill, prod compose config, test batteries.
**B. Only appears to work:** email channel; lineage endpoint; legacy simulator's "win probability"; demo site security.
**C. Partially implemented:** accounts (0021), documents/BoQ/LLM, notification failure handling.
**D. Simulated:** demo password gate.
**E. Mocked:** none in product paths (test fakes only).
**F. Hardcoded:** logistic constants and risk multipliers in `simulate_price` and `pricing_seed`; `baseline_default` 100,000; default `dev-console` target; P2W tunables (documented, acceptable).
**G. Could fabricate results:** `baseline_default`; legacy simulator.
**H. Could mislead users:** simulator recommendation; `"partner"` agency; award dates absent; email option; failed alerts shown only as counts.
**I. Security/privacy failure modes:** no headers/rate limits; laptop exposure history; no privacy policy; auth-event retention undefined.
**J. Could fail under load:** DB pool 5; sync Monte Carlo; single uvicorn worker.
**K. Could corrupt/lose data:** local-only backups; awards delete/re-insert on every harvest (mitigated by first-seen table); no off-site copy.
**L. Fix before any public launch:** §29 list.
**M. Safe to postpone:** billing, self-signup, Modal training, lineage UI, mobile polish.
**N. Technical debt:** 4k-line `app.py`, 2.9k-line `index.html`, two pricing code paths (legacy vs P2W), env-driven feature toggles without a registry.
**O. Production blockers:** the nine items in §29.

---

## 31. Recommended Next Actions

1. Execute Phase A today (small, high-truth-value diffs) and commit with tests T-ALR-03, T-SIM-01, T-P2W-04.
2. Finish and commit F11 accounts (currently uncommitted and unverified on disk).
3. As soon as Cloudflare/Oracle exist: provision, off-site backups, reboot and isolation tests on prod.
4. Wire operator alerts and notification retry before inviting anyone.
5. Draft Terms/Privacy/Data notice in Arabic; get quotes for review.
6. Start F31 (BoQ upload + review) with the TXC file as fixture; keep prices private per tenant.
