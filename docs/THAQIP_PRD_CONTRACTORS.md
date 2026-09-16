# Thaqip for Contractors (ثاقب للمقاولين) — Product Requirements

**Status:** blueprint, 16 Sep 2026. Companion to `THAQIP_PRD_v2.md` (platform audit, release gates, anti-fake rules — all of which apply here and are not repeated).
**Labels:** **VERIFIED** (observed in code/DB/tests this week), **UNVERIFIED**, **NOT BUILT**, **DATA WE DO NOT HAVE**.

---

## 1. Executive summary

A contractor pays for a tool when it changes the outcome of a bid: win one more job a year, avoid one bad bid, or cut a week from estimating. Alerts alone do not do that; the competitor already sells alerts at 290 SAR/month. Thaqip for Contractors is worth paying for only if it does three things a contractor cannot do alone:

1. **Show what this agency actually paid for this kind of work, item by item** — not a total, a priced bill of quantities (BoQ) view built from consented uploads and published awards.
2. **Catch the mistakes that lose bids before submission** — arithmetic, inverted unit prices, concentration risk, missing certifications, classification mismatch (the reference file from a real contractor had two of these).
3. **Tell the truth about competition** — who bid at this agency in this activity, how far apart their prices were (median spread in our contracting sample is 87%, so pricing "by feel" is a coin toss), and whether the lowest price wins here (76% of the time in our sample).

**Hard constraint the product must be built around:** Etimad publishes bid **totals**, never line items. The item-level price intelligence that justifies a paid tier can only come from contractors' own priced BoQs, pooled with explicit consent and shown only when at least five independent companies have contributed. That data engine is the product; everything else is the on-ramp to it.

**Current fit of our data (VERIFIED, 16 Sep):** 589 contracting-type tenders in the corpus, but awards are skewed to small direct purchases (88 of 90 contracting awards are under 250,000 SAR; 2 are above). Large projects like the reference file (28.7M SAR hospital rehabilitation) are nearly absent because the corpus is eight weeks old and the historical backfill has only just started. **The contracting product cannot be sold on today's data; the plan below sequences data acquisition before paid features.**

---

## 2. Who pays, and for what

| Persona | Company | What they do today | What they would pay to stop doing |
|---|---|---|---|
| **Estimator / quantity surveyor** | Grade 3–5 general or MEP contractor, 20–200 staff | Prices 93-line BoQs in Excel from supplier quotes and memory; re-keys the same items every tender | Re-pricing from scratch; not knowing what the agency accepted last time |
| **Bid manager / tender officer** | Same | Watches Etimad daily, downloads booklets, tracks deadlines in WhatsApp | Missing a tender that fits the company's classification; missing an addendum; missing a site-visit date |
| **Owner / GM (small contractor, < 20 staff)** | Subcontractor, maintenance, fit-out | Bids on direct purchases and small competitions; decides go/no-go by gut | Bidding 12 times to win 1; not knowing which agencies pay on time (**DATA WE DO NOT HAVE**) |
| **MEP specialist subcontractor** | HVAC, electrical, fire, medical gas | Prices packages for main contractors | Finding which main contractors won which projects to sell into |

Buying trigger: a lost bid where the winner was 25–40% lower; a booklet with 100+ lines and 10 days to price it.

---

## 3. Jobs to be done and the "worth paying for" test

Each feature below must pass one test before it ships in a paid tier: **a named contractor can point to a decision it changed.** Features that only look informative are cut.

| Job | Feature | Value evidence required before charging |
|---|---|---|
| "Show me only tenders I can legally bid on" | Eligibility filter (classification field, grade, region, activity) | ≥ 80% of alerts judged relevant by 5 design partners |
| "What did this agency pay for this work before?" | Agency × activity award history with BoQ-level prices where available | design partner uses it in a submitted bid |
| "Is my BoQ wrong somewhere?" | Deterministic BoQ review | catches ≥ 1 real error per 3 uploads in pilot |
| "Where are my prices vs the market?" | Item benchmarks (consented pool) | ≥ 5 contributors per item before display; partner confirms usefulness |
| "Who will I be up against?" | Competitor field for this agency/activity | matches actual bidders ≥ 60% on awarded tenders (measurable) |
| "Should we bid at all?" | Go/no-go scorecard (fit, competition, evidence) | partners' win rate on "go" ≥ 1.5× their baseline over 6 months |
| "Don't let us miss a date" | Deadline, enquiry, site-visit, addendum tracking | zero missed deadlines for tracked pursuits |

