import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { Language } from '../types';
import { getLanguageLabel, getDirection, cn } from '../utils';

export function LanguageToggle() {
  const [searchParams, setSearchParams] = useSearchParams();

  const currentLang = searchParams.get('lang') === 'en' ? 'en' : 'ar';
  const direction = getDirection(currentLang);

  const toggleLanguage = useCallback((newLang: Language) => {
    setSearchParams({ ...Object.fromEntries(searchParams), lang: newLang });
  }, [searchParams, setSearchParams]);

  return (
    <div
      className={cn(
        'relative inline-flex items-center p-1 rounded-lg',
        'bg-white border border-[var(--color-surface-container-high)]',
        'shadow-sm'
      )}
      dir={direction}
    >
      <button
        type="button"
        onClick={() => toggleLanguage('ar')}
        className={cn(
          'relative z-10 px-3 py-1.5 rounded-md text-sm font-medium',
          'transition-colors',
          currentLang === 'ar'
            ? 'bg-[var(--color-primary)] text-white'
            : 'text-[var(--color-on-surface)] hover:bg-[var(--color-surface-container-low)]'
        )}
        aria-pressed={currentLang === 'ar'}
      >
        {getLanguageLabel('ar')}
      </button>
      <button
        type="button"
        onClick={() => toggleLanguage('en')}
        className={cn(
          'relative z-10 px-3 py-1.5 rounded-md text-sm font-medium',
          'transition-colors',
          currentLang === 'en'
            ? 'bg-[var(--color-primary)] text-white'
            : 'text-[var(--color-on-surface)] hover:bg-[var(--color-surface-container-low)]'
        )}
        aria-pressed={currentLang === 'en'}
      >
        {getLanguageLabel('en')}
      </button>
    </div>
  );
}

export function LanguageProvider({
  children,
  defaultLanguage = 'ar'
}: {
  children: React.ReactNode;
  defaultLanguage?: Language;
}) {
  const [searchParams] = useSearchParams();
  const lang = (searchParams.get('lang') as Language) || defaultLanguage;

  return (
    <html dir={getDirection(lang)} lang={lang}>
      {children}
    </html>
  );
}