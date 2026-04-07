"""
FlowWatch AI — Alert Manager
============================
Evaluates anomaly results against configurable severity thresholds,
deduplicates alerts with per-host cooldown periods, dispatches structured
log messages via loguru, and optionally ships metrics and log events to AWS
CloudWatch.

Design decisions:
    - All in-memory state uses a single ``deque`` (maxlen-bounded) so the
      process never grows unbounded regardless of alert volume.
    - Cooldown is enforced per-host: once an alert fires for ``host-01``,
      subsequent anomalies for that host are suppressed for
      ``cooldown_seconds`` seconds.  Different hosts are independent.
    - CloudWatch failures are always caught and logged as warnings; they never
      prevent the local alert from being recorded.
    - The manager is fully synchronous (no async I/O).  FastAPI callers must
      wrap ``evaluate()`` in ``asyncio.to_thread()`` to avoid blocking the
      event loop during boto3 network calls.

Classes
-------
Alert             — Fired alert record.
AlertRule         — Threshold configuration (for future multi-rule support).
AlertManagerStats — Aggregate statistics over stored alert history.
AlertManager      — Main manager class.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from backend.models.lstm_model import CombinedAnomalyResult

# ─── Constants ────────────────────────────────────────────────────────────────

_SEVERITY_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

_CLOUDWATCH_NAMESPACE = "FlowWatchAI"
_CLOUDWATCH_LOG_GROUP = "/flowwatch/alerts"


# ─── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class Alert:
    """
    A single fired alert record.

    Attributes:
        alert_id:                  UUID4 identifier for this alert.
        host_id:                   Target host that triggered the alert.
        severity:                  One of: critical / high / medium / low.
        combined_score:            Ensemble anomaly score (0–1) that triggered this alert.
        worst_feature:             Most degraded network metric (from LSTM analysis).
        top_contributing_features: Top-3 IF deviation features.
        message:                   Human-readable alert summary.
        timestamp:                 UTC datetime when the alert was created.
        acknowledged:              True once an operator has acknowledged this alert.
        resolved:                  True once the condition has been resolved.
        resolution_timestamp:      UTC datetime when the alert was resolved, or None.
    """

    alert_id: str
    host_id: str
    severity: str
    combined_score: float
    worst_feature: str
    top_contributing_features: list[str]
    message: str
    timestamp: datetime
    acknowledged: bool = False
    resolved: bool = False
    resolution_timestamp: Optional[datetime] = None


@dataclass
class AlertRule:
    """
    Configuration for an alert-firing rule.

    Attributes:
        name:             Human-readable rule name, e.g. ``"default"``.
        min_severity:     Minimum severity level to fire (inclusive).
                          One of: low / medium / high / critical.
        cooldown_seconds: Minimum seconds between consecutive alerts for the
                          same host.
        enabled:          When False the rule is skipped during evaluation.
    """

    name: str
    min_severity: str
    cooldown_seconds: int
    enabled: bool = True


@dataclass
class AlertManagerStats:
    """
    Aggregate statistics computed from the in-memory alert history.

    Attributes:
        total_alerts_fired:    Total alerts that fired (not suppressed).
        alerts_suppressed:     Total alerts suppressed by the cooldown.
        alerts_by_severity:    Count of fired alerts per severity level.
        alerts_by_host:        Count of fired alerts per host.
        most_affected_host:    Host with the most fired alerts ("none" if empty).
        last_alert_timestamp:  Timestamp of the most recently fired alert, or None.
    """

    total_alerts_fired: int
    alerts_suppressed: int
    alerts_by_severity: dict[str, int]
    alerts_by_host: dict[str, int]
    most_affected_host: str
    last_alert_timestamp: Optional[datetime]


# ─── AlertManager ─────────────────────────────────────────────────────────────


class AlertManager:
    """
    Evaluate anomaly results, manage cooldowns, and dispatch alerts.

    The manager is stateful — it owns the in-memory alert deque and the
    per-host cooldown map.  A single instance should be created at startup
    and stored in ``app.state.alert_manager``.

    Args:
        cloudwatch_enabled: Explicitly enable/disable CloudWatch dispatch.
                            ``None`` (default) auto-detects from the presence
                            of ``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY``
                            environment variables.
        cooldown_seconds:   Minimum seconds between consecutive alerts for the
                            same host.  Default: 300 (5 minutes).
        min_severity:       Minimum severity level to fire an alert.
                            Default: ``"medium"`` (low alerts are suppressed).
        max_alerts_stored:  Maximum number of alerts kept in memory.
                            Oldest entries are evicted when the limit is reached.
    """

    def __init__(
        self,
        cloudwatch_enabled: Optional[bool] = None,
        cooldown_seconds: int = 300,
        min_severity: str = "medium",
        max_alerts_stored: int = 1000,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.min_severity = min_severity
        self.max_alerts_stored = max_alerts_stored

        # ── CloudWatch initialisation ─────────────────────────────────────────
        has_aws_creds = bool(
            os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")
        )
        self.cloudwatch_enabled: bool = (
            cloudwatch_enabled if cloudwatch_enabled is not None else has_aws_creds
        )

        self._cw = None        # boto3 CloudWatch client
        self._logs = None      # boto3 CloudWatch Logs client
        self._created_streams: set[str] = set()   # cache of created log streams

        if self.cloudwatch_enabled:
            self._init_cloudwatch()

        # ── In-memory state ───────────────────────────────────────────────────
        self._alerts: deque[Alert] = deque(maxlen=max_alerts_stored)
        self._last_alert_time: dict[str, datetime] = {}   # host → last fired ts
        self._suppressed_count: int = 0

        logger.info(
            "AlertManager initialised | cloudwatch={cw} cooldown={cd}s "
            "min_severity={ms} max_stored={ma}",
            cw=self.cloudwatch_enabled,
            cd=cooldown_seconds,
            ms=min_severity,
            ma=max_alerts_stored,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _init_cloudwatch(self) -> None:
        """
        Attempt to create boto3 CloudWatch and CloudWatch Logs clients.

        If client creation fails (e.g. botocore not installed, bad credentials),
        CloudWatch dispatch is disabled with a warning so the rest of the
        manager continues to function.
        """
        try:
            import boto3  # local import — optional dependency
            region = os.getenv("AWS_REGION", "us-east-1")
            self._cw = boto3.client("cloudwatch", region_name=region)
            self._logs = boto3.client("logs", region_name=region)
            self._ensure_log_group()
            logger.info(
                "CloudWatch clients ready | region={r} log_group={lg}",
                r=region,
                lg=_CLOUDWATCH_LOG_GROUP,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CloudWatch init failed — dispatch disabled | err={e}", e=exc
            )
            self.cloudwatch_enabled = False
            self._cw = None
            self._logs = None

    def _ensure_log_group(self) -> None:
        """Create the CloudWatch Logs group if it does not exist yet."""
        try:
            self._logs.create_log_group(logGroupName=_CLOUDWATCH_LOG_GROUP)
        except self._logs.exceptions.ResourceAlreadyExistsException:
            pass  # Already exists — nothing to do
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create log group | err={e}", e=exc)

    def _ensure_log_stream(self, stream_name: str) -> None:
        """
        Create a CloudWatch Logs stream for ``stream_name`` if it is new.

        Uses a local cache to avoid issuing redundant CreateLogStream API
        calls after the first one succeeds.

        Args:
            stream_name: Name of the log stream (typically the host ID).
        """
        if stream_name in self._created_streams:
            return
        try:
            self._logs.create_log_stream(
                logGroupName=_CLOUDWATCH_LOG_GROUP,
                logStreamName=stream_name,
            )
            self._created_streams.add(stream_name)
        except self._logs.exceptions.ResourceAlreadyExistsException:
            self._created_streams.add(stream_name)  # Mark as known
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not create log stream '{s}' | err={e}", s=stream_name, e=exc
            )

    @staticmethod
    def _severity_passes(severity: str, min_severity: str) -> bool:
        """
        Return True when ``severity`` is at least as severe as ``min_severity``.

        Uses the ``_SEVERITY_ORDER`` mapping; unknown severities default to 0
        (treated as ``"low"``).

        Args:
            severity:     Candidate alert severity.
            min_severity: Minimum threshold severity.

        Returns:
            True if the candidate meets or exceeds the threshold.
        """
        return _SEVERITY_ORDER.get(severity.lower(), 0) >= _SEVERITY_ORDER.get(
            min_severity.lower(), 0
        )

    @staticmethod
    def _build_message(host_id: str, result: CombinedAnomalyResult) -> str:
        """
        Build a concise human-readable alert message from an anomaly result.

        Args:
            host_id: Target host identifier.
            result:  Ensemble anomaly result dataclass.

        Returns:
            Single-line alert summary string.
        """
        features = ", ".join(result.top_contributing_features[:3]) or "n/a"
        return (
            f"[{result.severity.upper()}] Anomaly on {host_id}: "
            f"score={result.combined_score:.3f} via {result.detection_method}. "
            f"Worst feature: {result.worst_feature}. "
            f"Top IF features: {features}."
        )

    # ── Public interface ──────────────────────────────────────────────────────

    def evaluate(
        self,
        host_id: str,
        anomaly_result: CombinedAnomalyResult,
    ) -> Optional[Alert]:
        """
        Evaluate an anomaly result and fire an alert if conditions are met.

        Conditions that must *all* be satisfied for an alert to fire:
            1. ``anomaly_result.is_anomaly`` is True.
            2. The anomaly severity meets the configured ``min_severity``.
            3. The host is not within its cooldown window.

        If any condition fails the alert is suppressed: the suppression counter
        is incremented and ``None`` is returned.  When all conditions pass an
        ``Alert`` is created, stored, dispatched, and returned.

        Args:
            host_id:        Host identifier to evaluate.
            anomaly_result: Ensemble result from ``AnomalyDetector.detect()``.

        Returns:
            Fired ``Alert`` instance, or ``None`` if suppressed.
        """
        if not anomaly_result.is_anomaly:
            return None

        severity = anomaly_result.severity

        # Check severity threshold
        if not self._severity_passes(severity, self.min_severity):
            logger.debug(
                "Alert suppressed (below threshold) | host={h} severity={s} "
                "min_severity={ms}",
                h=host_id,
                s=severity,
                ms=self.min_severity,
            )
            self._suppressed_count += 1
            return None

        # Check cooldown
        if self.is_in_cooldown(host_id):
            last = self._last_alert_time[host_id]
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            logger.debug(
                "Alert suppressed (cooldown) | host={h} elapsed={e:.0f}s "
                "cooldown={c}s",
                h=host_id,
                e=elapsed,
                c=self.cooldown_seconds,
            )
            self._suppressed_count += 1
            return None

        # Build and fire the alert
        now = datetime.now(timezone.utc)
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            host_id=host_id,
            severity=severity,
            combined_score=anomaly_result.combined_score,
            worst_feature=anomaly_result.worst_feature,
            top_contributing_features=list(anomaly_result.top_contributing_features),
            message=self._build_message(host_id, anomaly_result),
            timestamp=now,
        )

        self._alerts.append(alert)
        self._last_alert_time[host_id] = now

        self.dispatch(alert)
        return alert

    def dispatch(self, alert: Alert) -> None:
        """
        Dispatch a fired alert to loguru and (optionally) AWS CloudWatch.

        Log level mapping:
            critical / high → ``logger.critical``
            medium          → ``logger.warning``
            low             → ``logger.info``

        CloudWatch dispatch (when ``cloudwatch_enabled`` is True):
            - ``put_metric_data`` → namespace ``FlowWatchAI``, metric
              ``AnomalyScore``, dimension ``Host=host_id``.
            - ``put_log_events``  → log group ``/flowwatch/alerts``,
              stream named after the host.

        CloudWatch failures are caught and logged as warnings; they never
        propagate to callers.

        Args:
            alert: The ``Alert`` instance to dispatch.
        """
        # ── Loguru dispatch ───────────────────────────────────────────────────
        log_kwargs = dict(
            alert_id=alert.alert_id,
            host=alert.host_id,
            sev=alert.severity,
            score=alert.combined_score,
            feature=alert.worst_feature,
        )
        if alert.severity in ("critical", "high"):
            logger.critical("ALERT FIRED | {msg}", msg=alert.message, **log_kwargs)
        elif alert.severity == "medium":
            logger.warning("ALERT FIRED | {msg}", msg=alert.message, **log_kwargs)
        else:
            logger.info("ALERT FIRED | {msg}", msg=alert.message, **log_kwargs)

        # ── CloudWatch dispatch ───────────────────────────────────────────────
        if not self.cloudwatch_enabled or self._cw is None:
            return

        self._dispatch_cloudwatch_metric(alert)
        self._dispatch_cloudwatch_log(alert)

    def _dispatch_cloudwatch_metric(self, alert: Alert) -> None:
        """
        Publish an ``AnomalyScore`` metric to CloudWatch.

        The metric lives in namespace ``FlowWatchAI`` with dimension
        ``Host=alert.host_id`` so per-host dashboards and alarms can be
        built in the AWS console.

        Args:
            alert: The fired alert containing the score and host metadata.
        """
        try:
            self._cw.put_metric_data(
                Namespace=_CLOUDWATCH_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": "AnomalyScore",
                        "Dimensions": [
                            {"Name": "Host", "Value": alert.host_id},
                            {"Name": "Severity", "Value": alert.severity},
                        ],
                        "Value": alert.combined_score,
                        "Unit": "None",
                        "Timestamp": alert.timestamp,
                    }
                ],
            )
            logger.debug(
                "CloudWatch metric sent | host={h} score={s:.4f}",
                h=alert.host_id,
                s=alert.combined_score,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CloudWatch metric failed | host={h} err={e}",
                h=alert.host_id,
                e=exc,
            )

    def _dispatch_cloudwatch_log(self, alert: Alert) -> None:
        """
        Publish a structured JSON log event to CloudWatch Logs.

        Log group:   ``/flowwatch/alerts``
        Log stream:  ``alert.host_id`` (auto-created on first use)

        The event payload is a JSON object containing all alert fields.

        Args:
            alert: The fired alert to serialise and publish.
        """
        if self._logs is None:
            return

        try:
            self._ensure_log_stream(alert.host_id)

            event_payload = json.dumps(
                {
                    "alert_id": alert.alert_id,
                    "host_id": alert.host_id,
                    "severity": alert.severity,
                    "combined_score": alert.combined_score,
                    "worst_feature": alert.worst_feature,
                    "top_contributing_features": alert.top_contributing_features,
                    "message": alert.message,
                    "timestamp": alert.timestamp.isoformat(),
                }
            )

            self._logs.put_log_events(
                logGroupName=_CLOUDWATCH_LOG_GROUP,
                logStreamName=alert.host_id,
                logEvents=[
                    {
                        "timestamp": int(alert.timestamp.timestamp() * 1000),
                        "message": event_payload,
                    }
                ],
            )
            logger.debug(
                "CloudWatch log event sent | host={h} alert_id={aid}",
                h=alert.host_id,
                aid=alert.alert_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CloudWatch log event failed | host={h} err={e}",
                h=alert.host_id,
                e=exc,
            )

    def get_recent_alerts(
        self,
        host_id: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
    ) -> list[Alert]:
        """
        Return recent alerts from the in-memory store with optional filters.

        Filters are applied with AND semantics: both ``host_id`` and
        ``severity`` must match when both are provided.  Results are
        returned newest-first.

        Args:
            host_id:  Only return alerts for this host.  ``None`` = all hosts.
            severity: Only return alerts with this severity level.
                      ``None`` = all severities.
            limit:    Maximum number of alerts to return.

        Returns:
            List of matching ``Alert`` objects, newest-first, at most ``limit``.
        """
        severity_lower = severity.lower() if severity else None
        host_lower = host_id.lower() if host_id else None

        matching: list[Alert] = []
        # Iterate newest-first (deque is ordered oldest→newest, so reverse)
        for alert in reversed(self._alerts):
            if host_lower and alert.host_id.lower() != host_lower:
                continue
            if severity_lower and alert.severity.lower() != severity_lower:
                continue
            matching.append(alert)
            if len(matching) >= limit:
                break

        return matching

    def acknowledge(self, alert_id: str) -> bool:
        """
        Mark an alert as acknowledged by an operator.

        Args:
            alert_id: UUID of the alert to acknowledge.

        Returns:
            ``True`` if the alert was found and marked; ``False`` otherwise.
        """
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                logger.info("Alert acknowledged | alert_id={aid}", aid=alert_id)
                return True
        return False

    def resolve(self, alert_id: str) -> bool:
        """
        Mark an alert as resolved and record the resolution timestamp.

        Args:
            alert_id: UUID of the alert to resolve.

        Returns:
            ``True`` if the alert was found and resolved; ``False`` otherwise.
        """
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.resolved = True
                alert.resolution_timestamp = datetime.now(timezone.utc)
                logger.info("Alert resolved | alert_id={aid}", aid=alert_id)
                return True
        return False

    def is_in_cooldown(self, host_id: str) -> bool:
        """
        Check whether a host is still within its cooldown window.

        Args:
            host_id: Host identifier to check.

        Returns:
            ``True`` when the time since the last alert for this host is less
            than ``cooldown_seconds``; ``False`` otherwise or if no prior alert
            exists for the host.
        """
        last = self._last_alert_time.get(host_id)
        if last is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed < self.cooldown_seconds

    def get_stats(self) -> AlertManagerStats:
        """
        Compute and return aggregate statistics from the in-memory alert history.

        All statistics are computed over the full deque (not time-windowed).
        Counters are built in a single pass for efficiency.

        Returns:
            ``AlertManagerStats`` with totals, per-severity and per-host
            breakdowns, the most affected host, and the last alert timestamp.
        """
        alerts = list(self._alerts)
        total = len(alerts)

        severity_counter: Counter[str] = Counter(a.severity for a in alerts)
        host_counter: Counter[str] = Counter(a.host_id for a in alerts)

        most_affected = (
            host_counter.most_common(1)[0][0] if host_counter else "none"
        )
        last_ts = (
            max((a.timestamp for a in alerts), default=None)
            if alerts
            else None
        )

        return AlertManagerStats(
            total_alerts_fired=total,
            alerts_suppressed=self._suppressed_count,
            alerts_by_severity={
                "critical": severity_counter.get("critical", 0),
                "high": severity_counter.get("high", 0),
                "medium": severity_counter.get("medium", 0),
                "low": severity_counter.get("low", 0),
            },
            alerts_by_host=dict(host_counter),
            most_affected_host=most_affected,
            last_alert_timestamp=last_ts,
        )