---

## 4. What exists vs what this product needs

| Capability | Status | Gap for contractors |
|---|---|---|
| Etimad/Forsah ingestion, alerts, Telegram | **VERIFIED** | no classification/grade fields parsed; no addendum change events (UNVERIFIED whether `tender.updated` covers booklet changes) |
| Award/offer harvest | **VERIFIED** | large-project awards scarce; `awarded_at` NULL |
| War Room (pursuits, stages, compliance matrix) | **VERIFIED** | compliance items are generic; no site-visit/enquiry milestones |
| BoQ parser (`boq.py`) | **VERIFIED code**, 10 rows in DB | no upload API, no review rules, no catalogue |
| Document ingestion + LLM compliance extraction | **UNVERIFIED** (no API key, 1 document) | must extract classification requirement, bond %, site-visit, penalties |
| P2W market ranges | **VERIFIED, gated** | whole-tender ranges only; item level NOT BUILT |
| Competitor profiles | **VERIFIED** | activity-level; not linked to contractor classification |
| Accounts / tenant privacy | **partial, UNVERIFIED** | prerequisite for storing any contractor's prices |
| Contractor classification data (Saudi Contractors Authority / MOMRA grades) | **NOT BUILT / DATA WE DO NOT HAVE** | needs source (tender booklet text, or licensed dataset) |

---

## 5. Product definition

