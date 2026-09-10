# Thaqip Price-to-Win — Readiness Evaluation Packet

**Assessment date:** 2026-09-10
**Assessor:** independent readiness review (all findings re-measured, no agent self-report accepted)
**Commit under review:** `9fbd83d` + uncommitted P2W working tree
**Model version:** `p2w-0.1.0`

---

## 1. Decision

| | |
|---|---|
| **Overall decision** | **NO_GO for GA — DEGRADE to internal design-partner use** |
| **Allowed capability level** | **L1 (market range only), behind authentication that does not yet exist** |
| **Weighted readiness score** | **33 / 100** |
| **Hard red gates** | **4 FAIL, 1 UNVERIFIED, 4 PASS** |

Any hard red-gate FAIL forces NO_GO for the affected capability. Four fail. The system must not be
exposed to any external user in its current state, at any capability level, because there is no
authentication at all.

The engineering quality of the P2W package is genuinely high — the suppression machinery, determinism,
and point-in-time discipline are better than most production ML systems. The blocker is not code
quality. It is that **there is zero real evidence the numbers are right**, and the surrounding
application has no authentication and still ships an ungated competitor-price panel that contradicts
the gated one on the same screen.

---

## 2. Verification performed

Every number below was measured by this assessment, not taken from a report.

| Check | Command | Result |
|---|---|---|
| Full test suite | `uv run pytest -q` (services/ingestion) | **1036 passed**, 0 failed, 206s |
| Lint | `uv run ruff check src tests` | **All checks passed** |
| Stack health | `docker compose ps` | **11/11 Up**, none Restarting |
| Service logs | `docker compose logs --since 60m` | poller/relay/alerts/indexer/ops-health clean; console carries a live `ValueError: Out of range float values are not JSON compliant: nan` |
| Migrations | `schema_migrations` | **15/15 recorded**, incl. `0015_p2w_canonical.sql` |
| 0015 tables | `information_schema.tables` | **9/9 present** |
| `tenant_id` columns | `information_schema.columns` | **7 tables** carry it — `prediction_feedback` does **not** |
| Live endpoints | curl × 8 | `/api/stats`, `/api/tenders`, `/api/readiness`, `/api/tenders/{id}/competitors`, `/api/tenders/{id}/market-intelligence`, `/api/ops/summary`, `/api/scenarios`, `/api/calibration` → all 200 |
| Suppression integrity | SQL over 13,119 predictions | **0 suppressed rows carry any number** (p10/p50/p90/expected_value/win_probability) |
| Money envelope | live response inspection | every money value wrapped `{amount, currency:"SAR", vat_semantics:"unknown"}` |

### 2.1 Capability level actually reachable — the decisive measurement

The real engine (`orchestrator.tender_intelligence`) was run against **150 of the 789 genuinely open
tenders** (`last_offer_date > now()`), sampled evenly across the id range, against the live database:

| Level | Meaning | Count | Share |
|---|---|---|---|
| **L0** | facts only, no price | 68 | **45.3%** |
| **L1** | market P10/P50/P90 | 30 | 20.0% |
| **L2** | + named likely bidders | 5 | 3.3% |
| **L3** | + competitor price bands | 47 | 31.3% |
| **L4** | simulation grade (≥1 Tier-A band) | **0** | **0.0%** |

- Market range available on **82/150 (54.7%)** of open tenders.
- **244 competitor bands** produced across the sample. **Zero were Tier A.** All 244 are Tier B —
  deliberately widened by 1.4× and docked 10 confidence points, i.e. under-confident by construction
  and explicitly documented by their own author as "must not be read as a calibrated probability".
- Persisted corpus agrees: of 13,119 `price_predictions`, tiers are **B 3,355 / C 7,420 / D 2,344**.
  **No Tier A row exists anywhere in the database.**

**L4 is not thin — it is unreachable.** L3 is reachable but rests entirely on a tier the model itself
declines to stand behind.

---

## 3. Hard red gates

