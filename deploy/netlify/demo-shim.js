/* Thaqip static demo shim: password gate + client-side API over data/db.json.
   Simple share-gate only (client-side hash check) — not a security boundary;
   the snapshot contains public procurement data and demo state. */
(function () {
  'use strict';

  /* ---------------- password gate ---------------- */
  // SHA-256 of the share password
  const GATE_HASH = '80fd8896ffa0a27092200a1be847987baa813beb2ca7f0088c78ad5e0467047b';
  const KEY = 'thaqip_demo_gate_v1';

  async function sha256(s) {
    const b = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
  }

  function buildGate() {
    const el = document.createElement('div');
    el.id = 'demoGate';
    el.dir = 'rtl';
    el.style.cssText = 'position:fixed;inset:0;z-index:9999;background:#122019;display:grid;place-items:center;font-family:"IBM Plex Sans Arabic",sans-serif';
    el.innerHTML = `
      <div style="width:min(360px,90vw);background:#1b2b23;border:1px solid #2c4237;border-radius:16px;padding:32px 28px;text-align:center;color:#e8efe9">
        <div style="width:52px;height:52px;border-radius:14px;background:#0E6B4F;display:grid;place-items:center;margin:0 auto 16px">
          <svg viewBox="0 0 24 24" width="26" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"><path d="M12 3l7 4v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V7l7-4z"/><path d="M9 12l2 2 4-4"/></svg>
        </div>
        <div style="font-size:20px;font-weight:700;margin-bottom:4px">ثاقب</div>
        <div style="font-size:12px;color:#9db5a8;margin-bottom:20px">نسخة عرض خاصة — أدخل كلمة المرور</div>
        <input id="gatePw" type="password" placeholder="كلمة المرور" autocomplete="off"
          style="width:100%;box-sizing:border-box;background:#122019;border:1px solid #2c4237;border-radius:10px;padding:10px 14px;color:#e8efe9;font-size:14px;font-family:inherit;text-align:center">
        <button id="gateGo"
          style="width:100%;margin-top:12px;background:#0E6B4F;border:none;border-radius:10px;padding:11px;color:#fff;font-size:14px;font-weight:600;font-family:inherit;cursor:pointer">دخول</button>
        <div id="gateErr" style="height:18px;font-size:12px;color:#e08a77;margin-top:10px"></div>
      </div>`;
    document.body.appendChild(el);
    const tryGo = async () => {
      if (await sha256(document.getElementById('gatePw').value) === GATE_HASH) {
        sessionStorage.setItem(KEY, GATE_HASH);
        el.remove();
      } else {
        document.getElementById('gateErr').textContent = 'كلمة المرور غير صحيحة';
      }
    };
    document.getElementById('gateGo').onclick = tryGo;
    document.getElementById('gatePw').addEventListener('keydown', e => { if (e.key === 'Enter') tryGo(); });
    setTimeout(() => document.getElementById('gatePw').focus(), 50);
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (sessionStorage.getItem(KEY) !== GATE_HASH) buildGate();
    const banner = document.createElement('div');
    banner.dir = 'rtl';
    banner.style.cssText = 'position:fixed;bottom:14px;left:14px;z-index:50;background:#A16207;color:#fff;font-size:11.5px;border-radius:99px;padding:5px 14px;font-family:"IBM Plex Sans Arabic",sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.2)';
    banner.textContent = 'نسخة عرض ثابتة — البيانات لحظة التصدير، والتعديلات محلية فقط';
    document.body.appendChild(banner);
  });

  /* ---------------- static API ---------------- */
  let dbPromise = null;
  const loadDb = () => (dbPromise ??= fetch('data/db.json').then(r => r.json()));
  const json = (obj, status = 200) =>
    new Response(JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json' } });

  function csvEscape(v) {
    const s = String(v ?? '');
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function csvResponse(rows, filename) {
    const csv = '\ufeff' + rows.map(r => r.map(csvEscape).join(',')).join('\n');
    return new Response(csv, { headers: { 'Content-Type': 'text/csv;charset=utf-8', 'Content-Disposition': `attachment; filename="${filename}"` } });
  }

  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : input.url;
    if (!url.startsWith('/api/') && !url.includes('/api/')) return origFetch(input, init);
    const db = await loadDb();
    const u = new URL(url, location.origin);
    const p = u.pathname;
    const method = (init && init.method) || 'GET';
    const body = init && init.body ? JSON.parse(init.body) : null;

    if (p === '/api/dashboard') return json(db.dashboard);
    if (p === '/api/freshness/trend') return json(db.freshness_trend || { target_seconds: 900, days: 14, latest: null, items: [] });
    if (p === '/api/filters') return json(db.filters);
    if (p === '/api/lanes') return json(db.lanes || []);
    if (p === '/api/ops/summary') return json(db.ops_summary || { verdict: 'operational', lanes_total: (db.lanes || []).length, lanes_healthy: (db.lanes || []).length, lanes_running: 0, lanes_need_attention: 0, attention: [], pricing_accuracy: db.pricing_accuracy || {}, next_actions: [] });
    if (p === '/api/ops/incidents/acknowledge' && method === 'POST') {
      const connector = body && body.connector;
      if (Array.isArray(db.lanes)) {
        const acknowledgedAt = new Date().toISOString();
        db.lanes = db.lanes.map((lane) => lane.connector === connector ? { ...lane, status: 'acknowledged', needs_attention: false, acknowledged_at: acknowledgedAt, acknowledged_note: body && body.note } : lane);
      }
      if (db.ops_summary) { db.ops_summary.verdict = 'operational'; db.ops_summary.lanes_need_attention = 0; db.ops_summary.attention = []; db.ops_summary.next_actions = []; }
      return json({ acknowledged: true, connector, run_id: null });
    }
    if (p === '/api/settings' && method === 'PATCH') {
      db.settings = { ...(db.settings || {}), calculator: { default_markup_pct: Number(body.default_markup_pct), risk_tolerance: body.risk_tolerance, default_agency_id: body.default_agency_id || null }, alerts: { frequency: body.alert_frequency }, my_company: { name: (body.my_company_name || 'شركتي').trim(), target_win_rate_pct: Number(body.target_win_rate_pct ?? 25), cost_advantage_pct: Number(body.cost_advantage_pct ?? 0) }, retention_days: Number(body.retention_days), updated_at: new Date().toISOString(), gated_features: (db.settings || {}).gated_features || { etimad_supplier_credentials: false, llm_compliance_extraction: false, telegram_alerts: false, ksa_staging: false } };
      return json(db.settings);
    }
    if (p === '/api/settings') return json(db.settings || { gated_features: { etimad_supplier_credentials: false, llm_compliance_extraction: false, telegram_alerts: false, ksa_staging: false }, calculator: { default_markup_pct: 12, risk_tolerance: 'balanced', default_agency_id: null }, alerts: { frequency: 'instant' }, my_company: { name: 'شركتي', target_win_rate_pct: 25, cost_advantage_pct: 0 }, retention_days: 180 });
    if (p === '/api/pricing/accuracy') return json(db.pricing_accuracy || {
      measured: 0, status: 'awaiting_awards', confidence: 'low', recent: [],
      mape_all: null, mape_30d: null, mape_90d: null,
      sample_30d: 0, sample_90d: 0, within_10_pct: null, within_20_pct: null, within_30_pct: null
    });
    if (p === '/api/pricing/seed-baselines' && method === 'POST') {
      const missing = db.pursuits.filter(x => !((db.pursuit_details[String(x.id)] || {}).market || {}).last_simulation).length;
      return json({ seeded: missing, measured_after_seed: 0, items: [] });
    }

    if (p === '/api/tenders') {
      let items = db.tenders.slice();
      const q = u.searchParams.get('q');
      if (q) items = items.filter(t => (t.name || '').includes(q) || t.reference_number === q);
      const ag = u.searchParams.get('agency_id');
      const seen = new Set(); items = items.filter(t => !seen.has(t.id) && seen.add(t.id));
      if (ag) { const names = new Map(db.filters.agencies.map(a => [String(a.id), a.canonical_name]));
        items = items.filter(t => t.agency === names.get(ag)); }
      const src = u.searchParams.get('source');
      if (src) items = items.filter(t => t.source === src);
      if (u.searchParams.get('awarded') === 'true') items = items.filter(t => t.has_award);
      if (u.searchParams.get('open_only') === 'true') items = items.filter(t => t.remaining_s > 0);
      const off = +(u.searchParams.get('offset') || 0), lim = +(u.searchParams.get('limit') || 50);
      return json({ total: items.length, items: items.slice(off, off + lim) });
    }
    let m;
    if ((m = p.match(/^\/api\/tenders\/(\d+)\/export\/awards$/))) {
      const t = db.tender_details[m[1]];
      if (!t) return json({ detail: 'خارج نطاق نسخة العرض' }, 404);
      const rows = [
        ['ثاقب — تصدير تفاصيل الترسية والعروض'],
        ['المنافسة', t.name || ''],
        ['الرقم المرجعي', t.reference_number || ''],
        ['الجهة', t.agency || ''],
        ['النشاط', t.activity_name_raw || t.activity || ''],
        [],
        ['المورد','قيمة العرض (ر.س)','قيمة الترسية (ر.س)','مطابق فنياً','النتيجة'],
        ...((t.offers || []).map(o => [o.vendor || '', o.offer_value ?? '', (t.awards || []).find(a => a.vendor === o.vendor)?.award_value ?? '', o.technical_pass === true ? 'نعم' : o.technical_pass === false ? 'لا' : '—', o.is_winner ? 'فائز' : 'غير فائز'])),
        ...((t.awards || []).filter(a => !(t.offers || []).some(o => o.vendor === a.vendor)).map(a => [a.vendor || '', '', a.award_value ?? '', '—', 'فائز — ترسية بلا عرض محفوظ']))
      ];
      return csvResponse(rows, `tender_awards_${t.reference_number || m[1]}.csv`);
    }
    if ((m = p.match(/^\/api\/tenders\/(\d+)$/)))
      return db.tender_details[m[1]] ? json(db.tender_details[m[1]])
        : json({ detail: 'خارج نطاق نسخة العرض' }, 404);

    if (p === '/api/agencies') {
      let as = (db.agencies_board || []).slice();
      const q = u.searchParams.get('q');
      if (q) as = as.filter(a => a.canonical_name.includes(q));
      return json(as);
    }
    if ((m = p.match(/^\/api\/agencies\/(\d+)$/)))
      return (db.agency_details || {})[m[1]] ? json(db.agency_details[m[1]])
        : json({ detail: 'خارج نطاق نسخة العرض' }, 404);

    if (p === '/api/vendors') {
      let vs = db.vendors.slice();
      const q = u.searchParams.get('q');
      if (q) vs = vs.filter(v => v.canonical_name.includes(q));
      return json(vs);
    }
    if ((m = p.match(/^\/api\/vendors\/(\d+)\/compare$/))) {
      const detail = (db.vendor_details || {})[m[1]];
      if (!detail) return json({ detail: 'خارج نطاق نسخة العرض' }, 404);
      const stats = detail.stats || {};
      const participations = Number(stats.participations || 0);
      const wins = Number(stats.wins || 0);
      const vendorWinRate = participations ? Math.round((1000 * wins) / participations) / 10 : 0;
      const vendorTechRate = Number(stats.tech_rate || 0);
      const avgOffer = stats.avg_offer == null ? null : Number(stats.avg_offer);
      const cfg = (db.settings && db.settings.my_company) || { name: 'شركتي', target_win_rate_pct: 25, cost_advantage_pct: 0 };
      const targetWin = Number(cfg.target_win_rate_pct ?? 25);
      const costAdv = Number(cfg.cost_advantage_pct ?? 0);
      const targetOffer = avgOffer == null ? null : Math.round(avgOffer * (1 - costAdv / 100) * 100) / 100;
      const gap = Math.round((vendorWinRate - targetWin) * 10) / 10;
      const recommendations = [];
      if (gap > 10) recommendations.push('المورد يملك معدل فوز أعلى من هدفك؛ راقب جهاته المتكررة وافتح فرصًا بسعر أشرس أو عرض فني أقوى عند مواجهته.');
      else if (gap < -10) recommendations.push('هدف شركتك أعلى من أداء هذا المورد؛ يمكنك مهاجمته بثقة في الفرص المشابهة مع الحفاظ على هامش صحي.');
      else recommendations.push('الفجوة قريبة؛ القرار يجب أن يعتمد على الجهة، وزن التقييم الفني، وعدد المنافسين المتوقع.');
      if (vendorTechRate >= 80) recommendations.push('المطابقة الفنية لديه مرتفعة؛ لا تجعل السعر وحده سلاحك، بل اربط العرض بإثباتات امتثال واضحة.');
      else if (vendorTechRate && vendorTechRate < 60) recommendations.push('لديه ضعف فني ظاهر؛ ركّز على اكتمال المتطلبات وتوثيق الخبرات قبل خصم السعر.');
      if (costAdv > 0 && targetOffer != null) recommendations.push(`ميزة التكلفة المحفوظة تعني أن سعرًا حول ${targetOffer.toLocaleString('ar-SA')} ر.س يعادل متوسط عروضه بعد الخصم.`);
      return json({ vendor: { id: detail.id, name: detail.canonical_name, participations, wins, win_rate_pct: vendorWinRate, tech_rate_pct: vendorTechRate, avg_offer: avgOffer }, my_company: { name: cfg.name || 'شركتي', target_win_rate_pct: targetWin, cost_advantage_pct: costAdv, target_offer_vs_vendor_avg: targetOffer }, deltas: { win_rate_gap_pct: gap, technical_gap_pct: Math.round((vendorTechRate - 75) * 10) / 10, cost_advantage_pct: costAdv }, recommendations });
    }
    if ((m = p.match(/^\/api\/vendors\/(\d+)$/))) {
      const detail = (db.vendor_details || {})[m[1]];
      if (!detail) return json({ detail: 'خارج نطاق نسخة العرض' }, 404);
      if (!detail.agency_matrix) {
        detail.agency_matrix = (detail.agencies || []).map(a => ({
          agency: a.agency,
          participations: Number(a.n || 0),
          wins: Number(a.wins || 0),
          win_rate: Number(a.n || 0) ? Math.round((1000 * Number(a.wins || 0)) / Number(a.n || 1)) / 10 : 0,
          tech_rate: detail.stats?.tech_rate ?? null,
          avg_offer: detail.stats?.avg_offer ?? null,
          avg_gap_vs_lowest_pct: null,
        }));
      }
      return json(detail);
    }

    if (p === '/api/pursuits' && method === 'GET') return json(db.pursuits);
    if (p === '/api/pursuits' && method === 'POST') {
      const t = db.tenders.find(x => x.id === body.tender_id) || {};
      const id = Math.max(0, ...db.pursuits.map(x => x.id)) + 1;
      const pur = { id, stage: 'studying', tender_id: body.tender_id, name: t.name,
        reference_number: t.reference_number, source: t.source, agency: t.agency,
        last_offer_date: t.last_offer_date, remaining_s: t.remaining_s, items: 9, items_done: 0 };
      db.pursuits.unshift(pur);
      db.pursuit_details[String(id)] = { ...pur, compliance: DEMO_BASELINE.map((r, i) => ({
        id: id * 100 + i, requirement: r[0], category: r[1], source_ref: r[2],
        status: 'missing', origin: 'rule', confidence: 1 })) };
      return json({ id, created: true });
    }
    if ((m = p.match(/^\/api\/pursuits\/(\d+)$/))) return json(db.pursuit_details[m[1]]);
    if ((m = p.match(/^\/api\/pursuits\/(\d+)\/simulate-price$/))) {
      const proposed = Number(body && body.proposed_price) || 0;
      const detail = db.pursuit_details[m[1]] || {};
      const market = detail.market || {};
      const median = market.median_award || proposed || 1;
      const p25 = market.p25_award || median * 0.85;
      const p75 = market.p75_award || median * 1.15;
      const ratio = proposed / median;
      const win = Math.max(2, Math.min(95, 100 / (1 + Math.exp(4 * (ratio - 0.95)))));
      const settings = db.settings || { calculator: { default_markup_pct: 12, risk_tolerance: 'balanced' } };
      const risk = (body && body.risk_tolerance) || settings.calculator.risk_tolerance || 'balanced';
      const margin = Number((body && body.target_margin_pct) ?? settings.calculator.default_markup_pct ?? 12);
      const riskFactor = ({low: 0.96, balanced: 0.92, high: 0.86})[risk] || 0.92;
      const floorFactor = Math.max(0.55, 1 - margin / 100);
      const optimal = Math.round(median * riskFactor * 100) / 100;
      const floor = Math.round(median * floorFactor * 100) / 100;
      const scenario = { p10: Math.round(Math.max(floor, optimal * 0.93) * 100) / 100, p50: optimal, p90: Math.round(Math.min(p75, optimal * 1.10) * 100) / 100 };
      const modes = [
        { key:'bid_optimizer', label:'Bid Price Optimizer', status: median ? 'ready':'needs_history', primary: optimal, unit:'SAR', note:`احتمالية الفوز ${Math.round(win*10)/10}% عند السعر المقترح.` },
        { key:'boq_line_pricer', label:'BOQ Line-Item Pricer', status:'needs_boq', primary:0, unit:'items', note:'يفتح عند توفر جدول كميات مستخرج من الكراسة أو المرفقات.' },
        { key:'markup_calculator', label:'Markup Calculator', status:'ready', primary: margin, unit:'%', note:'يستخدم هامش الربح الافتراضي من الإعدادات.' },
        { key:'risk_adjusted_pricing', label:'Risk-Adjusted Pricing', status:'ready', primary: Math.round((1-riskFactor)*1000)/10, unit:'% buffer', note:`شهية المخاطرة الحالية: ${risk}.` },
        { key:'competitor_price_match', label:'Competitor Price Match', status:'needs_competitor_history', primary:null, unit:'SAR', note:'يحتاج سجل عروض منافس محدد داخل نفس النشاط.' },
        { key:'agency_calibration', label:'Agency-Specific Calibration', status: market.award_samples ? 'ready':'needs_agency_history', primary: median, unit:'SAR', note:'يبدأ بوسيط النشاط الحالي حتى يتسع corpus.' },
        { key:'boq_completeness', label:'BOQ Completeness Check', status:'needs_boq', primary:0, unit:'%', note:'لا توجد بنود BOQ محفوظة لهذه المنافسة في snapshot.' },
        { key:'scenario_simulator', label:'Scenario Simulator', status:'ready', primary: scenario, unit:'SAR', note:'نطاق P10/P50/P90 مبسط حتى نضيف Monte Carlo كامل.' }
      ];
      return json({
        proposed_price: proposed,
        win_probability_pct: Math.round(win * 10) / 10,
        expected_value: Math.round(proposed * win) / 100,
        competitive_zone: proposed < p25 ? 'aggressive' : proposed <= median ? 'sweet_spot' : proposed <= p75 ? 'conservative' : 'uncompetitive',
        gtpl_abnormally_low_flag: proposed < median * 0.70,
        basis: market.award_samples ? 'activity_history' : 'demo_fallback',
        benchmarks: { sample_count: market.award_samples || 0, median_award: median, p25_award: p25, p75_award: p75, target_margin_pct: margin, risk_tolerance: risk },
        recommendations: { optimal_price: optimal, safe_margin_floor: floor, target_margin_pct: margin, risk_tolerance: risk },
        pricing_ladder: [
          { key: 'aggressive', label: 'هجومي', price: Math.round(median * 0.82 * 100) / 100, note: 'يضغط المنافسين ويرفع احتمالية الفوز، راقب هامش الربح وخطر العرض المنخفض.' },
          { key: 'balanced', label: 'متوازن', price: optimal, note: 'النقطة العملية الأقرب للفوز مع بقاء مساحة ربح معقولة.' },
          { key: 'safe', label: 'آمن', price: floor, note: 'حد أدنى إرشادي لا ينبغي النزول عنه دون مبرر تكلفة موثق.' },
          { key: 'conservative', label: 'متحفظ', price: Math.round(p75 * 0.98 * 100) / 100, note: 'يحافظ على الهامش لكنه قد يخفض احتمالية الفوز إذا كان السوق حساساً للسعر.' }
        ],
        calculator_modes: modes
      });
    }
    if ((m = p.match(/^\/api\/pursuits\/(\d+)\/stage$/))) {
      const pur = db.pursuits.find(x => x.id === +m[1]);
      if (pur) { pur.stage = body.stage; db.pursuit_details[m[1]].stage = body.stage; }
      return json({ ok: true });
    }
    if ((m = p.match(/^\/api\/pursuits\/(\d+)\/outcome$/))) {
      const pur = db.pursuits.find(x => x.id === +m[1]);
      if (pur) { pur.stage = body.result; db.pursuit_details[m[1]].stage = body.result; }
      return json({ ok: true, award_value: null, competitor_count: null });
    }
    if ((m = p.match(/^\/api\/compliance\/(\d+)$/))) {
      for (const d of Object.values(db.pursuit_details)) {
        const it = (d.compliance || []).find(c => c.id === +m[1]);
        if (it) it.status = body.status;
      }
      for (const pur of db.pursuits) {
        const d = db.pursuit_details[String(pur.id)];
        if (d) { pur.items = d.compliance.length;
          pur.items_done = d.compliance.filter(c => ['met', 'n_a'].includes(c.status)).length; }
      }
      return json({ ok: true });
    }

    if (p === '/api/profiles' && method === 'GET') return json(db.profiles);
    if (p === '/api/profiles' && method === 'POST') {
      const id = Math.max(0, ...db.profiles.map(x => x.id)) + 1;
      db.profiles.unshift({ id, active: true, sent_count: 0, last_at: null, ...body });
      return json({ id });
    }
    if ((m = p.match(/^\/api\/profiles\/(\d+)\/toggle$/))) {
      const pr = db.profiles.find(x => x.id === +m[1]);
      if (pr) pr.active = !pr.active;
      return json({ active: pr ? pr.active : false });
    }
    if ((m = p.match(/^\/api\/profiles\/(\d+)$/)) && method === 'DELETE') {
      db.profiles = db.profiles.filter(x => x.id !== +m[1]);
      return json({ deleted: true });
    }
    if (p === '/api/notifications') {
      let items = db.notifications.slice();
      const profileId = u.searchParams.get('profile_id');
      const q = (u.searchParams.get('q') || '').trim();
      if (profileId) items = items.filter(n => String(n.profile_id || '') === profileId || (n.profile_name && db.profiles.find(p => String(p.id) === profileId && p.name === n.profile_name)));
      if (q) items = items.filter(n => [n.title, n.body, n.profile_name].some(v => String(v || '').includes(q)));
      return json(items.slice(0, Number(u.searchParams.get('limit') || 60)));
    }

    return json({ detail: 'not in demo snapshot' }, 404);
  };

  const DEMO_BASELINE = [
    ['سجل تجاري ساري المفعول', 'document', 'نظام المنافسات — متطلبات التأهيل'],
    ['شهادة تسديد الزكاة والدخل سارية', 'document', 'نظام المنافسات — شروط تقديم العطاء'],
    ['شهادة اشتراك الغرفة التجارية سارية', 'document', 'نظام المنافسات — شروط تقديم العطاء'],
    ['شهادة التأمينات الاجتماعية (GOSI)', 'document', 'متطلبات التأهيل النظامية'],
    ['شهادة السعودة / نطاقات', 'document', 'متطلبات التأهيل النظامية'],
    ['خطاب تقديم موقّع بالإقرار بالاطلاع على كراسة الشروط', 'document', 'نظام المنافسات'],
    ['بيان الأعمال السابقة المماثلة', 'qualification', 'نظام المنافسات — تقييم القدرات'],
  ];
}());
