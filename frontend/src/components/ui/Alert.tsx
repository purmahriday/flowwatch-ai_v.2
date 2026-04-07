'use client';

import { useState } from 'react';

type AlertLevel = 'info' | 'warning' | 'error' | 'success';

const ALERT_STYLES: Record<AlertLevel, { container: string; icon: string }> = {
  info:    { container: 'bg-blue-500/10 border-blue-500/30 text-blue-300',    icon: 'ℹ' },
  warning: { container: 'bg-yellow-500/10 border-yellow-500/30 text-yellow-300', icon: '⚠' },
  error:   { container: 'bg-red-500/10 border-red-500/30 text-red-300',       icon: '✕' },
  success: { container: 'bg-green-500/10 border-green-500/30 text-green-300', icon: '✓' },
};

interface AlertBannerProps {
  level?: AlertLevel;
  message: string;
  dismissible?: boolean;
  className?: string;
}

/**
 * Dismissible inline alert banner with icon.
 * Used for API errors, system notifications, and transient feedback.
 */
export function AlertBanner({
  level = 'info',
  message,
  dismissible = true,
  className = '',
}: AlertBannerProps) {
  const [visible, setVisible] = useState(true);

  if (!visible) return null;

  const { container, icon } = ALERT_STYLES[level];

  return (
    <div
      role="alert"
      className={`flex items-start gap-3 px-4 py-3 rounded-lg border text-sm animate-fade-in ${container} ${className}`}
    >
      <span className="flex-shrink-0 font-bold leading-5" aria-hidden="true">
        {icon}
      </span>
      <p className="flex-1 leading-5">{message}</p>
      {dismissible && (
        <button
          onClick={() => setVisible(false)}
          aria-label="Dismiss"
          className="flex-shrink-0 opacity-60 hover:opacity-100 transition-opacity leading-5"
        >
          ✕
        </button>
      )}
    </div>
  );
}