| # | Gate | Status | Evidence |
|---|---|---|---|
| 1 | No cross-tenant leakage of cost/margin/bid | **FAIL** | There is **no authentication of any kind** in `app.py` — no `Depends(auth)`, no bearer scheme, no API key. Tenant identity is whatever the caller writes in `X-Thaqip-Tenant`. `curl` with **no header at all** returned tenant 1's private scenarios including `estimated_cost 200000`. Row-level filtering is genuinely sound (26 endpoints audited, IDOR blocked, header injection fails closed) but it filters on attacker-supplied identity. Separately, `prediction_feedback` has **no `tenant_id` column**; one tenant's feedback is readable by another and moves a globally served statistic. |
| 2 | Legal basis recorded for every source | **PASS** | `source_registry` holds both real sources — `etimad_visitor_api` and `forsah_public_api` — each `access_class=PUBLIC_OPEN`, `redistribution_policy=derived_only`, `auth_method=none`, with refresh SLA and rate limit. No login-gated content is ingested. |
| 3 | Observed offers traceable to source evidence | **FAIL** | `source_lineage` contains **0 rows**. `/api/readiness` confirms `lineage_rows: 0, lineage_offers: 0, lineage_awards: 0`. `explain.py` cites lineage "when a row exists" — none ever does. Every "مُلاحظ" fact on screen is currently untraceable to a fetch. The table and code path exist; the data does not. |
| 4 | No temporal leakage / point-in-time correctness | **PASS** | Two genuine leaks were found by the test batteries and fixed: the competitor ratio queries did not exclude the subject tender (critical), and similarity consumed the subject's own `award_value` as a scoring input (high). The remaining `COALESCE(awards.awarded_at, awards.created_at)` fallback is **conservatively late** — ingestion time is never earlier than the true award, so it can only withhold evidence, never leak it. Direction verified by inspection and by 40 dedicated tests. **Qualification:** `resolve_as_of` falls back to `datetime.now(UTC)` for the 3 tenders carrying neither `offers_opening_date` nor `last_offer_date`, making those outputs clock-dependent. |
| 5 | High-confidence bucket demonstrably better than low-confidence | **UNVERIFIED** | **Not testable today, and untested.** Bucketing the 46 scored feedback rows by confidence returns a **single row**: `low(<50), n=46`. There is no high-confidence bucket — no scored prediction has ever scored ≥50. Worse, see §4.1: the scored corpus is synthetic. There is no measurement of predictive skill at any confidence level. |
| 6 | Optimizer respects hard floors (margin, cost) | **PASS** | Genuinely verified: property sweep, brute-force argmax cross-check, and an independent `_verify` that re-checks the returned bid against every raw constraint and downgrades to infeasible if it fails. A real ULP bug (floor unreachable near 100% margin, ~4.5% of a random grid) was found and fixed. The residual `numeric(6,2)` rounding defect fails **safe** — it refuses a satisfiable request rather than violating a floor. |
| 7 | Rollback / kill switch for model output | **FAIL** | `THAQIP_KILL_SWITCH` exists in `circuit_breaker.py` but governs **ingestion routes only**. `grep` over `app.py` finds **no reference** to it or to `circuit_breaker`. There is no flag, env var, or setting that disables P2W output. Turning off a bad model requires a redeploy. |
| 8 | UI never represents a prediction as a competitor fact | **FAIL** | `GET /api/tenders/1221/competitor-prices` is live and auto-loads on every tender detail view (`index.html:1702`). On tender 1221 it returns **12 named vendors** with an actionable `price_to_beat_median`, of which **11 are computed from a single observation** (`samples: 1`). It carries no evidence tier, no suppression, no confidence, and no مُلاحظ/متوقع chip. The column header reads **"للتفوق عليه"** ("to outbid him") and the section copy asserts the capability as fact: *"نستخرج السعر المعتاد لكل منافس … ونحسب سعرًا قريبًا للتفوق على وسيطه"* (`index.html:1774`). **On the same tender, the gated `/competitors` endpoint returns `competitors: 0`** — it refuses to speak about any vendor. Two panels on one screen, opposite answers. |
| 9 | Monitoring detects stale or failed feeds | **PASS** | Verified live. `/api/ops/summary` returned `verdict: "down"`, `lanes_healthy: 4/7`, and correctly named the stale lane (`pricing.seed`, `age_minutes: 1750`, `stale: true`) with an acknowledgement flow and Arabic next-actions. `ingest_runs` shows real recency per connector. |