### 5.1 Eligibility & fit (on-ramp, free tier)
- **Inputs:** company profile: activities (ISIC-like list already in `activities`), classification fields and grade (self-declared, validated later against booklet requirements), regions, min/max project value, certifications (ISO, civil defence licence, medical-gas approvals).
- **Behaviour:** every new tender is scored: activity match, region match, value band match, classification requirement extracted from the details report (the reference PDF lists "مجال التصنيف: أعمال الميكانيكية، نظام التكييف المركزي، المباني الخرسانية" — parse this field from Etimad's `OpenTenderDetailsReport`).
- **Output:** fit score 0–100 with reasons; alert only above a user threshold.
- **Source of truth:** Etimad details report per tender (fetch and store `classification_fields[]`, `execution_location`, `site_visit_date`, `expected_award_date`, `work_start_date`, `stop_period_days` — the reference PDF shows all of these exist). **NOT BUILT:** details-report fetch in the poller. **Verification:** stored fields equal the PDF for a sample of 30 tenders.
- **Failure:** if the report is unavailable, fit score is computed without classification and shown as "غير مؤكد التصنيف"; never assume eligibility.

### 5.2 Bid pipeline with contracting milestones (paid: نافس)
Extends War Room stages with: booklet purchased → site visit (date, attendee) → enquiries deadline → addenda received (count, last date) → bond issued (amount = % × bid, bank) → submitted → opened (bid rank if published) → awarded/lost. Reminders at T−3/T−1 days via Telegram for each milestone. **Verification:** reminder rows in `notifications` with milestone ids; e2e test creates a pursuit with a deadline in 2 days and asserts a message.

### 5.3 BoQ workbench (paid: نافس)
- **Upload:** `.xlsx` (Etimad booklet format and the free-form format in the reference file), ≤ 10 MB, virus-scanned or explicitly labelled unscanned, stored under `tenant/<id>/boq/<uuid>`.
- **Parse:** columns detected by header synonyms (`boq.py` does this; extend to Arabic variants: البند/وصف البند/المواصفات/وحدة القياس/الكمية/سعر الوحدة/الإجمالي); numbers stored as text converted; formulas evaluated; unit normalisation (عدد/بالعدد → EA, م2, م.ط, م3, طن, مقطوعية).
- **Deterministic review (no AI):** arithmetic mismatch; unit price with > 2 decimals (back-solved from lump sums); quantity 0 or blank; duplicate description with different price; inverted price by capacity (parse kVA, HP, mm, amps, tons: bigger capacity priced lower ⇒ flag); top-10 lines share of total (> 50% ⇒ concentration warning); category subtotals; VAT presence check; lines with no specification. Each finding: severity, line, explanation in Arabic, suggested fix. **Verification:** the reference file must yield exactly: UPS inversion (lines 55/56), 5 unrounded prices, 63 text-number cells, label/spec mismatch on line 59 (requires keyword rule "خادم" vs "استدعاء الممرضة" — implement as "description mentions system X, spec mentions system Y" using a small term list), 58% top-10 concentration.
- **Catalogue matching (AI-assisted, human-confirmed):** each line is matched to a canonical item ("باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", unit EA). Model proposes, estimator confirms or edits; unconfirmed lines never enter benchmarks. Model outputs carry `kind: "suggested"`, confidence, and the two nearest catalogue candidates.
- **Private by default:** a company's prices are visible only to its tenant. Sharing into the pool is a per-upload consent checkbox with plain Arabic wording; consent is stored with timestamp and can be withdrawn (withdrawal removes future use; already-published aggregates are not recomputed retroactively — say this in the terms).

### 5.4 Item price benchmarks (paid: فز)
- **Display rule:** an item shows a price range only when ≥ 5 distinct tenants contributed within 24 months, and the displayed statistics are p25/p50/p75 with the contributor count and the date range. Never show a single company's price. Never show a range from fewer than 5.
- **Adjustment:** CPI restatement exists (`p2w.indices`); add construction cost index once ≥ 24 months of GASTAT CCI exist (starts 2025-06).
- **Provenance:** each benchmark carries `n_contributors`, `n_lines`, `period`, `index_used`. A benchmark with `n_contributors < 5` cannot exist in the API (DB constraint in the materialised table, plus test).
- **What it is not:** not a recommendation. UI copy: "نطاق أسعار مُلاحَظ من عروض سابقة" with the count.

### 5.5 Competition view for contractors (paid: نافس)
For agency × activity: bidders seen, wins, median ratio to lowest, spread, lowest-wins rate, last seen. All observed (exists at activity level — VERIFIED). Add: "likely field" = vendors with ≥ 3 bids in this activity with this agency in 24 months (observed set, not a prediction), and a clear separation from the P2W participation estimate (which stays `calibrated=false` and labelled).

### 5.6 Go/no-go scorecard (paid: فز)
Inputs: fit score, expected bidders (observed count median for agency×activity), spread, lowest-wins rate, evidence tier for a market range, company's own history (win rate in this activity from its logged outcomes), bond and site-visit cost. Output: a scored recommendation "ادخل / ادرس / تجنّب" with each factor shown and its source. **Rule:** if any factor is missing, it is shown as missing and the score is capped at "ادرس"; no factor is ever defaulted to a favourable value. **Verification:** a test tender with no award history must never produce "ادخل".

### 5.7 Agency intelligence (paid: نافس)
Award history, typical bidder count, typical award size, award timing (**requires `awarded_at` — NOT BUILT**, currently NULL), cancellation rate (needs status tracking — UNVERIFIED), payment behaviour (**DATA WE DO NOT HAVE**; only collectable from customers' own experience, opt-in survey; never inferred).

### 5.8 Team and roles
Estimator (upload, price), bid manager (pipeline, submit), viewer. Per-tenant seats. Platform admin separate. (Accounts F11 in the platform PRD is the prerequisite.)

---

## 6. The price-data engine (the moat)

**Sources, ranked by yield and legitimacy**
1. Consented BoQ uploads (own bids, won or lost) — item prices tied to a real tender, date, agency.
2. Won-bid reconstruction — uploaded BoQ whose total matches the Etimad award (±1%) gets `verified_award=true`; these are the gold rows.
3. Supplier quotes uploaded by contractors — material cost floor per item (optional, phase 3).
4. Published estimated values in booklets, where present (parse; store as `agency_estimate`, distinct from bids).
5. GASTAT indices for time adjustment (exists).
6. Partnerships with quantity-surveying firms (commercial, phase 3).

**Explicitly excluded:** scraping suppliers' or competitors' private systems; using any customer's data outside its consent; buying leaked bid documents.

**Cold start plan:** 10–20 design partners upload their last 12 months of BoQs (typical contractor has 30–100). At 15 partners × 40 BoQs × ~80 lines ≈ 48,000 lines; with catalogue matching, common items (doors, cables, FCUs, tiles, paint) reach 5 contributors within the first month. Track and publish internally: `items_with_benchmark / items_in_catalogue` weekly.

**Data model (NOT BUILT):**
- `boq_documents(id, tenant_id, tender_id?, filename, storage_key, sha256, scan_status, uploaded_by, consent_pool bool, consent_at, total_declared, total_computed)`
- `boq_lines(id, document_id, line_no, category, item, description, spec, unit_raw, unit, qty, unit_price, total, text_numbers bool, catalogue_item_id?, match_confidence, match_confirmed_by?)`
- `boq_findings(id, document_id, line_id?, rule, severity, message_ar, suggestion_ar)`
- `catalogue_items(id, code, name_ar, unit, family, capacity_key?, attributes jsonb, created_by)`
- `item_benchmarks(catalogue_item_id, period_start, period_end, n_contributors CHECK (n_contributors >= 5), n_lines, p25, p50, p75, index_used, computed_at)` — materialised nightly; rows with `< 5` are never written.
- `consents(tenant_id, document_id, scope, granted_at, withdrawn_at)`

---

## 7. Packaging and price (proposal)

| Tier | Price | Includes | Why this price |
|---|---|---|---|
| اكتشف | free | eligibility alerts (5/day), agency history totals, 3 BoQ reviews/month (private) | on-ramp; seeds the pool |
| نافس | 349 SAR/mo | unlimited alerts, pipeline milestones, unlimited BoQ review, competition view, exports, 3 seats | above the alerts competitor (290) because it changes bids, not just finds them |
| فز | 899 SAR/mo | item benchmarks, go/no-go, agency intelligence, 10 seats, priority support | one avoided bad bid or one extra win pays for years |

Benchmarks are not sold until the pool has ≥ 200 catalogue items with ≥ 5 contributors each (a public "pool health" number), so the فز tier launches on evidence, not promise. Billing itself is out of beta scope (see platform PRD F27).

---

## 8. What we will not claim (and how the product enforces it)

- No "recommended bid price" until the blind scorecard reaches 150 scored outcomes and the interval hit rate is published with its model version.
- No benchmark from fewer than 5 companies (DB constraint).
- No competitor "will bid at X" statements; competitor bands remain tier-gated.
- No eligibility guarantee: the fit score says "متوافق مع المتطلبات المنشورة" and links to the booklet field it read.
- No payment-behaviour claims about agencies without opt-in customer reports and a minimum of 5 reports.

---

## 9. Non-functional requirements specific to this product

- Upload to review result ≤ 15 s for a 500-line BoQ (parse + rules); catalogue suggestions may arrive asynchronously (≤ 2 min) with a visible "قيد المطابقة" state.
- Privacy: BoQ objects encrypted at rest (MinIO SSE or volume encryption on the VM), tenant-prefixed keys, access logged in `auth_events`-style table `data_access_events(tenant_id, user_id, document_id, action)`.
- Retention: uploads kept while the tenant is active; deleted 90 days after tenant deactivation; withdrawal of consent stops future pooling within 24 h.
- Arabic-first, RTL, mobile usable for alerts and pipeline; the BoQ workbench is desktop-first.

---

## 10. Roadmap (dependency-ordered)

| Phase | Weeks | Deliverables | Gate |
|---|---|---|---|
| 0. Platform P0s | 1 | accounts, truthfulness fixes, hosting, monitoring, legal pages (platform PRD §29) | beta gate passed |
| 1. Details report + eligibility | 1–2 | details-report fetch (classification, dates), company profile, fit score, alerts by fit | 30-tender sample matches PDFs; partners rate ≥ 80% relevant |
| 2. BoQ workbench | 2–3 | upload, parser (Arabic headers, text numbers), deterministic review, private storage, consent | reference file yields the 5 known findings; 10 partner uploads reviewed |
| 3. Pipeline milestones + competition view | 1 | milestones, reminders, agency×activity competition | zero missed deadlines in pilot month |
| 4. Catalogue + benchmarks | 3–4 | catalogue seed (500 MEP/civil items), AI matching with confirmation, nightly benchmarks with n≥5 constraint | 200 items benchmarked; partner feedback |
| 5. Go/no-go + agency intelligence | 2 | scorecard, `awarded_at` parsing, cancellation tracking | no favourable defaults; scorecard tested on awarded tenders |
| 6. Paid tiers | after 5 | billing, e-invoicing | commercial registration in place |

Data acquisition runs in parallel from week 1: historical backfill prioritised to contracting activities and large tenders (add a category filter to `awards_backfill`), and design-partner BoQ collection.

---

## 11. Testing matrix (additions to the platform matrix)

| ID | Test | Pass criteria | Sev |
|---|---|---|---|
| T-ELIG-01 | details report parse | 30/30 sample tenders: classification fields and 5 dates equal the PDF | P0 |
| T-ELIG-02 | fit score never assumes | tender without report ⇒ status "غير مؤكد", not eligible | P0 |
| T-BOQ-01 | reference file parse | 93 lines, total 28,669,108 ± 1, 63 text cells | P0 |
| T-BOQ-02 | review rules | finds UPS inversion, 5 unrounded, concentration 58%, spec mismatch line 59 | P0 |
| T-BOQ-03 | privacy | tenant B cannot fetch tenant A's document/lines/findings (404), including by id guessing | P0 |
| T-BOQ-04 | consent | unconsented lines never appear in `item_benchmarks` inputs | P0 |
| T-BENCH-01 | n ≥ 5 constraint | inserting a benchmark with 4 contributors fails at DB | P0 |
| T-BENCH-02 | API | benchmark JSON includes n_contributors, period, index_used | P1 |
| T-MATCH-01 | AI suggestions labelled | every suggestion has kind "suggested" and ≥ 2 candidates; unconfirmed excluded | P0 |
| T-GNG-01 | no favourable defaults | missing factors ⇒ cap at "ادرس" | P0 |
| T-MILE-01 | reminders | T−3/T−1 messages sent with provider ids | P1 |
| T-UPL-01 | upload abuse | non-xlsx, > 10 MB, macro-enabled, zip bomb rejected | P0 |
| T-FAKE-01 | no benchmark placeholders | UI shows "لا توجد بيانات كافية" when n < 5; never a number | P0 |

---

## 12. Risks specific to this product

| Risk | Mitigation |
|---|---|
| Cold start: pool too thin to be worth فز | do not sell فز until the pool health gate; free reviews drive uploads |
| Contractors refuse to share prices | private-by-default; benefit-first (review works without sharing); anonymised aggregates only; consent per upload |
| Catalogue mismatch produces wrong benchmarks | human confirmation; two-candidate display; benchmark excludes unconfirmed |
| Large-project data scarcity | backfill prioritisation; partner BoQ history; publish coverage honestly |
| Classification data wrong | parse from booklet, show source, let user correct, never guarantee |
| Legal: pooled bid data | consent text reviewed; no per-company disclosure; withdrawal path |

---

## 13. Success metrics (measured, not claimed)

- Weekly: active contractor tenants, BoQs uploaded, findings per upload, catalogue coverage, benchmark items (n ≥ 5), alert relevance rating.
- Quarterly: partner win-rate change on "go" decisions vs their own prior 12 months (logged outcomes), churn, NPS in Arabic.
- Always public inside the app: pool health and the prediction clock scorecard, so the value claims are visible and falsifiable.
