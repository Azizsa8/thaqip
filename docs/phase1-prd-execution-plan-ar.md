# ثاقب — PRD وخطة تنفيذ Phase 1

تاريخ النسخة: 2026-09-08  
الحالة المرجعية: الكود الحالي بعد Phase 0، مع تشغيل Docker Compose محلياً ونشر نسخة Netlify عامة محمية بكلمة مرور.

## 1. الهدف

ثاقب ليس لوحة عرض للمنافسات. ثاقب يجب أن يصبح نظام تشغيل الفوز بالمناقصات الحكومية السعودية: يرصد، يجمع، يطابق، يحلل المنافسين، يقدر الأسعار، يحفظ الذاكرة التاريخية، ويحوّل كل منافسة إلى قرار عملي: ندخل أو لا ندخل، بأي سعر تقريبي، وما الذي يجب إنجازه قبل الموعد.

المطلب المركزي من Phase 1 هو أن يشعر المستخدم كلما فتح لوحة القيادة أن المنصة تعمل بلا توقف: عدد المنافسات أعلى، عدد الشركات المدروسة أعلى، عدد العروض والترسيات أعلى، ودقة تقدير الأسعار تتحسن مع كل دورة. النمو اليومي في البيانات ليس زينة؛ هو جوهر الثقة والبيع.

## 2. الحالة الحالية

المنصة تملك الآن أساساً قوياً:

- راصد اعتماد `B1` يعمل ضد API الزوار مع retries وrate limits.
- تفاصيل المنافسة `B2` تعمل عبر جلسة TSPD وparsers للـ view components.
- حصاد الترسيات والعروض `B4` موجود ويغذي vendors/offers/awards.
- حلقة delta `D2` تعمل كخدمة Docker كل 5 دقائق.
- relay إلى Redis Streams `D1` يعمل بنمط at-least-once.
- محرك التنبيهات `M2` موجود ويدعم log/telegram/email pending.
- مصالحة ليلية `C5`، freshness harness `D3`، audit harness `D4`.
- pipeline وثائق `C6` وBOQ parser `C7`.
- بحث Typesense `E2` over tenders + doc text.
- War Room v0: pursuits، compliance matrix، outcomes، pricing simulator.
- Forsah spike/connector موجود مع bid-count deltas وتنبيهات competition rising.
- نسخة عامة ثابتة على Netlify محمية بكلمة مرور.

الخدمات المحلية الحالية: `postgres`, `redis`, `minio`, `typesense`, `poller`, `relay`, `alerts`, `indexer`, `console`.

## 3. مبادئ المنتج

1. الحقيقة أولاً: لا نعرض رقماً كسعر منافس أو احتمال فوز إلا ومعه basis، sample size، confidence، وآخر تحديث.
2. كل شيء يتعلم: كل ترسية جديدة تقيس توقعاتنا السابقة وتعدل النموذج.
3. القرار قبل العرض: المستخدم يدفع لأنه يعرف ماذا يفعل الآن، لا لأنه رأى بيانات أكثر فقط.
4. التشغيل المستمر ميزة: المنصة يجب أن تزيد corpus كل يوم بدون تدخل يدوي.
5. لا وعود كاذبة: إذا كانت الدقة منخفضة نقول ذلك ونقترح كيف نرفعها.
6. الالتزام القانوني خط أحمر: نبني ميزة تقدير أسعار المنافسين من مصادر عامة، بيانات تاريخية، وثائق رسمية، وحسابات احتمالية. لا نستخدم وصولاً غير مصرح به، لا نتجاوز مصادقة، لا نستغل ثغرات للوصول إلى عطاءات سرية قبل فتحها.

## 4. ما يشتريه العميل

العميل لا يشتري “مناقصات”. العميل يشتري هذه النتائج:

- يعرف مبكراً ما يناسبه قبل أن يضيع فريقه الوقت.
- يرى من ينافسه غالباً في النشاط والجهة والمنطقة.
- يعرف نطاق السعر المتوقع للفوز بناءً على تاريخ الترسيات والعروض.
- يتلقى تنبيه عندما تصبح المنافسة أكثر شراسة.
- يرى checklist نظامية وفنية قابلة للتنفيذ.
- يملك سجل قرار: لماذا دخلنا؟ لماذا خسرنا؟ ماذا تعلمنا؟
- يقيس دقة ثاقب بمرور الوقت: توقع مقابل ترسية فعلية.

## 5. الميزة القاتلة: تقدير سعر المنافسين

### 5.1 المشكلة

