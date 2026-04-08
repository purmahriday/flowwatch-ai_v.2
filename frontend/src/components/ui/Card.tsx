'use client';

interface CardProps {
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
  /** Extra content placed in the top-right of the header. */
  headerAction?: React.ReactNode;
  /**
   * Extra classes applied to the inner body wrapper (the div that wraps children).
   * Use "flex-1 flex flex-col min-h-0 overflow-hidden" when the card needs to
   * participate in a flex-1 height chain (e.g. scrollable panels in the dashboard).
   */
  bodyClassName?: string;
}

/**
 * Dark-themed container card with optional title, subtitle, and header action.
 * Used as the building block for every dashboard panel.
 */
export function Card({ title, subtitle, children, className = '', headerAction, bodyClassName = '' }: CardProps) {
  return (
    <div className={`bg-gray-800 border border-gray-700 rounded-xl ${className}`}>
      {(title || headerAction) && (
        <div className="shrink-0 flex items-start justify-between px-5 pt-4 pb-3 border-b border-gray-700">
          <div>
            {title && (
              <h2 className="text-sm font-semibold text-gray-100 leading-tight">{title}</h2>
            )}
            {subtitle && (
              <p className="text-xs text-gray-400 mt-0.5">{subtitle}</p>
            )}
          </div>
          {headerAction && <div className="ml-4 flex-shrink-0">{headerAction}</div>}
        </div>
      )}
      <div className={`p-5 ${bodyClassName}`}>{children}</div>
    </div>
  );
}

/** Skeleton placeholder shown while the card's content is loading. */
export function CardSkeleton({ className = '' }: { className?: string }) {
  return (
    <div className={`bg-gray-800 border border-gray-700 rounded-xl p-5 animate-pulse ${className}`}>
      <div className="h-3 bg-gray-700 rounded w-1/3 mb-4" />
      <div className="space-y-2">
        <div className="h-3 bg-gray-700 rounded w-full" />
        <div className="h-3 bg-gray-700 rounded w-5/6" />
        <div className="h-3 bg-gray-700 rounded w-4/6" />
      </div>
    </div>
  );
}
