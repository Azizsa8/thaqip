# ثاقب — ملاحظات استمرار التطوير والتشغيل

آخر تحديث: 2026-09-09

## الحالة المثبتة الآن

- لوحة التحكم المحلية تعمل عبر Docker Compose على `http://localhost:8091`.
- نسخة العرض العامة منشورة على Netlify: `https://thaqip-demo.netlify.app`.
- نسخة العرض محمية ببوابة كلمة مرور بسيطة من جهة المتصفح، وهي مناسبة للمشاركة التجريبية وليست حدًا أمنيًا حقيقيًا.
- `poller` المحلي يعمل ويحدّث منافسات اعتماد بشكل مستمر.
- `awards_harvest` أصبح يتعامل مع `waf cool-off` كتهدئة مصدر مجدولة بدل انهيار traceback.
- `/api/lanes` يميز الآن بين `healthy`, `running`, `stalled`, `failed`, `stale`, و`cooldown`، ويعرض عدادات الصفحات/العناصر من آخر run. تمت إضافة `ops_health` لإغلاق سجلات `ingest_runs` اليتيمة القديمة كـ failed/stalled بدل تركها مفتوحة للأبد، ويسجل نبضه في `ingest_runs` باسم `ops.health`.
- تمت إضافة `/api/ops/summary` كحكم تشغيلي واحد: `operational`, `degraded`, أو `down` مع إجراءات مقترحة وملخص دقة التسعير. أخطاء `waf cool-off` تصنف كـ `cooldown` حتى لو كانت runs قديمة مسجلة `ok=false`، لأنها تهدئة مصدر وليست عطل operator.
- الـ static demo يصدّر ويخدم `/api/lanes` حتى يرى المستخدم صحة خطوط الاستيعاب في النسخة العامة.
- حلقة دقة التسعير مفعلة: كل محاكاة سعر تُقاس لاحقًا مقابل قيمة الترسية عند توفرها.
- تمت إضافة `POST /api/pricing/seed-baselines` لبذر فرضية تسعير واحدة لكل pursuit نشط لا يملك محاكاة. آخر تشغيل محلي زرع 5 فرضيات ورفع القياسات إلى 2 مع 43 محاكاة محفوظة/مقاسة.
- تمت إضافة `thaqip_ingestion.pricing_seed` وتشغيله في Modal كل 6 ساعات، وكذلك خدمة Docker Compose باسم `pricing-seed` وسكربت `bin/pricing-seed.sh` للتشغيل المحلي. يسجل الآن connector باسم `pricing.seed` في `ingest_runs` ويظهر في `/api/lanes`.
- آخر محاولتي نشر Netlify في 2026-09-09 رجعت `JSONHTTPError: Forbidden` رغم وجود login؛ يلزم إصلاح صلاحية/ربط Netlify ثم إعادة أمر النشر.

## آخر سلسلة تحقق موصى بها

```bash
cd /home/ais04/thaqip/services/ingestion
uv run ruff check src tests ../console/src/thaqip_console/app.py ../../deploy/modal/modal_app.py ../../deploy/modal/check_readiness.py ../../tools/export_demo.py
uv run pytest -q
```

المتوقع حاليًا: `36 passed`.

## أوامر نشر نسخة العرض

```bash
cd /home/ais04/thaqip
uv run --project services/ingestion python tools/export_demo.py
netlify deploy --site f0f4cc70-bb3f-42ef-83ad-f258fc3b0f92 --dir deploy/netlify --prod
unlink .netlify/netlify.toml 2>/dev/null || true
find .netlify -depth -type d -empty -delete 2>/dev/null || true
```

بعد النشر تحقق من:

```bash
curl -fsS https://thaqip-demo.netlify.app/data/db.json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("lanes", [])), d.get("pricing_accuracy", {}).get("status"), d.get("pricing_accuracy", {}).get("measured"))'
```

## Modal 24/7

لم يتم النشر الحقيقي على Modal بعد. الموجود الآن scaffold وفاحص جاهزية.

قبل النشر:

```bash
cd /home/ais04/thaqip
python3 deploy/modal/check_readiness.py
```

المتوقع الآن أن تمر المتطلبات البنيوية، مع تحذير إذا كان Modal CLI أو `DATABASE_URL` غير موجودين محليًا.

المطلوب في حساب Modal:

```bash
modal secret create thaqip-runtime \
  DATABASE_URL='postgres://...' \
  REDIS_URL='redis://...' \
  TYPESENSE_URL='https://...' \
  TYPESENSE_KEY='...' \
  TELEGRAM_BOT_TOKEN='...'
```

ثم:

```bash
modal deploy deploy/modal/modal_app.py
```

## الأولويات التالية

1. نشر Modal فعليًا بعد توفير secret `thaqip-runtime`، ثم مراقبة `/api/lanes` و`/api/pricing/accuracy`.
2. جعل `awards_harvest` يسجل `cooldown` في checkpoint/metadata أيضًا إذا احتجنا تقارير تفصيلية لاحقًا.
3. محليًا، تأكد أن خدمتي `pricing-seed` و`ops-health` تعملان عبر Docker Compose وأن `/api/lanes` تعرض `pricing.seed` و`ops.health` بحالة healthy. إذا ظهر أي lane بحالة `stalled` فافحص العملية المقابلة، ثم شغّل `python -m thaqip_ingestion.ops_health --close-stalled` إذا كانت العملية اختفت وبقي السجل مفتوحًا.
4. أصلح صلاحية Netlify وأعد نشر export الأخير حتى تظهر `seed-baselines` وpricing ladder في الرابط العام.
5. رفع corpus الترسيات والعروض تدريجيًا بدون ضغط على Etimad، مع إعطاء الأولوية للأنشطة التجارية ذات الطلب الأعلى.

## ملفات لا تُثبت عادة

اترك الملفات التالية خارج commits إلا إذا كان هناك سبب صريح:

- `var/*.log`
- `var/*.lock`
- `.netlify/`
- `agent-fleet/`
