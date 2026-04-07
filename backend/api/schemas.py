"""
FlowWatch AI — Pydantic Request/Response Schemas
=================================================
Centralised schema definitions for all API endpoints.

All request bodies, response models, and shared sub-schemas live here so
that route modules stay thin and schemas remain testable in isolation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ─── Shared model result sub-schemas ─────────────────────────────────────────


class LSTMResultSchema(BaseModel):
    """LSTM autoencoder inference result."""

    is_anomaly: bool
    anomaly_score: float
    reconstruction_error: float
    threshold_used: float
    per_feature_errors: dict[str, float]
    worst_feature: str
    inference_time_ms: float
    model_version: str


class IFResultSchema(BaseModel):
    """Isolation Forest inference result."""

    is_anomaly: bool
    anomaly_score: float
    raw_score: float
    confidence: float
    top_contributing_features: list[str]
    host_id: str
    timestamp: str
    model_version: str
    inference_time_ms: float


class CombinedAnomalyResultSchema(BaseModel):
    """Ensemble anomaly result fusing LSTM (60%) and Isolation Forest (40%)."""

    is_anomaly: bool
    combined_score: float
    severity: str
    lstm_result: LSTMResultSchema
    if_result: IFResultSchema
    detection_method: str
    worst_feature: str
    top_contributing_features: list[str]
    timestamp: datetime


# ─── Telemetry schemas ────────────────────────────────────────────────────────


class TelemetryIngestRequest(BaseModel):
    """
    Raw telemetry payload submitted by a network agent or simulator.

    All numeric fields are range-validated at the Pydantic layer so that
    downstream ML code can assume clean, bounded inputs.
    """

    host_id: str = Field(description="Host identifier, e.g. host-01")
    latency_ms: float = Field(ge=0.0, le=1000.0, description="Round-trip latency in ms [0, 1000]")
    packet_loss_pct: float = Field(ge=0.0, le=100.0, description="Packet loss percentage [0, 100]")
    dns_failure_rate: float = Field(ge=0.0, le=1.0, description="DNS failure fraction [0, 1]")
    jitter_ms: float = Field(ge=0.0, le=500.0, description="Network jitter in ms [0, 500]")
    timestamp: Optional[datetime] = Field(
        None, description="Record time (UTC). Defaults to now if omitted."
    )

    @field_validator("host_id")
    @classmethod
    def validate_host_id_pattern(cls, v: str) -> str:
        if not re.match(r"^host-\d{2}$", v):
            raise ValueError(
                "host_id must match pattern host-XX where XX is two digits "
                "(e.g. host-01, host-12)"
            )
        return v


class TelemetryIngestResponse(BaseModel):
    """Result returned after a successful telemetry ingest call."""

    record_id: str = Field(description="UUID4 assigned to this record")
    host_id: str
    processed: bool = Field(description="True when preprocessing succeeded")
    anomaly_detected: bool = Field(description="False when the window is not yet full")
    anomaly_result: Optional[CombinedAnomalyResultSchema] = None
    window_ready: bool = Field(description="True once 30 records are buffered for this host")
    message: str


class HostStatusResponse(BaseModel):
    """Current health snapshot for a single monitored host."""

    host_id: str
    record_count: int
    latest_health_score: float
    latest_latency_ms: float
    window_ready: bool
    last_seen: str = Field(description="ISO 8601 UTC timestamp of the most recent record")


class TelemetryRecentResponse(BaseModel):
    """Paginated recent telemetry records from the in-memory store."""

    records: list[dict]
    total_count: int
    hosts_included: list[str]
    time_range_minutes: int


# ─── Anomaly schemas ──────────────────────────────────────────────────────────


class AnomalyDetectRequest(BaseModel):
    """
    On-demand anomaly detection request.

    ``feature_vector`` should be the dict produced by
    ``FeatureVector.to_dict()`` — i.e. it must contain all 19 scalar feature
    fields plus ``host_id`` and ``timestamp``.
    """

    host_id: str
    feature_vector: dict = Field(
        description="Dict from FeatureVector.to_dict() — must contain all 19 feature scalars"
    )


class AnomalyDetectResponse(BaseModel):
    """Result of an on-demand anomaly detection call."""

    host_id: str
    detection_timestamp: datetime
    recommendation: str
    result: CombinedAnomalyResultSchema


class AnomalyRecord(BaseModel):
    """
    A persisted anomaly event stored in the in-memory anomaly store.

    Created both by the ingest pipeline (automatic) and the on-demand
    /anomalies/detect endpoint.
    """

    record_id: str
    host_id: str
    detected_at: datetime
    severity: str
    combined_score: float
    is_anomaly: bool
    detection_method: str
    worst_feature: str
    top_contributing_features: list[str]
    lstm_result: LSTMResultSchema
    if_result: IFResultSchema
    anomaly_timestamp: datetime
    recommendation: str


class AnomalyListResponse(BaseModel):
    """Response for GET /anomalies/latest."""

    anomalies: list[AnomalyRecord]
    total_count: int
    critical_count: int
    high_count: int
    summary_by_host: dict[str, int]


class AnomalyStatsResponse(BaseModel):
    """Aggregate anomaly statistics across all hosts."""

    total_anomalies_detected: int
    anomaly_rate_last_hour: float
    most_affected_host: str
    most_common_severity: str
    worst_feature_overall: str
    detection_breakdown: dict[str, int]


# ─── Assistant schemas ────────────────────────────────────────────────────────


class AssistantAnalyzeRequest(BaseModel):
    """Request body for the LLM root-cause analysis endpoint."""

    host_id: str
    anomaly_result: dict = Field(
        description="CombinedAnomalyResult serialised as a dict (e.g. via dataclasses.asdict)"
    )
    recent_telemetry: list[dict] = Field(
        description="Last 10 ProcessedRecord dicts for context"
    )
    question: str = Field(
        default="What is causing this anomaly and what should I do?",
        description="Natural-language question for the assistant",
    )


class AssistantAnalyzeResponse(BaseModel):
    """Response from the LLM root-cause analysis call."""

    host_id: str
    analysis: str = Field(description="Full Claude response text")
    anomaly_severity: str
    recommended_actions: list[str]
    confidence: float
    analysis_timestamp: datetime
    model_used: str


class ChatMessage(BaseModel):
    """A single turn in a multi-turn conversation."""

    role: str = Field(description="'user' or 'assistant'")
    content: str


class AssistantChatRequest(BaseModel):
    """Conversational follow-up request with history."""

    host_id: str
    message: str
    conversation_history: list[ChatMessage] = Field(
        default_factory=list,
        description="Previous conversation turns (last 10 will be included)",
    )


class AssistantChatResponse(BaseModel):
    """Response from the conversational assistant with updated history."""

    host_id: str
    response: str
    conversation_history: list[ChatMessage]
