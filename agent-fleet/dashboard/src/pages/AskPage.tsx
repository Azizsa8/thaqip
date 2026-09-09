import { useState } from 'react';
import { type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, apiError } from '../api';
import type { AskResponse } from '../types';
import { Layout } from '../components/Header';
import { CitationList } from '../components/CitationChip';
import { TrustSignalDashboard } from '../components/TrustSignalGauge';
import { getDirection } from '../utils';


export function AskPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const lang = searchParams.get('lang') === 'en' ? 'en' : 'ar';
  const dir = getDirection(lang);

  const [question, setQuestion] = useState<string>(searchParams.get('q') ?? '');
  const [result, setResult] = useState<AskResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleAsk = async (e: FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    const newParams = new URLSearchParams(searchParams);
    newParams.set('q', question);
    setSearchParams(newParams);

    try {
      setResult(await api.ask({ question: question.trim(), language: lang }));
    } catch (err) {
      setError(apiError(err));
    } finally {
      setLoading(false);
    }
  };

  const examples = [
    'ما هي شروط تأسيس شركة ذات مسؤولية محدودة؟',
    'ما هي حقوق العامل في نظام العمل السعودي؟',
    'ما هي إجراءات تصفية الشركة وفق نظام الشركات؟',
  ];

  return (
    <Layout>
      <div className="px-6 lg:px-10 py-6 max-w-[880px]" dir={dir}>
        <div className="flex items-center gap-2 mb-2">
          <span className="material-symbols-outlined text-[var(--color-primary)] text-[26px]">psychology</span>
          <h1 className="font-headline-lg text-2xl font-bold text-[var(--color-on-surface)]">الاستدلال الذكي الموثق</h1>
        </div>
        <p className="text-[var(--color-on-surface-variant)] mb-6">إجابة قانونية مدعومة بالاستشهادات ومؤشرات الموثوقية</p>

        <form onSubmit={handleAsk} className="bg-white rounded-2xl p-5 shadow-sm mb-6">
          <textarea
            aria-label="Question" value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="مثال: ما هي شروط تأسيس شركة ذات مسؤولية محدودة؟"
            rows={4}
            className="w-full px-4 py-3.5 rounded-xl bg-[var(--color-surface-container-low)] text-base text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)] focus:bg-white focus:ring-1 focus:ring-[var(--color-primary)] outline-none resize-none"
            dir={dir}
            disabled={loading}
          />
          <div className="flex items-center gap-3 mt-4">
            <button
              type="submit"
              disabled={loading || !question.trim()}
              className="px-5 py-2.5 rounded-lg bg-[var(--color-primary)] text-white font-medium text-sm hover:bg-[var(--color-primary-container)] transition-colors disabled:opacity-50 flex items-center gap-2"
            >
              {loading ? (
                <><span className="material-symbols-outlined text-[18px] animate-spin">progress_activity</span> جاري الاستدلال...</>
              ) : (
                <><span className="material-symbols-outlined text-[18px]">auto_awesome</span> استدلال</>
              )}
            </button>
            {error && <span role="alert" className="text-sm text-[var(--color-error)]">{error}</span>}
          </div>
        </form>

        {!result && !loading && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {examples.map((ex) => (
              <button
                key={ex}
                onClick={() => setQuestion(ex)}
                className="p-4 bg-white rounded-2xl shadow-sm border border-[var(--color-surface-container-high)] hover:border-[var(--color-primary)] transition-colors text-right text-sm text-[var(--color-on-surface)]"
              >
                <span className="material-symbols-outlined text-[var(--color-primary)] text-[20px] mb-2 block">help</span>
                {ex}
              </button>
            ))}
          </div>
        )}

        {result && (
          <div className="space-y-5 animate-fade-in">
            <div className="bg-white rounded-2xl p-6 shadow-sm">
              <div className="flex items-center gap-2 mb-4">
                <h2 className="font-headline-md font-bold text-[var(--color-on-surface)]">الإجابة</h2>
                {(result.abstained || result.trust_signals.should_abstain) && (
                  <span className="px-2 py-0.5 rounded-full bg-[var(--color-amended-bg)] text-[var(--color-amended)] text-xs font-semibold">امتنع عن الإجابة</span>
                )}
              </div>
              <div className="text-base text-[var(--color-on-surface)] leading-relaxed whitespace-pre-wrap">{result.answer || (result.abstained ? 'No answer: evidence insufficient / الأدلة غير كافية' : 'Empty answer returned / الإجابة فارغة')}</div>

              {(
                <div className="mt-5 pt-4 border-t border-[var(--color-surface-container-high)]">
                  <h3 className="font-bold text-[var(--color-on-surface)] mb-3 flex items-center gap-2">
                    <span className="material-symbols-outlined text-[var(--color-primary)] text-[20px]">link</span>
                    الاستشهادات
                    <span className="px-2 py-0.5 text-xs bg-[var(--color-surface-container-low)] text-[var(--color-on-surface-variant)] rounded-full">{result.citations.length}</span>
                  </h3>
                  <CitationList citations={result.citations} language={lang} />
                </div>
              )}
            </div>

            <TrustSignalDashboard signals={result.trust_signals} />
          </div>
        )}
      </div>
    </Layout>
  );
}