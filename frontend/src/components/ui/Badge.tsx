'use client';

import { SEVERITY_COLORS } from '@/lib/constants';

type SeverityVariant = 'critical' | 'high' | 'medium' | 'low';
type StatusVariant = 'online' | 'offline' | 'warning' | 'healthy' | 'anomaly' | 'loading';

type BadgeVariant = SeverityVariant | StatusVariant;

const STATUS_STYLES: Record<StatusVariant, string> = {
  online:   'bg-green-500/20 text-green-400 border border-green-500/30',
  healthy:  'bg-green-500/20 text-green-400 border border-green-500/30',
  offline:  'bg-red-500/20 text-red-400 border border-red-500/30',
  anomaly:  'bg-red-500/20 text-red-400 border border-red-500/30',
  warning:  'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30',
  loading:  'bg-gray-500/20 text-gray-400 border border-gray-500/30',
};

function getSeverityStyle(variant: SeverityVariant): string {
  const c = SEVERITY_COLORS[variant];
  return `${c.bg} ${c.text}`;
}

interface BadgeProps {
  variant: BadgeVariant;
  children: React.ReactNode;
  className?: string;
  dot?: boolean;
}

/**
 * Colored pill badge for severity levels and status indicators.
 * Accepts both severity variants (critical/high/medium/low) and
 * status variants (online/offline/warning/healthy/anomaly).
 */
export function Badge({ variant, children, className = '', dot = false }: BadgeProps) {
  const isSeverity = variant in SEVERITY_COLORS;
  const baseStyle = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold uppercase tracking-wide';
  const colorStyle = isSeverity
    ? getSeverityStyle(variant as SeverityVariant)
    : STATUS_STYLES[variant as StatusVariant] ?? STATUS_STYLES.loading;

  return (
    <span className={`${baseStyle} ${colorStyle} ${className}`}>
      {dot && (
        <span
          className="w-1.5 h-1.5 rounded-full bg-current animate-pulse-slow"
          aria-hidden="true"
        />
      )}
      {children}
    </span>
  );
}
