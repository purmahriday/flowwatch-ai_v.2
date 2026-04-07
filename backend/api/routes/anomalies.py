"""
FlowWatch AI — Anomaly Route Handlers
======================================
Handles on-demand anomaly detection and serves the in-memory anomaly history.

Endpoints
---------
POST /anomalies/detect
    Accepts a raw feature vector dict and runs both LSTM and Isolation Forest
    detectors via AnomalyDetector.detect().  Returns full ensemble result plus
    a rule-based recommendation.  Also persists the result to the anomaly store.

GET /anomalies/latest
    Returns recent anomaly records from the in-memory store, with optional
    filtering by host and severity.

GET /anomalies/stats
    Returns aggregate statistics across all hosts and the current hour.

In-memory anomaly store (app.state.anomaly_store):
    dict[host_id, deque[AnomalyRecord]]  — maxlen=500 per host
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger

from backend.api.dependencies import get_anomaly_detector, severity_recommendation
from backend.api.schemas import (
    AlertRecentResponse,
    AlertSchema,
    AlertStatsSchema,
    AnomalyDetectRequest,
    AnomalyDetectResponse,
    AnomalyListResponse,
    AnomalyRecord,
    AnomalyStatsResponse,
    CombinedAnomalyResultSchema,
    IFResultSchema,
    LSTMResultSchema,
)
from backend.models.feature_engineering import (
    FeatureVector,
    WINDOW_SIZE,
    _IF_FEATURE_ORDER,
)
from backend.models.lstm_model import AnomalyDetector, CombinedAnomalyResult

router = APIRouter()

_ANOMALY_MAXLEN: int = 500


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _reconstruct_feature_vector(host_id: str, fv_dict: dict) -> FeatureVector:
    """
    Reconstruct a FeatureVector from the dict produced by FeatureVector.to_dict().

    The raw LSTM window is not serialised by to_dict(), so a zero-filled
    placeholder is used.  The LSTM reconstruction error will reflect a
    near-zero input, meaning the IF score dominates for on-demand requests.

    Args:
        host_id:  Host identifier (may differ from the value inside fv_dict).
        fv_dict:  Dict from FeatureVector.to_dict() — must contain all 19
                  scalar features plus a ``timestamp`` key.

    Returns:
        FeatureVector ready for AnomalyDetector.detect().

    Raises:
        KeyError:   If any of the 19 required feature keys are absent.
        ValueError: If feature values cannot be cast to float.
    """
    timestamp: str = fv_dict.get("timestamp", datetime.now(timezone.utc).isoformat())

    feature_kwargs = {name: float(fv_dict[name]) for name in _IF_FEATURE_ORDER}

    # Placeholder raw window for the LSTM input path
    raw_window = np.zeros((WINDOW_SIZE, 5), dtype=np.float64)

    return FeatureVector(
        host_id=host_id,
        timestamp=timestamp,
        _raw_window=raw_window,
        **feature_kwargs,
    )


def _combined_to_schema(result: CombinedAnomalyResult) -> CombinedAnomalyResultSchema:
    """Convert a CombinedAnomalyResult dataclass to its Pydantic schema."""
    return CombinedAnomalyResultSchema(
        is_anomaly=result.is_anomaly,
        combined_score=result.combined_score,
        severity=result.severity,
        lstm_result=LSTMResultSchema(
            is_anomaly=result.lstm_result.is_anomaly,
            anomaly_score=result.lstm_result.anomaly_score,
            reconstruction_error=result.lstm_result.reconstruction_error,
            threshold_used=result.lstm_result.threshold_used,
            per_feature_errors=result.lstm_result.per_feature_errors,
            worst_feature=result.lstm_result.worst_feature,
            inference_time_ms=result.lstm_result.inference_time_ms,
            model_version=result.lstm_result.model_version,
        ),
        if_result=IFResultSchema(
            is_anomaly=result.if_result.is_anomaly,
            anomaly_score=result.if_result.anomaly_score,
            raw_score=result.if_result.raw_score,
            confidence=result.if_result.confidence,
            top_contributing_features=result.if_result.top_contributing_features,
            host_id=result.if_result.host_id,
            timestamp=result.if_result.timestamp,
            model_version=result.if_result.model_version,
            inference_time_ms=result.if_result.inference_time_ms,
        ),
        detection_method=result.detection_method,
        worst_feature=result.worst_feature,
        top_contributing_features=result.top_contributing_features,
        timestamp=result.timestamp,
    )


def _combined_to_record(
    combined: CombinedAnomalyResult,
    host_id: str,
    recommendation: str,
) -> AnomalyRecord:
    """Build a persistable AnomalyRecord from a CombinedAnomalyResult."""
    return AnomalyRecord(
        record_id=str(uuid.uuid4()),
        host_id=host_id,
        detected_at=datetime.now(timezone.utc),
        severity=combined.severity,
        combined_score=combined.combined_score,
        is_anomaly=combined.is_anomaly,
        detection_method=combined.detection_method,
        worst_feature=combined.worst_feature,
        top_contributing_features=combined.top_contributing_features,
        lstm_result=LSTMResultSchema(
            is_anomaly=combined.lstm_result.is_anomaly,
            anomaly_score=combined.lstm_result.anomaly_score,
            reconstruction_error=combined.lstm_result.reconstruction_error,
            threshold_used=combined.lstm_result.threshold_used,
            per_feature_errors=combined.lstm_result.per_feature_errors,
            worst_feature=combined.lstm_result.worst_feature,
            inference_time_ms=combined.lstm_result.inference_time_ms,
            model_version=combined.lstm_result.model_version,
        ),
        if_result=IFResultSchema(
            is_anomaly=combined.if_result.is_anomaly,
            anomaly_score=combined.if_result.anomaly_score,
            raw_score=combined.if_result.raw_score,
            confidence=combined.if_result.confidence,
            top_contributing_features=combined.if_result.top_contributing_features,
            host_id=combined.if_result.host_id,
            timestamp=combined.if_result.timestamp,
            model_version=combined.if_result.model_version,
            inference_time_ms=combined.if_result.inference_time_ms,
        ),
        anomaly_timestamp=combined.timestamp,
        recommendation=recommendation,
    )


# ─── POST /anomalies/detect ───────────────────────────────────────────────────


@router.post(
    "/detect",
    response_model=AnomalyDetectResponse,
    status_code=status.HTTP_200_OK,
    summary="On-demand anomaly detection from a feature vector",
)
async def detect_anomaly(
    body: AnomalyDetectRequest,
    request: Request,
    anomaly_detector: Optional[AnomalyDetector] = Depends(get_anomaly_detector),
) -> AnomalyDetectResponse:
    """
    Run ensemble anomaly detection on a caller-supplied feature vector.

    The feature vector must be the dict produced by ``FeatureVector.to_dict()``
    — i.e. all 19 scalar features plus ``host_id`` and ``timestamp``.  The raw
    LSTM window is not required; a zero-filled placeholder is used internally.

    The result is also stored in the in-memory anomaly store so it appears in
    ``GET /anomalies/latest``.

    Args:
        body:             Host ID and feature vector dict.
        request:          Provides app.state access.
        anomaly_detector: Loaded AnomalyDetector, or None if models missing.

    Returns:
        AnomalyDetectResponse with full ensemble result and recommendation.

    Raises:
        HTTPException 503: Models not loaded yet.
        HTTPException 422: Feature vector dict is missing required keys.
    """
    if anomaly_detector is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ML models are not loaded. Train or provide model artifacts and "
                "restart the service."
            ),
        )

    # Reconstruct FeatureVector from the supplied dict
    try:
        feature_vector = _reconstruct_feature_vector(body.host_id, body.feature_vector)
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid feature_vector: {exc}",
        ) from exc

    # Run inference in a thread pool (CPU-bound)
    combined: CombinedAnomalyResult = await asyncio.to_thread(
        anomaly_detector.detect, feature_vector
    )
    detection_ts = datetime.now(timezone.utc)
    recommendation = severity_recommendation(combined.severity)

    logger.info(
        "On-demand detection | host={host} anomaly={a} severity={sev} "
        "score={score:.4f} method={method}",
        host=body.host_id,
        a=combined.is_anomaly,
        sev=combined.severity,
        score=combined.combined_score,
        method=combined.detection_method,
    )

    # Persist to anomaly store
    store: dict[str, deque] = request.app.state.anomaly_store
    if body.host_id not in store:
        store[body.host_id] = deque(maxlen=_ANOMALY_MAXLEN)
    store[body.host_id].append(
        _combined_to_record(combined, body.host_id, recommendation)
    )

    return AnomalyDetectResponse(
        host_id=body.host_id,
        detection_timestamp=detection_ts,
        recommendation=recommendation,
        result=_combined_to_schema(combined),
    )


# ─── GET /anomalies/latest ────────────────────────────────────────────────────


@router.get(
    "/latest",
    response_model=AnomalyListResponse,
    summary="Fetch recent anomaly records",
)
async def get_latest_anomalies(
    request: Request,
    host_id: Optional[str] = Query(None, description="Filter to a single host"),
    severity: Optional[str] = Query(
        None,
        description="Filter by severity: critical / high / medium / low",
    ),
    limit: int = Query(50, ge=1, le=200, description="Max records to return [1, 200]"),
    minutes: int = Query(60, ge=1, le=1440, description="Lookback window in minutes"),
) -> AnomalyListResponse:
    """
    Return recent AnomalyRecords from the in-memory store.

    Filters applied (all optional, combined with AND):
        - ``host_id``: Only records for the specified host.
        - ``severity``: One of critical / high / medium / low.
        - ``minutes``: Records detected within the last N minutes.

    Results are sorted newest-first and capped at ``limit``.

    Args:
        request:  Provides app.state.anomaly_store.
        host_id:  Optional host filter.
        severity: Optional severity filter.
        limit:    Maximum records in response.
        minutes:  Lookback window.

    Returns:
        AnomalyListResponse with filtered records and summary counts.
    """
    store: dict[str, deque] = request.app.state.anomaly_store
    cutoff = datetime.now(timezone.utc)

    severity_lower = severity.lower() if severity else None
    valid_severities = {"critical", "high", "medium", "low"}
    if severity_lower and severity_lower not in valid_severities:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"severity must be one of: {', '.join(sorted(valid_severities))}",
        )

    hosts_to_query = [host_id] if host_id else list(store.keys())

    matching: list[AnomalyRecord] = []
    for hid in hosts_to_query:
        if hid not in store:
            continue
        for record in store[hid]:
            age = (cutoff - record.detected_at).total_seconds() / 60.0
            if age > minutes:
                continue
            if severity_lower and record.severity.lower() != severity_lower:
                continue
            matching.append(record)

    # Sort newest-first, cap at limit
    matching.sort(key=lambda r: r.detected_at, reverse=True)
    matching = matching[:limit]

    critical_count = sum(1 for r in matching if r.severity == "critical")
    high_count = sum(1 for r in matching if r.severity == "high")

    host_counts: dict[str, int] = Counter(r.host_id for r in matching)

    return AnomalyListResponse(
        anomalies=matching,
        total_count=len(matching),
        critical_count=critical_count,
        high_count=high_count,
        summary_by_host=dict(host_counts),
    )


# ─── GET /anomalies/stats ─────────────────────────────────────────────────────


@router.get(
    "/stats",
    response_model=AnomalyStatsResponse,
    summary="Aggregate anomaly statistics",
)
async def get_anomaly_stats(request: Request) -> AnomalyStatsResponse:
    """
    Return aggregate anomaly statistics across all hosts.

    Metrics computed over the entire in-memory anomaly store (all time):
        - ``total_anomalies_detected``: All stored anomaly records.
        - ``anomaly_rate_last_hour``: Fraction of last-hour detections that are
          anomalies (always 1.0 since the store only contains anomalies).
        - ``most_affected_host``: Host with the most anomaly records.
        - ``most_common_severity``: Severity level appearing most often.
        - ``worst_feature_overall``: Most frequently cited worst_feature.
        - ``detection_breakdown``: Counts by detection_method.

    Args:
        request: Provides app.state.anomaly_store and telemetry_store.

    Returns:
        AnomalyStatsResponse with all computed statistics.
    """
    anomaly_store: dict[str, deque] = request.app.state.anomaly_store
    telemetry_store: dict[str, deque] = request.app.state.telemetry_store
    cutoff = datetime.now(timezone.utc)

    # Flatten all anomaly records
    all_records: list[AnomalyRecord] = [
        rec
        for records in anomaly_store.values()
        for rec in records
    ]

    total = len(all_records)

    if total == 0:
        return AnomalyStatsResponse(
            total_anomalies_detected=0,
            anomaly_rate_last_hour=0.0,
            most_affected_host="none",
            most_common_severity="none",
            worst_feature_overall="none",
            detection_breakdown={"lstm+if": 0, "lstm_only": 0, "if_only": 0, "none": 0},
        )

    # Anomaly rate last hour
    last_hour = [
        r for r in all_records
        if (cutoff - r.detected_at).total_seconds() <= 3600
    ]
    total_last_hour_telemetry = sum(
        len(recs)
        for recs in telemetry_store.values()
    )
    anomaly_rate = (
        len(last_hour) / total_last_hour_telemetry
        if total_last_hour_telemetry > 0
        else 0.0
    )

    # Most affected host
    host_counter: Counter = Counter(r.host_id for r in all_records)
    most_affected_host = host_counter.most_common(1)[0][0] if host_counter else "none"

    # Most common severity
    sev_counter: Counter = Counter(r.severity for r in all_records)
    most_common_severity = sev_counter.most_common(1)[0][0] if sev_counter else "none"

    # Worst feature overall
    feat_counter: Counter = Counter(r.worst_feature for r in all_records)
    worst_feature = feat_counter.most_common(1)[0][0] if feat_counter else "none"

    # Detection method breakdown
    method_counter: Counter = Counter(r.detection_method for r in all_records)
    breakdown = {
        "lstm+if": method_counter.get("lstm+if", 0),
        "lstm_only": method_counter.get("lstm_only", 0),
        "if_only": method_counter.get("if_only", 0),
        "none": method_counter.get("none", 0),
    }

    return AnomalyStatsResponse(
        total_anomalies_detected=total,
        anomaly_rate_last_hour=round(anomaly_rate, 6),
        most_affected_host=most_affected_host,
        most_common_severity=most_common_severity,
        worst_feature_overall=worst_feature,
        detection_breakdown=breakdown,
    )


# ─── Alerts sub-router ────────────────────────────────────────────────────────
# Mounted in main.py with prefix="/alerts" → exposes /alerts/recent, /alerts/stats

alerts_router = APIRouter()


@alerts_router.get(
    "/recent",
    response_model=AlertRecentResponse,
    summary="Fetch recent fired alerts",
)
async def get_recent_alerts(
    request: Request,
    host_id: Optional[str] = Query(None, description="Filter alerts to a single host"),
    severity: Optional[str] = Query(
        None,
        description="Filter by severity: critical / high / medium / low",
    ),
    limit: int = Query(50, ge=1, le=200, description="Max alerts to return [1, 200]"),
) -> AlertRecentResponse:
    """
    Return recent fired alerts from the AlertManager's in-memory store.

    Filters are optional and combined with AND semantics.  Results are
    returned newest-first and capped at ``limit``.

    Args:
        request:  Provides access to ``app.state.alert_manager``.
        host_id:  Optional host filter.
        severity: Optional severity filter.
        limit:    Maximum alerts in the response.

    Returns:
        AlertRecentResponse with matching alerts and applied filter summary.

    Raises:
        HTTPException 503: AlertManager not initialised.
        HTTPException 422: Invalid severity value.
    """
    alert_manager = getattr(request.app.state, "alert_manager", None)
    if alert_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AlertManager is not initialised.",
        )

    valid_severities = {"critical", "high", "medium", "low"}
    if severity and severity.lower() not in valid_severities:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"severity must be one of: {', '.join(sorted(valid_severities))}",
        )

    alerts = alert_manager.get_recent_alerts(
        host_id=host_id,
        severity=severity,
        limit=limit,
    )

    alert_schemas = [
        AlertSchema(
            alert_id=a.alert_id,
            host_id=a.host_id,
            severity=a.severity,
            combined_score=a.combined_score,
            worst_feature=a.worst_feature,
            top_contributing_features=a.top_contributing_features,
            message=a.message,
            timestamp=a.timestamp,
            acknowledged=a.acknowledged,
            resolved=a.resolved,
            resolution_timestamp=a.resolution_timestamp,
        )
        for a in alerts
    ]

    applied: dict[str, str] = {}
    if host_id:
        applied["host_id"] = host_id
    if severity:
        applied["severity"] = severity.lower()

    return AlertRecentResponse(
        alerts=alert_schemas,
        total_count=len(alert_schemas),
        filters_applied=applied,
    )


@alerts_router.put(
    "/{alert_id}/acknowledge",
    summary="Acknowledge a fired alert",
)
async def acknowledge_alert(
    alert_id: str,
    request: Request,
) -> dict:
    """
    Mark an alert as acknowledged by an operator.

    Args:
        alert_id: UUID of the alert to acknowledge.
        request:  Provides access to ``app.state.alert_manager``.

    Returns:
        JSON with ``alert_id`` and ``acknowledged: True``.

    Raises:
        HTTPException 503: AlertManager not initialised.
        HTTPException 404: Alert not found.
    """
    alert_manager = getattr(request.app.state, "alert_manager", None)
    if alert_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AlertManager is not initialised.",
        )

    found = alert_manager.acknowledge(alert_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id!r} not found.",
        )

    return {"alert_id": alert_id, "acknowledged": True}


@alerts_router.get(
    "/stats",
    response_model=AlertStatsSchema,
    summary="Aggregate alert statistics",
)
async def get_alert_stats(request: Request) -> AlertStatsSchema:
    """
    Return aggregate statistics from the AlertManager's in-memory history.

    Includes total fired and suppressed counts, per-severity and per-host
    breakdowns, the most-alerted host, and the most recent alert timestamp.

    Args:
        request: Provides access to ``app.state.alert_manager``.

    Returns:
        AlertStatsSchema with all computed statistics.

    Raises:
        HTTPException 503: AlertManager not initialised.
    """
    alert_manager = getattr(request.app.state, "alert_manager", None)
    if alert_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AlertManager is not initialised.",
        )

    stats = alert_manager.get_stats()

    return AlertStatsSchema(
        total_alerts_fired=stats.total_alerts_fired,
        alerts_suppressed=stats.alerts_suppressed,
        alerts_by_severity=stats.alerts_by_severity,
        alerts_by_host=stats.alerts_by_host,
        most_affected_host=stats.most_affected_host,
        last_alert_timestamp=stats.last_alert_timestamp,
    )