أهم سؤال تجاري للمستخدم: كم تقريباً سيضع المنافسون أسعارهم؟ كلما اقترب ثاقب من الإجابة بنسبة دقة 80-90% على نطاق السعر، زادت قيمة الاشتراك.

### 5.2 الحل القانوني

نبني `Competitive Price Intelligence` من:

- تاريخ ترسيات النشاط والجهة والمنطقة.
- عروض vendors المسجلة في `offers`.
- سجل فوز/خسارة المنافس حسب الجهة والنشاط.
- عدد المنافسين/العطاءات عند توفره.
- ميزانية المنافسة وقيمة الكراسة ومدتها.
- BOQ items عندما تتوفر.
- تشابه المنافسة مع منافسات سابقة.
- نتائجنا السابقة: prediction vs actual.

### 5.3 مخرجات الواجهة

لكل pursuit:

- `expected_award_range`: نطاق الترسية المتوقع.
- `competitor_bid_band`: نطاق أسعار المنافسين المحتمل.
- `recommended_bid`: سعر مقترح مع margin.
- `win_probability`: احتمال الفوز.
- `confidence`: low/medium/high.
- `basis`: activity_history, agency_history, vendor_behavior, boq_similarity, sparse_data.
- `accuracy_track`: كيف كان أداء النموذج في آخر 30/90 يوم.

### 5.4 ما لا نبنيه

لا نبني أو نوثق طريقة للحصول على عطاءات سرية قبل فتحها، ولا نتجاوز صلاحيات منصة اعتماد أو فرصة، ولا نستخدم ثغرات للوصول إلى بيانات غير عامة. أي اختبار أمني يجب أن يكون مصرحاً، محدود النطاق، ومسجلاً كمسار responsible disclosure. المطلوب تجارياً يتحقق بطرق قانونية أقوى على المدى الطويل لأنها قابلة للبيع للمؤسسات والحكومة.

## 6. حزمة ميزات Phase 1

### M1. Data Growth Board

لوحة أعلى dashboard تعرض:

- عدد المنافسات الكلي.
- عدد المنافسات المفتوحة.
- عدد vendors المدروسة.
- عدد offers.
- عدد awards.
- عدد documents/chunks.
- النمو آخر 24 ساعة.
- freshness p50/p95.
- health لكل lane.

Acceptance:

- كل رقم يأتي من DB live.
- يظهر delta مقارنة بالأمس.
- أي lane متأخرة يظهر عليها warning.

### M2. Alert Engine v2

أنواع تنبيه جديدة:

- منافسة مناسبة جداً للملف.
- ارتفاع عدد العروض أو المهتمين.
- تمديد موعد.
- ترسية لمنافس معروف.
- فرصة تسعير قريبة من نمط الشركة.
- daily executive digest.

Acceptance:

- لا duplicate notifications.
- كل تنبيه له reason واضح.
- دعم Telegram الآن، email يبقى pending حتى SMTP worker.

### M3. War Room v1

تطوير غرفة العمليات:

- Bid / No-bid score.
- readiness score.
- risk flags.
- next actions.
- owner/due date لكل compliance item.
- decision log.
- pricing simulator history.

Acceptance:

- API يرجع `decision_score`, `readiness_score`, `risk_flags`, `next_actions`.
- UI تعرض القرار في أعلى pursuit.
- يمكن تصدير compliance + decision summary.

### M4. Competitor Intelligence

ملف competitor/vendor:

- win rate.
- median bid.
- median discount from estimated budget.
- favored agencies.
- favored activities.
- price aggressiveness.
- last seen.
- head-to-head overlap مع شركتنا.

Acceptance:

- صفحة vendor تجيب history/agencies/offers.
- export CSV موجود.
- تظهر confidence لكل insight.

### M5. Prediction Accuracy Loop

كل توقع سعر يتم حفظه ثم قياسه عند إعلان الترسية.

Acceptance:

- جدول `pricing_predictions` أو توسيع `pursuit_simulations`.
- عند outcome/award يتم حساب absolute error وpercentage error.
- dashboard يعرض MAPE آخر 30/90 يوم.

### M6. Search Quality

تحسين البحث:

- Arabic normalization.
- snippets من داخل الوثائق.
- ranking حسب المصدر، freshness، match داخل document.
- filters للجهة، النشاط، مفتوحة فقط، مرساة فقط.

Acceptance:

- عبارة موجودة فقط داخل doc_chunks ترجع المنافسة.
- search unavailable يعطي رسالة واضحة ولا يكسر صفحة الاستكشاف.