---

## 4. Scorecard

| Domain | Weight | Score | Measured basis |
|---|---:|---:|---|
| **Data** | 25% | **32** | 1,699 tenders / 1,147 offers / **307 awards** / 614 vendors; only 202 multi-bidder awarded tenders. `awards.awarded_at` is **NULL for all 307 rows**, so the award-time anchor does not exist — **599/1,699 (35.3%)** of tenders are structurally forced to Tier D regardless of how much comparable history exists. `source_lineage` 0 rows. `boq_items` 10, `documents` 1, `vendors_with_cr` 0 — no scope or spec data at all (Etimad documents are login-gated). 418/614 vendors have exactly one offer. `tender_similarity` never persisted (0 rows). Sources are legally clean and freshness SLAs are recorded, which is the one real strength. |
| **Model / statistical** | 25% | **30** | **Zero validated accuracy.** No Tier A prediction exists in 13,119 rows. All 244 measured competitor bands are Tier B, which the model's own author documents as deliberately over-dispersed and "not a calibrated probability". Competitor bands are anchored on the market P50 — itself a prediction — but the published interval reflects **only** ratio uncertainty, so bands are systematically too narrow by an unmeasured amount. Participation model self-reports ~**4× over-confident** (16.4% mean stated vs 4.3% observed) with a non-monotone reliability diagram and `calibrated=False`. Inflation is a hardcoded 2%/yr placeholder. Interval coverage never validated. **Offsetting:** the correctness machinery is excellent — suppression invariant enforced at both dataclass and DB CHECK (0 violations in 13,119 rows), Monte Carlo bit-identical across processes and hash seeds, monotonicity structural rather than tuned, thresholds named constants. Superb scaffolding around an unvalidated core. |
| **Product / UX** | 15% | **45** | The new P2W surface is well built: provenance badges (مُلاحظ/متوقع/إدخالك), suppression as a designed state rather than an empty panel, an explicit "الثقة ليست احتمالية فوز" note, RTL charts, and 6 real accessibility defects found and fixed (focus trap, aria-hidden focus, focus restoration, accessible name, aria-live, Escape handling). Undercut by three things: the contradictory legacy competitor panel (gate 8); `vat_semantics: "unknown"` on **every** money value in a tool used to set bid prices, a latent 15% error; and `allowed_level` never reaching the UI at all (see §4.2), so the capability ladder the interface was designed against is absent from every live payload. |
| **Security / privacy / legal** | 20% | **22** | **No authentication.** Everything else in this domain is good work resting on nothing: 26 endpoints correctly tenant-filtered, IDOR blocked, header spoofing fails closed, parameterized queries, a hardcoded Typesense key removed, a cross-tenant count leak in `/api/readiness` fixed, and legal basis recorded for both sources with `derived_only` redistribution. But identity is a client-supplied string, `prediction_feedback` is unscoped, and `price_predictions` has no `tenant_id` at all. |
| **Operational** | 10% | **55** | 11/11 services Up with no restarts; clean logs across all workers. Forward-only migration runner with 15/15 recorded, applied inside per-file transactions. Stale-feed monitoring genuinely works. Against that: no kill switch for model output (gate 7); an unguarded **HTTP 500 on ordinary client input** — `POST /api/tenders/1221/scenarios` with `estimated_cost=1e17` returns `500 Internal Server Error` as a bare string, reproduced twice, matching the `nan` ValueError in the live logs; no migration checksums and no down-migrations, so an edit to an applied file drifts silently. |
| **Commercial** | 5% | **25** | No pilot users, no paying customers, no willingness-to-pay signal. All 451 `user_bid_scenarios` and all 71 `prediction_feedback` rows are test artifacts. The one genuine commercial asset is the measured market fact — across multi-bidder awarded tenders the lowest technically-compliant offer won ~96% of the time (avg winner price-rank 1.04) — which is a real, defensible insight independent of any model. |
| **Weighted total** | 100% | **33** | 32(.25) + 30(.25) + 45(.15) + 22(.20) + 55(.10) + 25(.05) |

### 4.1 The accuracy number is fabricated — new finding

`/api/readiness` publishes `mean_abs_pct_error: 0.4846` as a corpus accuracy statistic. It is not one.

