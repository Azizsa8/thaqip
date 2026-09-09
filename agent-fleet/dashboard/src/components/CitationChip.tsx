import type { Citation, CitationChipProps } from '../types';
export function CitationChip({ citation }: CitationChipProps) {
  return <details className="bg-white border rounded-lg p-3">
    <summary>{citation.instrument_title_ar || citation.instrument_title || 'Unresolved instrument'} · {citation.article_no}</summary>
    <dl><dt>Citation coverage</dt><dd>{citation.coverage_valid ? 'Matched retrieved article' : 'Unverified'}</dd>
      <dt>Instrument resolved</dt><dd>{citation.instrument_resolved ? 'Yes' : 'No'}</dd>
      <dt>Matched article ID</dt><dd>{citation.matched_article_id ?? 'Unavailable'}</dd>
      <dt>Authority tier</dt><dd>{citation.instrument_tier ?? 'Unavailable'}</dd></dl>
    {citation.text_snippet && <p>{citation.text_snippet}</p>}
    {citation.validation_error && <p>Validation: {citation.validation_error}</p>}
    <p>Full source text and official URL are not supplied with citations. Use Search to locate the article.</p>
  </details>;
}
export function CitationList({ citations, language = 'ar' }: { citations: Citation[]; onCitationClick?: (citation: Citation) => void; language?: 'ar' | 'en' }) {
  return citations.length ? <div className="space-y-2">{citations.map((c, i) => <CitationChip key={i} citation={c} />)}</div>
    : <p>{language === 'ar' ? 'لا توجد استشهادات / No citations' : 'No citations'}</p>;
}
