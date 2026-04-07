'use client';

import { Component, useEffect, useRef, useState } from 'react';
import {
  getAlertStats,
  getHealth,
  getLatestAnomalies,
  type AlertRecord,
  type AlertStatsResponse,
  type AnomalyRecord,
  type HealthResponse,
} from '@/lib/api';
import { POLL_INTERVAL_MS } from '@/lib/constants';
import { Badge } from '@/components/ui/Badge';
import { TelemetryChart } from '@/components/dashboard/TelemetryChart';
import { AlertFeed } from '@/components/dashboard/AlertFeed';
import { AlertDetailModal } from '@/components/dashboard/AlertDetailModal';
import { RCAPanel } from '@/components/dashboard/RCAPanel';
import { HostStatusTable } from '@/components/dashboard/HostStatusTable';

// ─── Error boundary ────────────────────────────────────────────────────────────

interface EBState { hasError: boolean; message: string }

class ErrorBoundary extends Component<
  { children: React.ReactNode; name: string },
  EBState
> {
  constructor(props: { children: React.ReactNode; name: string }) {
    super(props);
    this.state = { hasError: false, message: '' };
  }
  static getDerivedStateFromError(err: Error): EBState {
    return { hasError: true, message: err.message };
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="bg-gray-800 border border-red-700/50 rounded-xl p-5 text-red-400 text-sm">
          <p className="font-semibold">{this.props.name} failed to render</p>
          <p className="text-xs text-gray-500 mt-1">{this.state.message}</p>
        </div>
      );
    }
    return this.props.children;
  }
}

// ─── Stat card ────────────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  sub,
  accent = false,
  loading = false,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: boolean;
  loading?: boolean;
}) {
  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl px-5 py-4">
      {loading ? (
        <div className="animate-pulse space-y-2">
          <div className="h-6 bg-gray-700 rounded w-1/2" />
          <div className="h-3 bg-gray-700 rounded w-3/4" />
        </div>
      ) : (
        <>
          <p
            className={`text-2xl font-bold tabular-nums ${
              accent ? 'text-red-400' : 'text-gray-100'
            }`}
          >
            {value}
          </p>
          <p className="text-xs text-gray-400 mt-0.5 font-medium">{label}</p>
          {sub && <p className="text-xs text-gray-600 mt-0.5">{sub}</p>}
        </>
      )}
    </div>
  );
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${Math.floor(seconds % 60)}s`;
}

// ─── Dashboard page ───────────────────────────────────────────────────────────

export default function DashboardPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [alertStats, setAlertStats] = useState<AlertStatsResponse | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [selectedAnomaly, setSelectedAnomaly] = useState<AnomalyRecord | null>(null);
  const [selectedAlert, setSelectedAlert] = useState<AlertRecord | null>(null);
  const [latestAnomaly, setLatestAnomaly] = useState<AnomalyRecord | null>(null);
  const [headerLoading, setHeaderLoading] = useState(true);
  const mountedRef = useRef(true);

  async function fetchHeaderData() {
    try {
      const [healthData, alertStatsData, anomalyData] = await Promise.allSettled([
        getHealth(),
        getAlertStats(),
        getLatestAnomalies(undefined, undefined, 50),
      ]);

      if (!mountedRef.current) return;

      if (healthData.status === 'fulfilled') setHealth(healthData.value);
      if (alertStatsData.status === 'fulfilled') {
        setAlertStats(alertStatsData.value);
      }
      if (anomalyData.status === 'fulfilled') {
        setLatestAnomaly(anomalyData.value.anomalies[0] ?? null);
      }

      setLastUpdated(new Date());
    } finally {
      if (mountedRef.current) setHeaderLoading(false);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    fetchHeaderData();
    const id = setInterval(fetchHeaderData, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const isOnline = health?.status === 'ok';
  const modelsLoaded = health?.models_loaded ?? false;

  return (
    <div className="min-h-screen bg-gray-900 text-gray-100">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-10 bg-gray-900/95 backdrop-blur border-b border-gray-800 px-6 py-3">
        <div className="max-w-screen-2xl mx-auto flex items-center justify-between gap-4">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm select-none">
              FW
            </div>
            <div>
              <h1 className="text-sm font-bold text-gray-100 leading-tight">FlowWatch AI</h1>
              <p className="text-xs text-gray-500 leading-tight">Network Monitoring</p>
            </div>
          </div>

          {/* Status indicators */}
          <div className="flex items-center gap-3 text-xs">
            <Badge variant={isOnline ? 'online' : 'offline'} dot>
              {isOnline ? 'ONLINE' : 'OFFLINE'}
            </Badge>

            <span className={`hidden sm:inline font-medium ${modelsLoaded ? 'text-green-400' : 'text-gray-500'}`}>
              {modelsLoaded ? '⬡ Models loaded' : '○ Models offline'}
            </span>

            {lastUpdated && (
              <span className="hidden md:inline text-gray-600">
                Updated {lastUpdated.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>
      </header>

      <main className="max-w-screen-2xl mx-auto px-4 sm:px-6 py-5 space-y-5">
        {/* ── Stats row ──────────────────────────────────────────────────── */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Records Processed"
            value={(health?.total_records_processed ?? 0).toLocaleString()}
            loading={headerLoading}
          />
          <StatCard
            label="Active Alerts"
            value={alertStats?.total_alerts_fired ?? 0}
            sub="all time"
            accent={(alertStats?.total_alerts_fired ?? 0) > 0}
            loading={headerLoading}
          />
          <StatCard
            label="Critical Alerts"
            value={alertStats?.alerts_by_severity?.['critical'] ?? 0}
            accent={(alertStats?.alerts_by_severity?.['critical'] ?? 0) > 0}
            loading={headerLoading}
          />
          <StatCard
            label="System Uptime"
            value={health ? formatUptime(health.uptime_seconds) : '—'}
            sub={alertStats?.most_affected_host !== 'none' ? `Worst: ${alertStats?.most_affected_host}` : undefined}
            loading={headerLoading}
          />
        </div>

        {/* ── Main grid: chart (60%) + alert feed (40%) ─────────────────── */}
        <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
          <div className="xl:col-span-3">
            <ErrorBoundary name="Telemetry Chart">
              <TelemetryChart />
            </ErrorBoundary>
          </div>
          <div className="xl:col-span-2">
            <ErrorBoundary name="Alert Feed">
              <AlertFeed onViewAnomalies={setSelectedAlert} />
            </ErrorBoundary>
          </div>
        </div>

        {/* ── Bottom grid: host table (50%) + RCA panel (50%) ──────────── */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <ErrorBoundary name="Host Status Table">
            <HostStatusTable />
          </ErrorBoundary>
          <ErrorBoundary name="RCA Panel">
            <RCAPanel
              selectedAnomaly={selectedAnomaly}
              latestAnomaly={latestAnomaly}
            />
          </ErrorBoundary>
        </div>
      </main>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <footer className="text-center py-4 text-xs text-gray-700 border-t border-gray-800 mt-6">
        FlowWatch AI · Phase 9 · Powered by Claude
      </footer>

      {/* ── Alert detail modal ─────────────────────────────────────────────── */}
      <AlertDetailModal
        alert={selectedAlert}
        onClose={() => setSelectedAlert(null)}
        onAnalyzeWithAI={(anomaly) => {
          setSelectedAlert(null);
          setSelectedAnomaly(anomaly);
        }}
      />
    </div>
  );
}