The entire scored calibration corpus is **46 rows on a single tender (1671)**, and it consists of exactly
two distinct fabricated values repeated:

```
 actual_award_value | abs_pct_error | interval_hit | count
--------------------+---------------+--------------+-------
          550950.30 |        0.9692 | f            |    23
           16970.70 |        0.0000 | t            |    23
```

Tender 1671 has **0 offers, 0 awards**, and is **still open** (`last_offer_date` 2026-09-25). No scored
feedback row corresponds to any award record in the database. The "50% interval coverage" and the
"48.5% mean error" are both arithmetic on a test fixture, not measurements of anything.

**Consequences:** (a) there is no accuracy evidence for this system whatsoever — not weak evidence,
none; (b) a readiness endpoint is serving a fabricated number as measured, which violates the project's
own rule that no fabricated number appears in the UI; (c) gate 5 cannot be evaluated even in principle
until real resolved outcomes accumulate. `/api/ops/summary` similarly publishes
`pricing_accuracy.mape_90d: 38.25` from `measured: 2`.

### 4.2 The orchestrator never runs in production — confirmed

Every live P2W response reports `"orchestrator": "console_composition"` and `allowed_level: null`.

The cause is a one-word signature mismatch. `app.py:2888` calls
`_delegate(orchestrator, "tender_intelligence", _positional=(conn,), tender=tender)`, but the function's
parameter is `tender_id: int` (`orchestrator.py:280`). `_delegate` binds by signature, the bind raises
`TypeError`, and it silently returns `None` — so the console falls back to local composition on every
request, forever.

The engine's entire graceful-degradation contract — `allowed_level`, per-stage `_Degradation` records,
`MODEL_UNAVAILABLE` suppression — is correct code that production has never executed. The fallback path
has no `try/except` at all, which is why an injected sub-model failure and two ordinary client inputs
reach an unhandled 500 instead of degrading to L0-with-reasons. **This is the highest-value fix in the
report: one parameter name.**

---

## 5. What shipped

- **Foundation:** forward-only migration runner (`migrate.py` + `bin/migrate.sh`) with `schema_migrations`, dry-run and baseline modes; migration `0015_p2w_canonical.sql` adding 9 tables and `tenant_id` on 6 private tables; `p2w/contracts.py` with the suppression invariant enforced in both directions and mirrored as a DB CHECK.
- **Models (7):** evidence gate with a hard tier ladder and one auditable point-in-time predicate; deterministic rule-based similar-tender retrieval; market P10/P50/P90 via similarity- and recency-weighted quantiles; a participation model that was actually re-calibrated against 371 real events (Brier 0.234 → 0.063) and honestly marked `calibrated=False`; competitor bid-ratio quantiles in log space with shrinkage toward an activity prior; a Monte Carlo win-probability engine with exact-monotone curves via common random numbers and bit-identical determinism; a closed-form bid optimizer with independent constraint re-verification.
- **Integration:** `explain.py` (four-part observed/derived/predicted/drivers with a structural assertion that observed entries are real rows) and `orchestrator.py` (composition root with the L0–L4 ladder — written, tested, and not reachable in production).
- **Console:** 9 P2W endpoints behind one envelope helper guaranteeing model_version, generated_at, confidence, evidence_count, tier and suppression on every prediction; SAR + VAT semantics on every money value; a `get_tenant_id` dependency applied to all 26 pre-existing private endpoints; `/api/readiness`.
- **UI:** a new Arabic RTL "ذكاء التسعير" view with three structurally separated layers, provenance badges, suppression as a designed state, and an explanation drill-down.
- **Testing:** 6 batteries adding ~398 tests (1036 total, all passing); 3 adversarial reviews that refuted 3 of 4 headline claims; 22 defects found, 13 fixed.
- **Deploy:** console build context widened to the repo root so the engine is importable in the image, fixing a 503 that every unit test passed through.

---

## 6. Outstanding defects

