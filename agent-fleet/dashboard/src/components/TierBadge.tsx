import type { TierBadgeProps } from '../types';
import { cn } from '../utils';

const TIER_META: Record<number, { ar: string; en: string }> = {
  1: { ar: 'نظام ملكي', en: 'TIER 1: STATUTE' },
  2: { ar: 'لائحة تنفيذية', en: 'TIER 2: REGULATION' },
  3: { ar: 'تعميم / قرار', en: 'TIER 3: CIRCULAR' },
  4: { ar: 'سبق قضائي', en: 'TIER 4: JUDICIAL' },
};

const sizeClasses = {
  sm: 'px-2 py-0.5 text-[10px]',
  md: 'px-2.5 py-1 text-[11px]',
  lg: 'px-3 py-1.5 text-xs',
};

export function TierBadge({ tier, size = 'md' }: TierBadgeProps) {
  const meta = TIER_META[tier] ?? TIER_META[1];
  return (
    <span
      className={cn('tier-badge', `tier-${tier}`, sizeClasses[size], 'shadow-sm')}
      title={meta.en}
    >
      {tier === 1 && <span className="material-symbols-outlined text-[14px]">gavel</span>}
      <span>{meta.en}</span>
    </span>
  );
}