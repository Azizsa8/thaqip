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
    if (p === '/api/filters') return json(db.filters);

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
    if ((m = p.match(/^\/api\/vendors\/(\d+)$/)))
      return db.vendor_details[m[1]] ? json(db.vendor_details[m[1]])
        : json({ detail: 'خارج نطاق نسخة العرض' }, 404);

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
    if (p === '/api/notifications') return json(db.notifications);

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