| Severity | Item | Owner / next step |
|---|---|---|
| **Critical** | No authentication anywhere. Tenant identity is a client-supplied header; with no header the API serves tenant 1's private cost/margin/bid data. | Backend. Ship real auth before any network exposure. Until then bind the console to localhost only. |
| **Critical** | The only accuracy statistic in the product (`mean_abs_pct_error 0.4846`) is computed from 46 synthetic rows on one award-less open tender, and is served by `/api/readiness`. | Data/backend. Delete the fixture rows; make the endpoint report `status: "no_validated_accuracy"` rather than a number, until real resolved outcomes exist. |
| **High** | `orchestrator.tender_intelligence` is never called: `_delegate(..., tender=…)` vs parameter `tender_id=`. `allowed_level` and `degradations` never ship; the fallback path has no exception guard. | Backend. Rename the kwarg at `app.py:2888`, then re-verify `"orchestrator": "engine"` and that `allowed_level` appears in live payloads. |
| **High** | Legacy `/api/tenders/{id}/competitor-prices` publishes a per-vendor "price to beat" from a single observation, ungated and unlabelled, on the same screen as the gated panel. Same for `POST /api/pursuits/{id}/simulate-price` (`competitor_price_match` mode). | Product + backend. Route both through the evidence gate or remove the panel. This is the clearest house-rule violation shipping today. |
| **High** | `evidence.py:181` falls back to `awards.created_at`, forcing 599/1,699 (35.3%) of tenders — including 264 of 307 awarded ones — to Tier D permanently. The entire historical backtest set can never produce a prediction. | Data. Source real `awarded_at` values; until then no backtest is possible. |
| **High** | `source_lineage` is empty, so no "observed" fact on screen is traceable to a fetch. | Ingestion. Write lineage rows at ingest time. |
| **High** | Unguarded HTTP 500 on ordinary input (`estimated_cost` ≥ 1e17, NaN) returning a bare `Internal Server Error`. | Backend. Guard the console composition path and validate finite bounds on `ScenarioIn`. |
| **Medium** | No kill switch for model output; `THAQIP_KILL_SWITCH` covers ingestion only. | Ops. Add a P2W-output flag checked at the envelope layer. |
| **Medium** | `prediction_feedback` has no `tenant_id`; `price_predictions` has none either. One tenant's feedback is readable by another and moves a globally served statistic. | Backend + migration. |
| **Medium** | Every money value carries `vat_semantics: "unknown"` in a tool used to set bid prices. | Data/product. Determine and record VAT semantics per source. |
| **Medium** | `ScenarioIn` accepts `min_margin_pct = 99.996`; `numeric(6,2)` rounds it to 100.00, which the same validator forbids and the optimizer reports unreachable. | Backend. Round-trip the value or widen the column. |
| **Medium** | `feature_snapshot_id` hashes only (contributing tender ids, as_of date, model_version) — never the feature **values**. Award values were moved ±50% and p50 shifted 32,760 → 49,141 while the snapshot id stayed byte-identical. For market/competitor rows `seed` is NULL, so this id is the entire reproducibility handle. | Models. Include the feature values in the hash. |
| **Medium** | `resolve_as_of` falls back to `datetime.now(UTC)` (duplicated in 3 modules), making 3 tenders' outputs clock-dependent and flipping their snapshot id at UTC midnight. | Models. Suppress instead of falling back to the clock; de-duplicate. |
| **Low** | `SimulationOutput.assumptions` has two shapes; the zero-competitor branch omits two keys a consumer would index. | Models. |
| **Low** | `normalized_tokens` does not case-fold Latin script, understating similarity on mixed-script names. | Models. |
| **Low** | `contracts.PricePrediction`'s suppression invariant does not cover `expected_value`; `_prediction_from_payload` passes it through on rehydration. Currently unreachable. | Models. Close the guard. |
| **Low** | No migration checksums, no down-migrations; editing an applied file drifts silently. | Ops. |
| **Housekeeping** | Test residue in the live DB: 451 `user_bid_scenarios`, 71 `prediction_feedback`, ~13k `price_predictions`, 2 rows named `probe`, and a `redteam` tenant with scenario 818 / pursuit 84. | Whoever owns the environment. |

---

## 7. Top limitations