### M7. Document Intelligence

استخراج:

- deadlines.
- guarantees.
- qualification requirements.
- delivery terms.
- payment terms.
- penalties.
- BOQ summary.

Acceptance:

- extraction origin واضح: rule/doc/llm/manual.
- confidence وsource_ref موجودان.
- low confidence يذهب إلى review queue.

### M8. Agency Intelligence

ملف الجهة:

- عدد المنافسات.
- متوسط عدد العروض.
- متوسط قيمة الترسية.
- سرعة الترسية.
- vendors الأكثر فوزاً.
- الأنشطة الأعلى.

Acceptance:

- agency detail page/API يرجع leaderboards.
- usable في pricing model.

### M9. Public Demo v2

تجهيز demo قابل للبيع:

- password gate أنظف.
- بيانات مختارة بدلاً من snapshot عشوائي.
- guided scenario: من discovery إلى War Room إلى pricing.
- demo badges توضّح أنها نسخة ثابتة.

Acceptance:

- deploy Netlify يعمل بأمر واحد.
- لا أسرار أو بيانات حساسة في snapshot.

### M10. Modal 24/7 Runtime

نقل long-running/data jobs إلى Modal:

- scheduled delta poll.
- awards harvest lane.
- Forsah lane.
- reconcile lane.
- digest lane.
- backfill lane.
- watchdog + health table.

Acceptance:

- كل job idempotent.
- restart لا يكرر data ولا يفقد events.
- dashboard health يعتمد على `ingest_runs`.
- secrets من Modal فقط، لا hardcoded.

## 7. Modal Runtime Design

### 7.1 Jobs

- `modal_delta_poller`: كل 5 دقائق، pages=6.
- `modal_awards_harvest`: كل ساعة أو حسب rate limits.
- `modal_forsah_pull`: كل ساعة.
- `modal_reconcile`: مرة يومياً.
- `modal_digest`: 07:00 Asia/Riyadh.
- `modal_backfill`: manually triggered أو scheduled low priority.
- `modal_indexer_bulk`: manually triggered بعد schema/search changes.

### 7.2 Secrets

مطلوب في Modal:

- `DATABASE_URL`
- `REDIS_URL`
- `TYPESENSE_URL`
- `TYPESENSE_KEY`
- `MINIO_ENDPOINT`
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `TELEGRAM_BOT_TOKEN`
- optional `THAQIP_ANTHROPIC_API_KEY`

### 7.3 Reliability

- كل lane يسجل `ingest_runs`.
- كل run له status، started_at، finished_at، counters، error.
- circuit breakers تمنع تخريب البيانات عند challenge/429.
- kill switch `THAQIP_KILL_SWITCH` يوقف crawlers بدون إطفاء dashboard.
- health endpoint في console يقرأ آخر runs ويصنف stale/degraded/healthy.

## 8. أوامر التشغيل المحلية

تشغيل الخدمات:

```bash
cd /home/ais04/thaqip
docker compose up -d
docker compose ps
```

اختبارات ingestion:

```bash
cd /home/ais04/thaqip/services/ingestion
uv run ruff check src tests
uv run pytest -q
```

إعادة بناء الخدمات بعد تعديل ingestion/console:

```bash
cd /home/ais04/thaqip
docker compose up -d --build poller relay alerts indexer console
```

تشغيل bulk index:

```bash
cd /home/ais04/thaqip/services/ingestion
DATABASE_URL=postgres://thaqip:thaqip_dev@localhost:5433/thaqip \
REDIS_URL=redis://localhost:6380/0 \
TYPESENSE_URL=http://localhost:8108 \
TYPESENSE_KEY=thaqip_dev_search \
uv run --extra db python -m thaqip_ingestion.indexer --bulk
```

تصدير ونشر demo:

```bash
cd /home/ais04/thaqip
uv run --project services/ingestion python tools/export_demo.py
netlify deploy --site f0f4cc70-bb3f-42ef-83ad-f258fc3b0f92 --dir deploy/netlify --prod
```

## 9. أوامر تنفيذ للـ Agents

### Agent A — War Room v1

Prompt:

```text
Work in /home/ais04/thaqip. Implement Phase 1 M3 War Room v1 from docs/phase1-prd-execution-plan-ar.md. Preserve existing data and runtime logs. Add API fields for decision_score, readiness_score, risk_flags, next_actions using existing pursuits, tenders, compliance_items, awards, offers, and pursuit_simulations. Add a focused UI summary panel in services/console/src/thaqip_console/static/index.html. Add tests if shared scoring logic is extracted. Run ruff/pytest for ingestion if touched, smoke-test console API, and report exact files changed.
```

