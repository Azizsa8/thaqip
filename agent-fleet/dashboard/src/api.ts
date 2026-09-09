import axios from 'axios';
import type { SearchRequest, SearchResponse, AskRequest, AskResponse, HealthResponse, GoldStatsResponse } from './types';
const client = axios.create({
  baseURL: (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, ''),
  timeout: 60000,
  adapter: 'xhr',
});
let sessionKey = '';
export function setApiKey(key: string) { sessionKey = key.trim(); }
export function hasApiKey() { return Boolean(sessionKey); }
client.interceptors.request.use(config => {
  if (sessionKey) config.headers.set('Authorization', `Bearer ${sessionKey}`);
  return config;
});
export function apiError(error: unknown): string {
  if (!axios.isAxiosError(error)) return 'Unexpected response / استجابة غير متوقعة';
  const data = error.response?.data;
  const detail = data?.detail;
  const message = typeof detail === 'string' ? detail : detail?.message || data?.message;
  const status = error.response?.status;
  const fallback = status === 401 || status === 403 ? 'Authentication required: enter a valid session API key'
    : status === 422 ? 'Invalid request / طلب غير صالح'
    : status === 503 ? 'Service unavailable / الخدمة غير متاحة'
    : error.code === 'ECONNABORTED' ? 'Request timed out / انتهت مهلة الطلب'
    : status ? `Request failed (HTTP ${status})` : 'Cannot reach API / تعذر الاتصال بالخدمة';
  const requestId = data?.request_id || detail?.request_id || error.response?.headers['x-request-id'];
  return `${typeof message === 'string' ? message : fallback}${requestId ? ` (Request ID: ${requestId})` : ''}`;
}
export const api = {
  async search(request: SearchRequest): Promise<SearchResponse> { return (await client.post<SearchResponse>('/search', request)).data; },
  async ask(request: AskRequest): Promise<AskResponse> { return (await client.post<AskResponse>('/ask', request)).data; },
  async health(): Promise<HealthResponse> { return (await client.get('/healthz')).data; },
  async goldStats(): Promise<GoldStatsResponse> { return (await client.get('/gold-stats')).data; },
};