1. **There is no evidence any prediction is accurate.** Not thin evidence — none. The only accuracy figure in the product is a fabricated fixture, and no Tier A prediction has ever been produced.
2. **The award-time anchor does not exist.** `awards.awarded_at` is NULL for all 307 rows. This one gap disables 35% of the corpus, flattens every recency weight, renders the staleness gates inert, and makes a historical backtest impossible.
3. **L4 is unreachable and L3 is not trustworthy.** 0 of 244 competitor bands reached Tier A. Every band is Tier B, deliberately over-dispersed, anchored on a predicted market P50 whose error is excluded from the published interval.
4. **The system is unauthenticated.** Correct row-level tenancy filtering on an identity anyone can assert is not isolation.
5. **The production path is not the tested path.** The orchestrator, its capability ladder, and its degradation contract are dead code in the deployed service.
6. **The product contradicts itself on screen.** One panel names 12 competitors and tells you what to bid to beat each; the panel beside it refuses to name any.
7. **No scope data.** 10 BOQ items and 1 document across 1,699 tenders. Similarity is a structural proxy — two tenders scoring 0.8 can be entirely different work — and the quantiles inherit that error.
8. **Nothing is calibrated.** Every threshold, weight, and coefficient is a reasoned choice against corpus shape, not a fit against outcomes. This is stated honestly throughout the code, and it remains true.

---

## 8. Recommended next actions

**Before any external exposure (blocking):**
1. Ship authentication; bind the console to localhost until then.
2. Delete the synthetic `prediction_feedback` rows and make `/api/readiness` report the absence of validated accuracy instead of a number.
3. Fix the `_delegate` kwarg (`tender=` → `tender_id=`), then verify `"orchestrator": "engine"` and `allowed_level` in live payloads.
4. Gate or remove `/competitor-prices` and the `competitor_price_match` simulate-price mode.
5. Guard the console composition path; return a typed degradation, never a bare 500.

**Before claiming anything above L1:**
6. Source real `awarded_at` values — this is the single highest-leverage data fix.
7. Add a P2W output kill switch checked at the envelope layer.
8. Add `tenant_id` to `prediction_feedback` and `price_predictions`.
9. Include feature values in `feature_snapshot_id`.
10. Populate `source_lineage` at ingest time.

**Posture for the next quarter:** run as an internal design-partner tool at **L1** — a market range
presented as *"the spread of comparable awarded contracts"*, never as a price prediction — plus the
observed-facts layer and the 96% lowest-price market insight, which is the most commercially valuable
and best-evidenced thing here. Collect real resolved outcomes. Do not sell price-to-win.

---

## 9. Evaluation questions to answer

### 30 days
1. How many predictions have been scored against a **real** award (not a fixture)? Target ≥ 50; today it is 0.
2. Has the source begun supplying real `awarded_at` values, and what fraction of awards now carry one?
3. Is the P10–P90 interval covering ~80% of realised awards? Report coverage with a confidence interval, or state that n is too small.
4. Are auth and the four blocking fixes shipped and verified in the running stack, not just in tests?
5. How many `source_lineage` rows exist per observed offer and award?

### 60 days
6. Split scored predictions by confidence decile — is error monotonically decreasing? This is red gate 5, and it is the gate that licenses L2+.
7. Has any Tier A prediction been produced? If not, is the Tier A threshold wrong or is the data genuinely insufficient?
8. Re-measure the L0–L4 distribution over open tenders. Has the L1 share moved above 55%?
9. Is the participation model still ~4× over-confident once real award dates spread the recency weights?
10. Do design partners act on the market range, and does a decision change when it is shown?

### 90 days
11. Does a competitor band's realised coverage match its stated width once the market-P50 baseline error is included? If not, widen the band to include it.
12. Is `DEFAULT_ANNUAL_INFLATION = 0.02` replaced by a real published Saudi cost index?
13. Has a temporal-holdout backtest been run — train before date T, predict after — and what is the honest MAPE?
14. Are the optimizer's `SAFE_WIN_FLOOR = 0.60` and shrinkage/widening constants defensible against realised outcomes, or still hand-set?
15. Given everything above, is L2 or L3 now licensed by evidence — and if the answer is still no, is the market-range-plus-facts product worth selling on its own?

---

*Prepared from measured evidence only. Where a claim could not be verified it is marked UNVERIFIED, not
PASS. Overstating readiness on a system that influences real bidding money is the worst available
outcome of this exercise.*