### Agent B — Prediction Accuracy Loop

Prompt:

```text
Work in /home/ais04/thaqip. Implement Phase 1 M5 Prediction Accuracy Loop. Use current pursuit_simulations and outcomes/awards to compute prediction error once actual award values exist. Add migration if needed. Add API endpoint returning 30/90 day accuracy, MAPE, sample count, and recent prediction-vs-actual rows. Surface it in the dashboard or War Room. Do not fabricate accuracy where samples are missing; show low sample state.
```

### Agent C — Modal Runtime

Prompt:

```text
Work in /home/ais04/thaqip. Add a Modal deployment scaffold for the 24/7 runtime described in docs/phase1-prd-execution-plan-ar.md. Create a minimal modal_app.py or deployment module that schedules delta poller, awards harvest, Forsah pull, reconcile, and digest using existing thaqip_ingestion modules. Do not hardcode secrets; read from Modal secrets/env. Keep jobs idempotent and rate-limited. Document setup commands and required secrets.
```

### Agent D — Public Demo v2

Prompt:

```text
Work in /home/ais04/thaqip. Improve the Netlify static demo flow. Keep the password gate, but make the demo open into a strong guided scenario showing discovery, War Room, pricing simulator, competitor dossier, and alerts. Update tools/export_demo.py if snapshot selection needs to be deterministic. Deploy only to site f0f4cc70-bb3f-42ef-83ad-f258fc3b0f92.
```

### Agent E — Search Quality

Prompt:

```text
Work in /home/ais04/thaqip. Improve E2 search quality. Verify Typesense indexing over doc_chunks, add Arabic normalization where it fits the existing code, improve snippets and fallback behavior when Typesense is unavailable. Add a test or smoke script proving a phrase from doc text can retrieve its tender. Keep UI compact and operational.
```

## 10. Engineering Tickets

### P1-01 War Room scoring backend

Build pure scoring helper:

- Inputs: tender, compliance rows, award stats, pricing simulations.
- Outputs: decision_score, readiness_score, risk_flags, next_actions.
- Test with deterministic fixtures.

### P1-02 War Room scoring API

Extend `GET /api/pursuits/{pid}`.

- Include scores.
- Include explanation fields.
- Keep old response compatible.

### P1-03 War Room UI command panel

Add compact panel above compliance matrix:

- decision badge.
- readiness bar.
- risk chips.
- next action list.
- last pricing simulation summary.

### P1-04 Prediction accuracy schema

Either extend `pursuit_simulations` or add `pricing_prediction_results`.

- simulation_id.
- actual_award_value.
- absolute_error.
- percentage_error.
- measured_at.

### P1-05 Accuracy API and dashboard

Return:

- sample_count_30d.
- mape_30d.
- mape_90d.
- best/worst recent predictions.

### P1-06 Modal scaffold

Add `deploy/modal/` or `services/ingestion/modal_app.py`.

- Schedules.
- Secret names.
- README.
- Local dry-run notes.

### P1-07 Health hardening

Make `/api/health` classify each lane:

- healthy.
- stale.
- failed.
- unknown.

### P1-08 Demo v2

Make public demo tell one sales story:

- password gate.
- guided first pursuit.
- data growth cards.
- pricing simulator callout.

## 11. Metrics

North-star:

- Number of actionable pursued opportunities per customer per week.

Data moat:

- Total tenders.
- Total offers.
- Total awards.
- Total vendors.
- New vendors/day.
- New offers/day.
- Capture rate.
- Freshness p50/p95.

Prediction:

- MAPE overall.
- MAPE by activity.
- MAPE by agency.
- % predictions within 10%, 20%, 30%.
- Sample count by confidence band.

Product:

- pursuits created.
- bid/no-bid decisions logged.
- alerts opened.
- pricing simulations run.
- compliance exports downloaded.

## 12. Completion Definition for Phase 1

Phase 1 is complete when:

- Platform runs continuously without manual babysitting for 7 days.
- Dashboard shows growing corpus and lane health.
- War Room gives bid/no-bid score and next actions.
- Pricing estimates are stored and later measured against actual awards.
- At least one public demo scenario is polished and shareable.
- Legal guardrails are documented in product and engineering docs.
- A new model/agent can pick up any ticket from this document and run it without needing the original conversation.

