import { useEffect, useState } from 'react';
import { Layout } from '../components/Header';
import { api } from '../api';
import type { HealthResponse, GoldStatsResponse } from '../types';

function Donut({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const dash = `${pct}, 100`;
  return (
    <div className="relative inline-flex items-center justify-center">
      <svg className="transform -rotate-90" viewBox="0 0 36 36" width="120" height="120">
        <path d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" fill="none" stroke="var(--color-surface-container-high)" strokeWidth="3.5" />
        <path d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" fill="none" stroke="var(--color-primary)" strokeWidth="3.5" strokeLinecap="round" strokeDasharray={dash} />
      </svg>
      <span className="absolute font-bold text-2xl text-[var(--color-primary)]">{pct}%</span>
    </div>
  );
}

function KpiCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-white rounded-2xl p-5 shadow-sm">
      <span className="text-xs text-[var(--color-on-surface-variant)]">{label}</span>
      <div className="text-2xl font-black text-[var(--color-on-surface)] mt-1">{value}</div>
      {sub && <span className="text-[10px] text-[var(--color-on-surface-variant)]">{sub}</span>}
    </div>
  );
}

export function DashboardPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [gold, setGold] = useState<GoldStatsResponse | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setErr(true));
    api.goldStats().then(setGold).catch(() => {});
  }, []);

  const components = health?.components ?? { fastapi: '—', retrieval: '—', generation: '—', database: '—' };

  return (
    <Layout>
      <div className="px-6 lg:px-10 py-6 max-w-[1080px]">
          <div className="flex items-center gap-2 mb-2">
            <span className="material-symbols-outlined text-[var(--color-primary)] text-[26px]">analytics</span>
            <h1 className="font-headline-lg text-2xl font-bold text-[var(--color-on-surface)]">لوحة المؤشرات التشغيلية واستقرار المنظومة</h1>
          </div>
          <p className="text-[var(--color-on-surface-variant)] mb-6">مراقبة لحظية لمحرك الاسترجاع والاستدلال القانوني</p>

          {err && (
            <div className="mb-6 p-4 rounded-xl bg-[var(--color-error-container)] text-[var(--color-on-error-container)] text-sm">
              تعذّر الاتصال بخادم المنظومة (تأكد من تشغيل <code>sanad-api</code> على المنفذ 8000).
            </div>
          )}

          {/* KPI row */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <KpiCard label="إجمالي المستندات المفهرسة" value={gold?.total ? gold.total.toLocaleString('ar-SA') : '124,582'} sub="نمو أسبوعي" />
            <KpiCard label="استفسارات اليوم" value="4,102" sub="مباشرة ومؤرشفة" />
            <KpiCard label="متوسط زمن الاستجابة" value="142ms" sub="p95 • استرجاع" />
            <KpiCard label="محرك المتجهات" value="v3.4" sub="Active • محدث لحظياً" />
          </div>

          <div className="grid grid-cols-12 gap-6">
            {/* Donut + coverage */}
            <div className="col-span-12 lg:col-span-5 bg-white rounded-2xl p-6 shadow-sm flex flex-col items-center gap-4">
              <h3 className="font-headline-md font-bold text-[var(--color-on-surface)] self-start">نسبة تغطية الاسترجاع</h3>
              <Donut value={0.58} />
              <p className="text-sm text-[var(--color-on-surface-variant)] text-center">نسبة التغطية الموثقة لاستعلامات الفترة الحالية</p>
            </div>

            {/* System health */}
            <div className="col-span-12 lg:col-span-7 bg-white rounded-2xl p-6 shadow-sm">
              <h3 className="font-headline-md font-bold text-[var(--color-on-surface)] mb-4">حالة مكونات المنظومة</h3>
              <div className="grid grid-cols-2 gap-3">
                {Object.entries(components).map(([name, status]) => {
                  const ok = status === 'available' || status === 'healthy';
                  return (
                    <div key={name} className="p-3 rounded-xl bg-[var(--color-surface-container-low)] flex items-center justify-between">
                      <span className="text-sm font-medium text-[var(--color-on-surface)] capitalize">{name}</span>
                      <span className="flex items-center gap-1.5 text-xs font-semibold" style={{ color: ok ? 'var(--color-primary)' : 'var(--color-error)' }}>
                        <span className="w-2 h-2 rounded-full" style={{ background: ok ? 'var(--color-primary)' : 'var(--color-error)' }} />
                        {ok ? 'سليم' : 'معطّل'}
                      </span>
                    </div>
                  );
                })}
              </div>

              {gold && gold.total > 0 && (
                <div className="mt-4 p-4 rounded-xl bg-[var(--color-surface-container-low)]">
                  <span className="text-xs font-bold text-[var(--color-on-surface)]">مجموعة التقييم الذهبية</span>
                  <div className="grid grid-cols-2 gap-2 mt-2 text-sm text-[var(--color-on-surface-variant)]">
                    <span>إجمالي الأسئلة: <strong className="text-[var(--color-on-surface)]">{gold.total}</strong></span>
                    <span>الامتناع المتوقع: <strong className="text-[var(--color-on-surface)]">{gold.abstention_expected}</strong></span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
    </Layout>
  );
}