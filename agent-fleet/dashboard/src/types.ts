export type Language = 'ar' | 'en';

export interface SearchRequest {
  query: string;
  language?: Language;
  limit?: number;
  expand_chapter?: boolean;
  expand_adjacent?: boolean;
  expand_xrefs?: boolean;
  cross_language?: boolean;
  use_reranker?: boolean;
  use_dense?: boolean;
  use_lexical?: boolean;
}

export interface HitModel {
  article_id: number;
  instrument_id: number;
  article_no: string;
  text_ar: string;
  text_en: string | null;
  chapter: string | null;
  chapter_no: string | null;
  score: number;
  rank: number;
  snippet: string;
  instrument_title_ar: string | null;
  instrument_tier: number | null;
  in_force_status: string | null;
}

export interface SearchResponse {
  query: string;
  language: string;
  total_candidates: number;
  hits: HitModel[];
}

export interface AskRequest {
  question: string;
  language: Language;
}

export interface TrustSignals {
  citation_coverage: number;
  currency_score: number;
  authority_tier_score: number;
  translation_exposure: number;
  corroboration_score: number;
  retrieval_sufficiency: number;
  overall_trust: number;
  should_abstain: boolean;
}

export interface AskResponse {
  question: string;
  language: string;
  answer: string;
  abstained: boolean;
  citations: Citation[];
  trust_signals: TrustSignals;
}

export interface Citation {
  article_no: string;
  instrument_title: string | null;
  instrument_title_ar?: string;
  instrument_tier?: number | null;
  text_snippet?: string | null;
  validation_error?: string | null;
  coverage_valid: boolean;
  instrument_resolved: boolean;
  matched_article_id?: number | null;
}

export interface HealthResponse {
  status: string;
  version: string;
  components: {
    fastapi: string;
    retrieval: string;
    generation: string;
    database: string;
  };
}

export interface GoldStatsResponse {
  total: number;
  by_category: Record<string, number>;
  by_difficulty: Record<string, number>;
  by_source: Record<string, number>;
  abstention_expected: number;
  path: string;
}

export interface TierBadgeProps {
  tier: number;
  size?: 'sm' | 'md' | 'lg';
}

export interface TrustSignalGaugeProps {
  signal: keyof TrustSignals;
  value: number;
  size?: 'xs' | 'sm' | 'md';
  label?: string;
}

export interface CitationChipProps {
  citation: Citation;
  onClick?: () => void;
}