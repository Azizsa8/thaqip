import { cn } from '../utils';

export type StatusKind = 'inforce' | 'amended' | 'repealed';

const STATUS_META: Record<StatusKind, { ar: string; en: string }> = {
  inforce: { ar: 'سارٍ ونافذ', en: 'IN FORCE' },
  amended: { ar: 'معدّل', en: 'AMENDED' },
  repealed: { ar: 'ملغى', en: 'REPEALED' },
};

export function StatusPill({ kind }: { kind: StatusKind }) {
  const meta = STATUS_META[kind];
  return (
    <span className={cn('status-pill', `status-${kind}`)}>
      <span className="dot" aria-hidden="true" />
      <span>{meta.ar} / {meta.en}</span>
    </span>
  );
}