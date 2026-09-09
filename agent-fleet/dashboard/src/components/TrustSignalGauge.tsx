import type { TrustSignals } from '../types';

const TRUST_COLOR = (v: number): string =>
  v >= 0.8 ? 'var(--color-primary)' : v >= 0.5 ? '#d97706' : 'var(--color-error)';

const SIGNAL_LABELS: Record<keyof Omit<TrustSignals, 'should_abstain'>, { ar: string; en: string }> = {
  citation_coverage: { ar: 'تغطية الاستشهاد', en: 'Citation Coverage' },
  currency_score: { ar: 'السريان التشريعي', en: 'Statutory Currency' },
  authority_tier_score: { ar: 'مستوى الحجية', en: 'Authority Level' },
  translation_exposure: { ar: 'التعرض للترجمة', en: 'Translation Exposure' },
  corroboration_score: { ar: 'التعاضد القضائي', en: 'Corroboration' },
  retrieval_sufficiency: { ar: 'كفاية الاسترجاع', en: 'Retrieval Sufficiency' },
  overall_trust: { ar: 'مؤشر الموثوقية', en: 'Reliability Score' },
};

/**
 * Radial trust gauge matching the approved "Legal Confidence & Trust Gauge"
 * (DESIGN.md): a ring scored 0.00–1.00 with an icon core; green >=0.80,
 * ochre 0.50–0.79, crimson <0.50.
 */
export function TrustRing({
  value,
  size = 64,
  label,
}: {
  value: number;
  size?: number;
  label?: string;
}) {
  const clamped = Math.max(0, Math.min(1, value));
  const pct = Math.round(clamped * 100);
  const color = TRUST_COLOR(clamped);

  // Ring geometry (r ~ 15.9155 => circumference ~100, matching the approved SVG)
  const dash = `${pct}, 100`;

  return (
    <div className="inline-flex flex-col items-center gap-1.5">
      <div className="relative flex items-center justify-center" style={{ width: size, height: size }}>
        <svg className="transform -rotate-90" viewBox="0 0 36 36" width={size} height={size}>
          <path
            d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
            fill="none"
            stroke="var(--color-surface-container-highest)"
            strokeWidth="3.5"
          />
          <path
            d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
            fill="none"
            stroke={color}
            strokeWidth="3.5"
            strokeLinecap="round"
            strokeDasharray={dash}
            className="transition-all duration-500"
          />
        </svg>
        <span
          className="material-symbols-outlined absolute"
          style={{ color, fontSize: size * 0.3 }}
        >
          verified_user
        </span>
      </div>
      {label && (
        <span className="text-[11px] text-[var(--color-on-surface-variant)] text-center max-w-[96px] leading-tight">
          {label}
        </span>
      )}
    </div>
  );
}

export function TrustSignalGauge({
  signal,
  value,
  label,
}: {
  signal: keyof Omit<TrustSignals, 'should_abstain'>;
  value: number;
  label?: string;
}) {
  return <div><span>{label ?? SIGNAL_LABELS[signal]?.en}</span><p>{Number.isFinite(value) ? value.toFixed(2) : 'Unavailable'}</p></div>;
}

export function TrustSignalDashboard({ signals }: { signals: TrustSignals }) {
  const keys: (keyof Omit<TrustSignals, 'should_abstain'>)[] = [
    'citation_coverage',
    'currency_score',
    'authority_tier_score',
    'translation_exposure',
    'corroboration_score',
    'retrieval_sufficiency',
  ];

  return (
    <div className="p-5 rounded-2xl bg-white shadow-sm">
      <div className="flex items-center gap-2 mb-4">
        <span className="material-symbols-outlined text-[var(--color-primary)] text-[22px]">psychology</span>
        <h3 className="font-headline-md font-bold text-[var(--color-on-surface)]">
          مؤشر موثوقية النص والحجية
        </h3>
      </div>

      <div className="flex items-center gap-6 mb-5">
        <TrustRing value={signals.overall_trust} size={72} />
        <div>
          <div className="text-2xl font-black" style={{ color: TRUST_COLOR(signals.overall_trust) }}>
            {(Number.isFinite(signals.overall_trust) ? signals.overall_trust.toFixed(2) : 'Unavailable')}
            <span className="text-sm font-medium text-[var(--color-secondary)]"> / 1.00</span>
          </div>
          <div className="text-xs font-semibold text-[var(--color-secondary)]">
            {signals.overall_trust >= 0.8
              ? 'مؤشر مرتفع من الخدمة؛ ليس ضماناً للسريان'
              : signals.overall_trust >= 0.5
              ? 'حجية معتدلة تتطلب تدقيقاً إضافياً'
              : 'حجية ضعيفة — يُنصح بالتحقق اليدوي'}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 sm:grid-cols-6 gap-3">
        {keys.map((k) => (
          <TrustSignalGauge key={k} signal={k} value={signals[k]} />
        ))}
      </div>
    </div>
  );
}