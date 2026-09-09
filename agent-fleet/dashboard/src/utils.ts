import type { Language } from './types';

export function getDirection(lang: Language): 'ltr' | 'rtl' {
  return lang === 'ar' ? 'rtl' : 'ltr';
}

export function getOppositeLanguage(lang: Language): Language {
  return lang === 'ar' ? 'en' : 'ar';
}

export function getLanguageLabel(lang: Language): string {
  return lang === 'ar' ? 'العربية' : 'English';
}

export function formatDate(dateString: string, lang: Language): string {
  const date = new Date(dateString);
  return date.toLocaleDateString(lang === 'ar' ? 'ar-SA' : 'en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

export function formatNumber(num: number, lang: Language): string {
  return new Intl.NumberFormat(lang === 'ar' ? 'ar-SA' : 'en-US').format(num);
}

export function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength).trim() + '...';
}

export function highlightTerms(text: string, query: string): string {
  const terms = query.split(/\s+/).filter(t => t.length > 1);
  let result = text;
  for (const term of terms) {
    const regex = new RegExp(`(${escapeRegExp(term)})`, 'gi');
    result = result.replace(regex, '<mark>$1</mark>');
  }
  return result;
}

function escapeRegExp(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function getTierLabel(tier: number, lang: Language): string {
  const labels: Record<number, { ar: string; en: string }> = {
    1: { ar: 'نظام', en: 'Statute' },
    2: { ar: 'ترجمة رسمية', en: 'Official Translation' },
    3: { ar: 'جهة تنظيمية', en: 'Regulator' },
    4: { ar: 'قضائي (إقناعي)', en: 'Judicial (Persuasive)' },
  };
  return labels[tier]?.[lang] ?? labels[1]?.[lang] ?? '';
}

export function getTrustColor(score: number): 'low' | 'medium' | 'high' {
  if (score < 0.4) return 'low';
  if (score < 0.7) return 'medium';
  return 'high';
}

export function getTrustLabel(score: number, lang: Language): string {
  if (score < 0.4) return lang === 'ar' ? 'منخفض' : 'Low';
  if (score < 0.7) return lang === 'ar' ? 'متوسط' : 'Medium';
  return lang === 'ar' ? 'عالي' : 'High';
}

export function debounce<T extends (...args: unknown[]) => unknown>(
  fn: T,
  delay: number
): (...args: Parameters<T>) => void {
  let timeoutId: ReturnType<typeof setTimeout>;
  return (...args: Parameters<T>) => {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => fn(...args), delay);
  };
}

export function getInitials(name: string): string {
  return name
    .split(/\s+/)
    .map(part => part[0])
    .slice(0, 2)
    .join('')
    .toUpperCase();
}

export function cn(...classes: (string | boolean | undefined | null)[]): string {
  return classes.filter(Boolean).join(' ');
}