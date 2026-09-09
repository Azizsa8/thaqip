import { beforeAll, afterAll, afterEach, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, delay } from 'msw';
import { setupServer } from 'msw/node';
import App from '../src/App';
import { setApiKey } from '../src/api';
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterAll(() => server.close());
afterEach(() => { server.resetHandlers(); setApiKey(''); });
const hit = { article_id: 123, instrument_id: 9, article_no: '4', text_ar: 'نص المادة من الخدمة', text_en: null, chapter: 'الباب الثاني', chapter_no: '2', score: .7, rank: 1, snippet: '<img src=x onerror=alert(1)>', instrument_title_ar: 'نظام الاختبار', instrument_tier: null, in_force_status: null };
const signals = { citation_coverage: .8, currency_score: .7, authority_tier_score: .8, translation_exposure: .1, corroboration_score: .6, retrieval_sufficiency: .8, overall_trust: .75, should_abstain: false };
const answer = { question: 'test', language: 'en', answer: 'Grounded answer', abstained: false, trust_signals: signals, citations: [{ article_no: '4', instrument_title: 'Test law', instrument_tier: null, coverage_valid: false, instrument_resolved: false, matched_article_id: null }] };
function mount(path = '/') { window.history.replaceState({}, '', path); return render(<App />); }
async function search(user: ReturnType<typeof userEvent.setup>, query = 'test') {
  await user.type(screen.getByRole('textbox', { name: 'Search query' }), query);
  await user.click(screen.getByRole('button', { name: /بحث$/ }));
}
async function ask(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByRole('textbox', { name: 'Question' }), 'test');
  await user.click(screen.getByRole('button', { name: /استدلال$/ }));
}
it('sends the search contract, shows loading and renders safe source details without invented metadata', async () => {
  let body: unknown;
  server.use(http.post('*/api/search', async ({ request }) => { body = await request.json(); await delay(80); return HttpResponse.json({ query: 'test', language: 'en', total_candidates: 1, hits: [hit] }); }));
  mount('/?lang=en'); const user = userEvent.setup(); await search(user);
  expect(screen.getByRole('button', { name: /جاري البحث/ })).toBeDisabled();
  expect(await screen.findByText(hit.snippet)).toBeVisible();
  expect(document.querySelector('img')).toBeNull();
  expect(body).toEqual({ query: 'test', language: 'en', limit: 20, expand_chapter: true, expand_adjacent: true, expand_xrefs: true, cross_language: true, use_reranker: false, use_dense: false, use_lexical: true });
  expect(screen.getByText('Authority tier unavailable')).toBeVisible();
  await user.click(screen.getByRole('link', { name: /Source details/ }));
  expect(screen.getByText(hit.text_ar)).toBeVisible();
  expect(screen.getByText('Translation unavailable')).toBeVisible();
  expect(screen.getByText(/Official source URL/)).toBeVisible();
});
it('clears previous results when a retry fails and supports recovery to empty results', async () => {
  server.use(http.post('*/api/search', () => HttpResponse.json({ query: 'test', language: 'ar', total_candidates: 1, hits: [hit] })));
  mount('/?lang=invalid'); const user = userEvent.setup(); await search(user);
  await screen.findByText(hit.snippet);
  server.use(http.post('*/api/search', () => HttpResponse.json({ detail: { message: 'Search unavailable', request_id: 'req-123' } }, { status: 503 })));
  await user.click(screen.getByRole('button', { name: /بحث$/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Search unavailable (Request ID: req-123)');
  expect(screen.queryByText(hit.snippet)).toBeNull();
  server.use(http.post('*/api/search', () => HttpResponse.json({ query: 'test', language: 'ar', total_candidates: 0, hits: [] })));
  await user.click(screen.getByRole('button', { name: /بحث$/ }));
  expect(await screen.findByText(/لا توجد نتائج مطابقة/)).toBeVisible();
});
it('sends ask language and expands unresolved citation details', async () => {
  let body: unknown;
  server.use(http.post('*/api/ask', async ({ request }) => { body = await request.json(); return HttpResponse.json(answer); }));
  mount('/ask?lang=en'); const user = userEvent.setup(); await ask(user);
  expect(await screen.findByText('Grounded answer')).toBeVisible();
  expect(body).toEqual({ question: 'test', language: 'en' });
  await user.click(screen.getByText('Test law · 4'));
  expect(screen.getByText('Unverified')).toBeVisible();
  expect(screen.getByText(/Full source text/)).toBeVisible();
  expect(screen.getByText('Translation Exposure')).toBeVisible();
});
it('shows abstention and empty citations, then clears it on unavailable generation', async () => {
  server.use(http.post('*/api/ask', () => HttpResponse.json({ ...answer, abstained: true, answer: '', citations: [] })));
  mount('/ask'); const user = userEvent.setup(); await ask(user);
  expect(await screen.findByText('امتنع عن الإجابة')).toBeVisible();
  expect(screen.getByText(/No answer: evidence insufficient/)).toBeVisible();
  expect(screen.getByText(/No citations/)).toBeVisible();
  server.use(http.post('*/api/ask', () => HttpResponse.json({ message: 'No generator', request_id: 'g-1' }, { status: 503 })));
  await user.click(screen.getByRole('button', { name: /استدلال$/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('No generator (Request ID: g-1)');
  expect(screen.queryByText('امتنع عن الإجابة')).toBeNull();
});
it('keeps optional credentials in memory, sends Bearer, and clears it', async () => {
  const headers: (string | null)[] = [];
  server.use(http.post('*/api/search', ({ request }) => { headers.push(request.headers.get('Authorization')); return HttpResponse.json({ hits: [], total_candidates: 0, query: 'test', language: 'ar' }); }));
  mount(); const user = userEvent.setup();
  await user.click(screen.getByText('API access / مفتاح الوصول'));
  await user.type(screen.getByLabelText('Session API key'), 'secret-test');
  await user.click(screen.getByRole('button', { name: 'Apply key' }));
  expect(screen.getByLabelText('Session API key')).toHaveValue('');
  await search(user); await screen.findByText(/لا توجد نتائج مطابقة/);
  expect(headers).toEqual(['Bearer secret-test']);
  expect(JSON.stringify(localStorage)).not.toContain('secret-test');
  expect(JSON.stringify(sessionStorage)).not.toContain('secret-test');
  await user.click(screen.getByRole('button', { name: 'Clear key' }));
  await user.click(screen.getByRole('button', { name: /بحث$/ }));
  await waitFor(() => expect(headers).toEqual(['Bearer secret-test', null]));
});
it.each(['/dashboard', '/browse', '/source/9'])('labels unsupported route %s without backend requests', path => {
  mount(path); expect(screen.getByRole('heading', { name: /Feature unavailable/ })).toBeVisible();
});
it('does not replace a direct document URL with mock content', () => {
  mount('/source/9/article/4'); expect(screen.getByText(/Article data unavailable/)).toBeVisible();
});
it('shows authentication failures and network failures without crashing', async () => {
  server.use(http.post('*/api/ask', () => HttpResponse.json({}, { status: 401 })));
  mount('/ask'); const user = userEvent.setup(); await ask(user);
  expect(await screen.findByRole('alert')).toHaveTextContent('Authentication required');
  server.use(http.post('*/api/ask', () => HttpResponse.error()));
  await user.click(screen.getByRole('button', { name: /استدلال$/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach API');
});
