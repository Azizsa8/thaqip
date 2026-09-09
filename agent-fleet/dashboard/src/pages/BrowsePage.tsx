import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Layout } from '../components/Header';
import { TierBadge } from '../components/TierBadge';
import { StatusPill } from '../components/StatusPill';
import { getDirection, cn } from '../utils';
import type { Language } from '../types';

interface Source {
  id: string;
  ar: string;
  en: string;
  tier: number;
  count: number;
  category: 'statute' | 'regulator' | 'judicial';
  status: 'inforce' | 'amended' | 'repealed';
}

const SOURCES: Source[] = [
  { id: 'companies', ar: 'نظام الشركات', en: 'Companies Law', tier: 1, count: 245, category: 'statute', status: 'inforce' },
  { id: 'labor', ar: 'نظام العمل', en: 'Labor Law', tier: 1, count: 189, category: 'statute', status: 'inforce' },
  { id: 'bankruptcy', ar: 'نظام الإفلاس', en: 'Bankruptcy Law', tier: 1, count: 131, category: 'statute', status: 'amended' },
  { id: 'commercial-register', ar: 'نظام السجل التجاري', en: 'Commercial Register', tier: 1, count: 94, category: 'statute', status: 'inforce' },
  { id: 'zatca', ar: 'قرارات هيئة الزكاة والضريبة', en: 'ZATCA Decisions', tier: 3, count: 234, category: 'regulator', status: 'inforce' },
  { id: 'cma', ar: 'هيئة السوق المالية', en: 'Capital Market Authority', tier: 3, count: 167, category: 'regulator', status: 'inforce' },
  { id: 'sama', ar: 'البنك المركزي السعودي', en: 'SAMA Circulars', tier: 3, count: 143, category: 'regulator', status: 'inforce' },
  { id: 'moj', ar: 'أحكام وزارة العدل', en: 'MoJ Rulings', tier: 4, count: 89, category: 'judicial', status: 'inforce' },
  { id: 'bog', ar: 'ديوان المظالم', en: 'Board of Grievances', tier: 4, count: 76, category: 'judicial', status: 'inforce' },
];

export function BrowsePage() {
  const [searchParams] = useSearchParams();
  const lang = (searchParams.get('lang') as Language) || 'ar';
  const dir = getDirection(lang);

  const [filter, setFilter] = useState<'all' | 'statute' | 'regulator' | 'judicial'>('all');
  const [q, setQ] = useState('');

  const filtered = SOURCES.filter((s) => {
    if (filter !== 'all' && s.category !== filter) return false;
    if (q && !s.ar.includes(q) && !s.en.toLowerCase().includes(q.toLowerCase())) return false;
    return true;
  });

  const categories = [
    { id: 'all', label: 'الكل' },
    { id: 'statute', label: 'الأنظمة' },
    { id: 'regulator', label: 'اللوائح والتعاميم' },
    { id: 'judicial', label: 'الأحكام القضائية' },
  ] as const;

  return (
    <Layout>
      <div className="px-6 lg:px-10 py-6 max-w-[1080px]" dir={dir}>
        <h1 className="font-headline-lg text-2xl font-bold text-[var(--color-on-surface)]">الأنظمة واللوائح</h1>
        <p className="text-[var(--color-on-surface-variant)] mt-1 mb-6">قاعدة تشريعية موثقة من الجريدة الرسمية (أم القرى)</p>

        <div className="bg-white rounded-2xl p-5 shadow-sm mb-6 flex flex-col md:flex-row gap-4">
          <div className="relative flex-1">
            <span className="material-symbols-outlined absolute right-3.5 top-1/2 -translate-y-1/2 text-[var(--color-outline-variant)] text-[18px]">search</span>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="البحث في المصادر..."
              className="w-full pr-10 py-2.5 rounded-xl bg-[var(--color-surface-container-low)] text-sm text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)] focus:bg-white focus:ring-1 focus:ring-[var(--color-primary)] outline-none"
              dir={dir}
            />
          </div>
          <div className="flex gap-1.5 bg-[var(--color-surface-container-low)] p-1.5 rounded-xl self-start">
            {categories.map((c) => (
              <button
                key={c.id}
                onClick={() => setFilter(c.id)}
                className={cn(
                  'px-3.5 py-2 rounded-lg text-sm font-semibold transition-all',
                  filter === c.id
                    ? 'bg-white text-[var(--color-primary)] shadow-sm'
                    : 'text-[var(--color-on-surface-variant)] hover:text-[var(--color-on-surface)]'
                )}
              >
                {c.label}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {filtered.map((s) => (
            <Link
              key={s.id}
              to={`/source/${s.id}/article/1`}
              className="bg-white rounded-2xl p-5 shadow-sm border border-[var(--color-surface-container-high)] hover:border-[var(--color-primary)] transition-all group"
            >
              <div className="flex items-start justify-between gap-3 mb-3">
                <div className="flex items-center gap-2.5">
                  <TierBadge tier={s.tier} />
                  <StatusPill kind={s.status} />
                </div>
                <span className="text-xs text-[var(--color-on-surface-variant)]">{s.count} مادة</span>
              </div>
              <h3 className="font-headline-md font-bold text-[var(--color-on-surface)] group-hover:text-[var(--color-primary)] transition-colors">
                {s.ar}
              </h3>
              <p className="text-sm text-[var(--color-on-surface-variant)] mt-0.5">{s.en}</p>
              <div className="mt-3 flex items-center gap-1 text-[var(--color-primary)] text-sm font-semibold">
                استعراض النظام
                <span className="material-symbols-outlined text-[16px]">arrow_left</span>
              </div>
            </Link>
          ))}
        </div>

        {filtered.length === 0 && (
          <div className="text-center py-16 bg-white rounded-2xl border border-[var(--color-surface-container-high)]">
            <span className="material-symbols-outlined text-[var(--color-outline-variant)] text-[48px] mb-3">search_off</span>
            <p className="text-[var(--color-on-surface-variant)]">لا توجد مصادر مطابقة.</p>
          </div>
        )}
      </div>
    </Layout>
  );
}