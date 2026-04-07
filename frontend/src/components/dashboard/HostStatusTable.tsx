'use client';

import { useEffect, useRef, useState } from 'react';
import { getHosts, getLatestAnomalies, type HostStatusResponse } from '@/lib/api';
import { HEALTH_THRESHOLDS, POLL_INTERVAL_MS } from '@/lib/constants';
import { Card, CardSkeleton } from '@/components/ui/Card';
import { AlertBanner } from '@/components/ui/Alert';

function formatRelativeTime(isoTs: string): string {
  const diffSec = Math.floor((Date.now() - new Date(isoTs).getTime()) / 1000);
  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  return `${Math.floor(diffMin / 60)}h ago`;
}

function HealthBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color =
    score <= HEALTH_THRESHOLDS.GOOD ? 'bg-green-500' :
    score <= HEALTH_THRESHOLDS.WARN ? 'bg-yellow-500' :
    'bg-red-500';

  return (
    <div className="flex items-center gap-2">
      <div className="w-16 h-1.5 bg-gray-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-300 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs text-gray-300 w-6">{pct}%</span>
    </div>
  );
}

function StatusDot({ score, hasAnomaly, hasData }: { score: number; hasAnomaly: boolean; hasData: boolean }) {
  if (!hasData) {
    return (
      <span className="inline-block w-2.5 h-2.5 rounded-full bg-gray-600" title="No data" />
    );
  }
  if (hasAnomaly) {
    return (
      <span
        className="inline-block w-2.5 h-2.5 rounded-full bg-red-500 animate-pulse"
        title="Anomaly active"
      />
    );
  }
  const isHealthy = score <= HEALTH_THRESHOLDS.GOOD;
  const isWarn = score <= HEALTH_THRESHOLDS.WARN;
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${
        isHealthy ? 'bg-green-500' : isWarn ? 'bg-yellow-500' : 'bg-red-400'
      }`}
      title={isHealthy ? 'Healthy' : isWarn ? 'Degraded' : 'Critical'}
    />
  );
}

/**
 * Host health status table.
 *
 * - Fetches /telemetry/hosts every 5 s.
 * - Cross-references /anomalies/latest to flag hosts with active anomalies.
 * - Sorts rows worst-first by health score (highest score = most degraded).
 * - Highlights rows with active anomalies in red.
 */
export function HostStatusTable() {
  const [hosts, setHosts] = useState<HostStatusResponse[]>([]);
  const [anomalyHosts, setAnomalyHosts] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  async function fetchData() {
    try {
      const [hostData, anomalyData] = await Promise.all([
        getHosts(),
        getLatestAnomalies(undefined, undefined, 50),
      ]);
      if (!mountedRef.current) return;

      // Hosts with anomalies detected in the last hour
      const recentCutoff = Date.now() - 60 * 60 * 1000;
      const activeHosts = new Set<string>();
      anomalyData.anomalies.forEach((a) => {
        if (new Date(a.detected_at).getTime() > recentCutoff) {
          activeHosts.add(a.host_id);
        }
      });

      setHosts(hostData);
      setAnomalyHosts(activeHosts);
      setError(null);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load host status');
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    fetchData();
    const id = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Sort worst-first (highest health score = most degraded)
  const sorted = [...hosts].sort((a, b) => {
    if (anomalyHosts.has(b.host_id) !== anomalyHosts.has(a.host_id)) {
      return anomalyHosts.has(b.host_id) ? 1 : -1;
    }
    return b.latest_health_score - a.latest_health_score;
  });

  if (loading) return <CardSkeleton className="h-64" />;

  return (
    <Card
      title="Host Health Status"
      subtitle={`${hosts.length} host${hosts.length !== 1 ? 's' : ''} monitored`}
      className="h-full"
    >
      {error && <AlertBanner level="error" message={error} className="mb-3" />}

      {hosts.length === 0 ? (
        <div className="flex items-center justify-center h-36 text-gray-500 text-sm">
          No hosts reporting yet
        </div>
      ) : (
        <div className="overflow-x-auto -mx-1">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500 text-left border-b border-gray-700">
                <th className="pb-2 pl-1 pr-3 font-medium">Host</th>
                <th className="pb-2 pr-3 font-medium">Status</th>
                <th className="pb-2 pr-3 font-medium">Health</th>
                <th className="pb-2 pr-3 font-medium">Latency</th>
                <th className="pb-2 pr-3 font-medium hidden sm:table-cell">Last Seen</th>
                <th className="pb-2 pr-1 font-medium hidden sm:table-cell">Window</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-700/50">
              {sorted.map((host) => {
                const hasAnomaly = anomalyHosts.has(host.host_id);
                return (
                  <tr
                    key={host.host_id}
                    className={`
                      transition-colors
                      ${hasAnomaly ? 'bg-red-900/10' : 'hover:bg-gray-700/30'}
                    `}
                  >
                    <td className="py-2.5 pl-1 pr-3 font-mono font-medium text-gray-200">
                      {host.host_id}
                    </td>
                    <td className="py-2.5 pr-3">
                      <div className="flex items-center gap-1.5">
                        <StatusDot
                          score={host.latest_health_score}
                          hasAnomaly={hasAnomaly}
                          hasData={host.record_count > 0}
                        />
                        <span className="text-gray-400 hidden md:inline">
                          {hasAnomaly ? 'anomaly' : host.latest_health_score <= HEALTH_THRESHOLDS.GOOD ? 'healthy' : 'degraded'}
                        </span>
                      </div>
                    </td>
                    <td className="py-2.5 pr-3">
                      <HealthBar score={host.latest_health_score} />
                    </td>
                    <td className="py-2.5 pr-3 tabular-nums text-gray-300">
                      {host.latest_latency_ms.toFixed(1)}
                      <span className="text-gray-500"> ms</span>
                    </td>
                    <td className="py-2.5 pr-3 text-gray-500 hidden sm:table-cell">
                      {formatRelativeTime(host.last_seen)}
                    </td>
                    <td className="py-2.5 pr-1 hidden sm:table-cell">
                      {host.window_ready ? (
                        <span className="text-green-500" title="Window ready">✓</span>
                      ) : (
                        <span className="text-gray-600 animate-spin inline-block text-xs" title="Filling window">⟳</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
