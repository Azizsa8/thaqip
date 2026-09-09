import { Link, useLocation, useParams } from 'react-router-dom';
import type { HitModel } from '../types';
import { Layout } from '../components/Header';
export function DocumentDetailPage() {
  const { sourceId, articleNo } = useParams();
  const { state } = useLocation();
  const hit = state?.hit as HitModel | undefined;
  const matches = hit && String(hit.instrument_id) === sourceId && hit.article_no === articleNo;
  return <Layout><section className="p-8 space-y-4"><h1>Source details / تفاصيل المصدر</h1>
    {!matches ? <p role="status">Article data unavailable. Open a result from search; the API has no document lookup endpoint.</p> : <>
      <h2>{hit.instrument_title_ar || 'Title unavailable'} — {hit.article_no}</h2>
      <p>Snapshot from search response; not a fresh document lookup.</p>
      <dl><dt>Article ID</dt><dd>{hit.article_id}</dd><dt>Instrument ID</dt><dd>{hit.instrument_id}</dd>
        <dt>Chapter</dt><dd>{hit.chapter || 'Unavailable'} {hit.chapter_no}</dd>
        <dt>Authority tier</dt><dd>{hit.instrument_tier ?? 'Unknown'}</dd><dt>Status reported by API</dt><dd>{hit.in_force_status || 'Unknown'}</dd>
        <dt>Rank / score</dt><dd>{hit.rank} / {hit.score}</dd></dl>
      <h3>النص العربي</h3><p dir="rtl" className="whitespace-pre-wrap">{hit.text_ar || 'غير متاح'}</p>
      <h3>English text</h3><p dir="ltr" className="whitespace-pre-wrap">{hit.text_en || 'Translation unavailable'}</p>
      <p>Official source URL, provenance hash, effective dates and version history are not supplied by this API response.</p>
      <Link to={`/ask?q=${encodeURIComponent(`${hit.instrument_title_ar || ''} المادة ${hit.article_no}`)}`}>Ask about this article</Link>
    </>}
    <Link to="/">Return to search</Link>
  </section></Layout>;
}
