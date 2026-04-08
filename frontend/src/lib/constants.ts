// Application-wide constants for FlowWatch AI frontend.

/** Polling interval for all auto-refreshing components (ms). */
export const POLL_INTERVAL_MS = 5_000;

/** Maximum chart data points retained per host. */
export const CHART_POINTS_PER_HOST = 60;

/** Latency threshold line displayed on the telemetry chart (ms). */
export const LATENCY_THRESHOLD_MS = 200;

/** Color assigned to each host in charts and status indicators. */
export const HOST_COLORS: Record<string, string> = {
  // Legacy simulated host format
  'host-01': '#3b82f6', // blue
  'host-02': '#22c55e', // green
  'host-03': '#eab308', // yellow
  'host-04': '#f97316', // orange
  'host-05': '#ef4444', // red
  // Phase 12 real website hosts
  'google.com':     '#3b82f6', // blue
  'github.com':     '#22c55e', // green
  'cloudflare.com': '#f97316', // orange
  'amazon.com':     '#a855f7', // purple
  '1.1.1.1':        '#06b6d4', // cyan
};

/**
 * Ordered color palette used to assign colors dynamically to hosts that are
 * not listed in HOST_COLORS. Colors are assigned by the host's sort index,
 * so they are stable across renders as long as the host set doesn't change.
 */
export const COLOR_PALETTE: readonly string[] = [
  '#3b82f6', // blue
  '#22c55e', // green
  '#f97316', // orange
  '#a855f7', // purple
  '#06b6d4', // cyan
  '#eab308', // yellow
  '#ef4444', // red
  '#ec4899', // pink
  '#14b8a6', // teal
  '#f59e0b', // amber
];

/** Fallback color for hosts not in HOST_COLORS and beyond the palette range. */
export const DEFAULT_HOST_COLOR = '#6b7280';

/** Tailwind + hex color palette for severity levels. */
export const SEVERITY_COLORS: Record<string, { bg: string; text: string; hex: string }> = {
  critical: { bg: 'bg-red-500/20',    text: 'text-red-400',    hex: '#ef4444' },
  high:     { bg: 'bg-orange-500/20', text: 'text-orange-400', hex: '#f97316' },
  medium:   { bg: 'bg-yellow-500/20', text: 'text-yellow-400', hex: '#eab308' },
  low:      { bg: 'bg-blue-500/20',   text: 'text-blue-400',   hex: '#3b82f6' },
};

/** Health score thresholds for color coding. */
export const HEALTH_THRESHOLDS = {
  GOOD: 0.3,    // score ≤ 0.3 → green
  WARN: 0.6,    // score ≤ 0.6 → yellow
  // > 0.6 → red
};
