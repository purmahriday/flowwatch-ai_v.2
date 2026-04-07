'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { getLatestAnomalies, getRecentTelemetry, type TelemetryRecord } from '@/lib/api';
import {
  CHART_POINTS_PER_HOST,
  DEFAULT_HOST_COLOR,
  HOST_COLORS,
  LATENCY_THRESHOLD_MS,
  POLL_INTERVAL_MS,
} from '@/lib/constants';
import { Card, CardSkeleton } from '@/components/ui/Card';
import { AlertBanner } from '@/components/ui/Alert';

// Unified chart data point: one entry per timestamp, columns per host.
type ChartPoint = {
  time: string;       // HH:MM:SS for X axis
  rawTs: number;      // epoch ms for sorting
  [hostId: string]: number | string; // latency per host, plus 'time' and 'rawTs'
};

type AnomalyTimestampSet = Set<string>; // "host-id|HH:MM:SS"

function formatTime(isoTs: string): string {
  try {
    return new Date(isoTs).toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  } catch {
    return isoTs.slice(-8);
  }
}

interface CustomDotProps {
  cx?: number;
  cy?: number;
  payload?: ChartPoint;
  hostId: string;
  anomalyKeys: AnomalyTimestampSet;
}

function AnomalyDot({ cx, cy, payload, hostId, anomalyKeys }: CustomDotProps) {
  if (!cx || !cy || !payload) return null;
  const key = `${hostId}|${payload.time}`;
  if (!anomalyKeys.has(key)) return null;
  return (
    <circle
      cx={cx}
      cy={cy}
      r={5}
      fill="#ef4444"
      stroke="#fff"
      strokeWidth={1.5}
    />
  );
}

/**
 * Real-time latency line chart.
 *
 * - Polls /telemetry/recent every 5 s, retaining the last 60 points per host.
 * - Renders one colored line per host (host-01 … host-05).
 * - Draws a red dashed reference line at the 200 ms threshold.
 * - Marks anomalous timestamps with a red dot overlay.
 */
export function TelemetryChart() {
  const [records, setRecords] = useState<TelemetryRecord[]>([]);
  const [anomalyKeys, setAnomalyKeys] = useState<AnomalyTimestampSet>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  async function fetchData() {
    try {
      const [telemetry, anomalies] = await Promise.all([
        getRecentTelemetry(undefined, 5, 300),
        getLatestAnomalies(undefined, undefined, 100),
      ]);

      if (!mountedRef.current) return;

      setRecords(telemetry.records);

      // Build lookup: "host|HH:MM:SS" so chart dots can flag anomalous points
      const keys = new Set<string>();
      anomalies.anomalies.forEach((a) => {
        const t = formatTime(a.anomaly_timestamp);
        keys.add(`${a.host_id}|${t}`);
      });
      setAnomalyKeys(keys);
      setError(null);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load telemetry');
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

  // Derive unique host list from fetched records
  const hosts = useMemo(() => {
    const seen = new Set<string>();
    records.forEach((r) => seen.add(r.host_id));
    return Array.from(seen).sort();
  }, [records]);

  // Merge records into unified per-timestamp rows, keep last CHART_POINTS_PER_HOST
  const chartData = useMemo((): ChartPoint[] => {
    const map = new Map<string, ChartPoint>();

    records.forEach((rec) => {
      const time = formatTime(rec.timestamp);
      if (!map.has(time)) {
        map.set(time, {
          time,
          rawTs: new Date(rec.timestamp).getTime(),
        });
      }
      const entry = map.get(time)!;
      entry[rec.host_id] = Math.round(rec.latency_ms * 10) / 10;
    });

    return Array.from(map.values())
      .sort((a, b) => (a.rawTs as number) - (b.rawTs as number))
      .slice(-CHART_POINTS_PER_HOST);
  }, [records]);

  if (loading) return <CardSkeleton className="h-72" />;

  return (
    <Card
      title="Real-time Latency"
      subtitle="Last 5 minutes · refreshes every 5 s"
      className="h-full"
    >
      {error && (
        <AlertBanner level="error" message={error} className="mb-4" />
      )}

      {chartData.length === 0 ? (
        <div className="flex items-center justify-center h-52 text-gray-500 text-sm">
          No telemetry data yet — waiting for records…
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={chartData} margin={{ top: 4, right: 16, left: -8, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="time"
              tick={{ fill: '#9ca3af', fontSize: 10 }}
              interval="preserveStartEnd"
              tickLine={false}
            />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              unit=" ms"
              width={55}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: '#1f2937',
                border: '1px solid #374151',
                borderRadius: '8px',
                fontSize: '12px',
              }}
              labelStyle={{ color: '#f9fafb', fontWeight: 600, marginBottom: 4 }}
              formatter={(value: number, name: string) => [`${value} ms`, name]}
            />
            <Legend
              wrapperStyle={{ paddingTop: 12, fontSize: '12px' }}
              formatter={(value) => (
                <span style={{ color: '#d1d5db' }}>{value}</span>
              )}
            />
            <ReferenceLine
              y={LATENCY_THRESHOLD_MS}
              stroke="#ef4444"
              strokeDasharray="5 5"
              label={{
                value: `${LATENCY_THRESHOLD_MS}ms threshold`,
                position: 'insideTopRight',
                fill: '#ef4444',
                fontSize: 10,
              }}
            />
            {hosts.map((hostId) => (
              <Line
                key={hostId}
                type="monotone"
                dataKey={hostId}
                stroke={HOST_COLORS[hostId] ?? DEFAULT_HOST_COLOR}
                strokeWidth={2}
                dot={(props: Record<string, unknown>) => (
                  <AnomalyDot
                    cx={props.cx as number}
                    cy={props.cy as number}
                    payload={props.payload as ChartPoint}
                    hostId={hostId}
                    anomalyKeys={anomalyKeys}
                  />
                )}
                activeDot={{ r: 4, strokeWidth: 0 }}
                connectNulls
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </Card>
  );
}
