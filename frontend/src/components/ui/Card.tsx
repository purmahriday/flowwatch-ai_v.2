'use client';

interface CardProps {
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
  /** Extra content placed in the top-right of the header. */
  headerAction?: React.ReactNode;
}

/**
 * Dark-themed container card with optional title, subtitle, and header action.
 * Used as the building block for every dashboard panel.
 */
export function Card({ title, subtitle, children, className = '', headerAction }: CardProps) {
  return (
    <div className={`bg-gray-800 border border-gray-700 rounded-xl ${className}`}>
      {(title || headerAction) && (
        <div className="flex items-start justify-between px-5 pt-4 pb-3 border-b border-gray-700">
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
      <div className="p-5">{children}</div>
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
