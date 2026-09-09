import { useState, useCallback } from 'react';
import { type FormEvent } from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { api, apiError } from '../api';
import type { SearchRequest, SearchResponse, HitModel } from '../types';
import { TierBadge } from '../components/TierBadge';

import { Layout } from '../components/Header';
import { getDirection } from '../utils';


const QUICK = ['نظام الشركات', 'نظام العمل', 'نظام الإفلاس', 'اللائحة التنفيذية', 'هيئة السوق المالية'];

export function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const lang = searchParams.get('lang') === 'en' ? 'en' : 'ar';
  const dir = getDirection(lang);

  const [query, setQuery] = useState<string>(searchParams.get('q') ?? '');
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!query.trim()) return;
      setLoading(true);
      setResults(null);
      setError(null);
      const newParams = new URLSearchParams(searchParams);
      newParams.set('q', query);
      setSearchParams(newParams);

      try {
        const request: SearchRequest = {
          query: query.trim(),
          language: lang,
          limit: 20,
          expand_chapter: true,
          expand_adjacent: true,
          expand_xrefs: true,
          cross_language: true,
          use_reranker: false,
          use_dense: false,
          use_lexical: true,
        };
        setResults(await api.search(request));
      } catch (err) {
        setError(apiError(err));
      } finally {
        setLoading(false);
      }
    },
    [query, lang, searchParams, setSearchParams]
  );

  return (
    <Layout>
      <div className="px-6 lg:px-10 py-6 max-w-[1080px]" dir={dir}>
        <h1 className="font-headline-lg text-2xl font-bold text-[var(--color-on-surface)]">
          البحث التشريعي والاستدلال النظامي
        </h1>
        <p className="text-[var(--color-on-surface-variant)] mt-1 mb-6">
          بحث نصي ثنائي اللغة في البيانات المتاحة عبر الخدمة
        </p>

        {/* Search shell */}
        <form onSubmit={handleSearch} className="bg-white rounded-2xl p-5 shadow-sm mb-6">
          <div className="relative">
            <span className="material-symbols-outlined absolute right-3.5 top-1/2 -translate-y-1/2 text-[var(--color-outline-variant)] text-[20px]">search</span>
            <input
              aria-label="Search query" disabled={loading} value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="ابحث في الأنظمة السعودية: مادة، لائحة، تعميم، حكم..."
              className="w-full pr-11 pl-24 py-3.5 rounded-xl bg-[var(--color-surface-container-low)] text-base text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)] border border-transparent focus:bg-white focus:border-[var(--color-primary)] focus:ring-1 focus:ring-[var(--color-primary)] outline-none transition-all"
              dir={dir}
            />
            <span className="absolute left-3 top-1/2 -translate-y-1/2 px-2 py-1 rounded bg-[var(--color-surface-container-highest)] text-[var(--color-on-surface-variant)] text-[11px] font-mono">Ctrl + K</span>
          </div>
          <div className="flex items-center gap-2 mt-4">
            <button
              type="submit"
              disabled={loading || !query.trim()}
              className="px-5 py-2.5 rounded-lg bg-[var(--color-primary)] text-white font-medium text-sm hover:bg-[var(--color-primary-container)] transition-colors disabled:opacity-50 flex items-center gap-2"
            >
              {loading ? (
                <><span className="material-symbols-outlined text-[18px] animate-spin">progress_activity</span> جاري البحث...</>
              ) : (
                <><span className="material-symbols-outlined text-[18px]">search</span> بحث</>
              )}
            </button>
            <div className="flex flex-wrap gap-2">
              {QUICK.map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => setQuery(q)}
                  className="px-3 py-1.5 rounded-full bg-[var(--color-surface-container-low)] text-[var(--color-on-surface-variant)] text-xs hover:bg-[var(--color-surface-container-high)] transition-colors"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
          {error && <div role="alert" className="mt-3 p-3 rounded-lg bg-[var(--color-error-container)] text-[var(--color-on-error-container)] text-sm">{error}</div>}
        </form>

        {/* Results */}
        {results && (
          <div className="flex items-center gap-2 mb-4 text-sm text-[var(--color-on-surface-variant)]">
            <span className="font-bold text-[var(--color-on-surface)]">نتائج البحث</span>
            <span className="font-mono text-[var(--color-on-surface-variant)]">({results.total_candidates} مرشح)</span>
          </div>
        )}

        {results && results.hits.length === 0 && !loading && (
          <div className="text-center py-16 bg-white rounded-2xl border border-[var(--color-surface-container-high)]">
            <span className="material-symbols-outlined text-[var(--color-outline-variant)] text-[48px] mb-3">search_off</span>
            <p className="text-[var(--color-on-surface-variant)]">لا توجد نتائج مطابقة. جرّب مصطلحات أوسع أو تحقق من الإملاء.</p>
          </div>
        )}

        <div className="flex flex-col gap-4">
          {results?.hits.map((hit) => (
            <ResultCard key={hit.article_id} hit={hit} />
          ))}
        </div>
      </div>
    </Layout>
  );
}

function ResultCard({ hit }: { hit: HitModel }) {
  return <article className="bg-white rounded-2xl p-5 shadow-sm">
    <h2>{hit.instrument_title_ar || 'عنوان غير متاح'} — المادة {hit.article_no}</h2>
    {hit.instrument_tier != null ? <TierBadge tier={hit.instrument_tier} /> : <span>Authority tier unavailable</span>}
    <p>Status: {hit.in_force_status || 'Unknown / غير معلوم'}</p>
    <p>{hit.snippet || hit.text_ar}</p>
    <Link to={`/source/${hit.instrument_id}/article/${encodeURIComponent(hit.article_no)}`} state={{ hit }}>Source details / تفاصيل المصدر</Link>
  </article>;
}
