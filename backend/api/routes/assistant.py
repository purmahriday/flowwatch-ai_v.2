"""
FlowWatch AI — LLM Assistant Route Handlers
============================================
Provides Claude-powered root cause analysis (RCA) and a conversational
follow-up interface for network anomaly investigation.

Endpoints
---------
POST /assistant/analyze
    Calls the Claude API with structured anomaly context and recent telemetry.
    Returns a root cause analysis, recommended actions, and confidence score.
    Falls back to rule-based recommendations if the API call fails.

POST /assistant/chat
    Stateless conversational endpoint that accepts a message plus conversation
    history (last 10 turns) and returns Claude's next reply.

Claude model: claude-sonnet-4-20250514
API key:      ANTHROPIC_API_KEY environment variable
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, HTTPException, status
from loguru import logger

from backend.api.schemas import (
    AssistantAnalyzeRequest,
    AssistantAnalyzeResponse,
    AssistantChatRequest,
    AssistantChatResponse,
    ChatMessage,
)

router = APIRouter()

# ─── Model configuration ──────────────────────────────────────────────────────

_CLAUDE_MODEL = "claude-sonnet-4-20250514"
_MAX_TOKENS = 500
_MAX_HISTORY_TURNS = 10  # kept per conversation context

# ─── System prompts ───────────────────────────────────────────────────────────

_ANALYZE_SYSTEM_PROMPT = (
    "You are a network operations expert analyzing real-time telemetry data. "
    "Be concise, technical, and actionable. "
    "When you recommend actions, format them as a numbered list prefixed with 'ACTION:'. "
    "Keep your total response under 400 words."
)

_CHAT_SYSTEM_PROMPT = (
    "You are a network operations expert helping investigate network anomalies. "
    "Be concise and technical. "
    "Answer questions based on the conversation history provided."
)

# ─── Rule-based fallback actions ─────────────────────────────────────────────

_FALLBACK_ACTIONS: dict[str, list[str]] = {
    "critical": [
        "Immediately escalate to the on-call network engineer.",
        "Capture a packet trace on the affected host.",
        "Check upstream router and switch error counters.",
        "Review recent configuration changes in the change management system.",
        "Activate incident response runbook for network outage.",
    ],
    "high": [
        "Schedule a network diagnostic within the next hour.",
        "Review host-level CPU, memory, and NIC statistics.",
        "Check for recent firmware or driver updates on the host.",
        "Verify BGP/OSPF routing table integrity.",
    ],
    "medium": [
        "Monitor the host closely for the next 30 minutes.",
        "Check interface error counters (CRC, drops, resets).",
        "Review application-level connection pool exhaustion.",
    ],
    "low": [
        "Log the anomaly for trend analysis.",
        "Continue standard monitoring procedures.",
        "Flag for review at the next operations meeting.",
    ],
}


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _get_client() -> anthropic.AsyncAnthropic:
    """Instantiate an AsyncAnthropic client from the environment API key."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANTHROPIC_API_KEY is not configured.",
        )
    return anthropic.AsyncAnthropic(api_key=api_key)


def _build_analyze_prompt(
    host_id: str,
    anomaly_result: dict,
    recent_telemetry: list[dict],
    question: str,
) -> str:
    """
    Build the user-facing prompt for the RCA analysis call.

    Includes host metadata, anomaly scores, feature breakdown, and a
    summary of the last 10 telemetry records for temporal context.

    Args:
        host_id:         Target host identifier.
        anomaly_result:  CombinedAnomalyResult as a dict.
        recent_telemetry: Last 10 ProcessedRecords as dicts.
        question:        The operator's natural-language question.

    Returns:
        Formatted prompt string.
    """
    severity = anomaly_result.get("severity", "unknown")
    combined_score = anomaly_result.get("combined_score", 0.0)
    detection_method = anomaly_result.get("detection_method", "unknown")
    worst_feature = anomaly_result.get("worst_feature", "unknown")
    top_features = anomaly_result.get("top_contributing_features", [])

    lstm = anomaly_result.get("lstm_result", {})
    per_feature_errors = lstm.get("per_feature_errors", {})

    # Format recent telemetry as a compact table
    telemetry_lines = []
    for i, rec in enumerate(recent_telemetry[-10:], 1):
        ts = rec.get("timestamp", "unknown")[-8:]  # HH:MM:SS suffix
        telemetry_lines.append(
            f"  [{i:2d}] t={ts} "
            f"latency={rec.get('latency_ms', 0):.1f}ms "
            f"loss={rec.get('packet_loss_pct', 0):.1f}% "
            f"dns_fail={rec.get('dns_failure_rate', 0):.3f} "
            f"jitter={rec.get('jitter_ms', 0):.1f}ms "
            f"health={rec.get('composite_health_score', 0):.3f}"
        )

    telemetry_block = "\n".join(telemetry_lines) if telemetry_lines else "  (no recent records)"

    feature_error_block = "\n".join(
        f"  {feat}: {err:.6f}"
        for feat, err in sorted(per_feature_errors.items(), key=lambda x: x[1], reverse=True)
    ) if per_feature_errors else "  (not available)"

    prompt = f"""NETWORK ANOMALY REPORT
======================
Host:             {host_id}
Severity:         {severity.upper()}
Combined Score:   {combined_score:.4f} (LSTM×0.6 + IF×0.4)
Detection Method: {detection_method}
Worst Feature:    {worst_feature}
Top IF Features:  {', '.join(top_features)}

LSTM Per-Feature Reconstruction Errors:
{feature_error_block}

Recent Telemetry (last 10 records, oldest→newest):
{telemetry_block}

QUESTION: {question}

Please provide:
1. Your assessment of the root cause.
2. Numbered ACTION items (prefix each with "ACTION:").
3. Your confidence level (0–100%).
"""
    return prompt


def _parse_actions(response_text: str) -> list[str]:
    """
    Extract ACTION items from Claude's response text.

    Looks for lines prefixed with ``ACTION:`` (case-insensitive).
    Falls back to extracting numbered list items if no explicit ACTION tags.

    Args:
        response_text: Raw text returned by Claude.

    Returns:
        List of action strings (may be empty).
    """
    # Primary: explicit ACTION: tags
    actions = re.findall(r"ACTION:\s*(.+?)(?=\n|$)", response_text, re.IGNORECASE)
    if actions:
        return [a.strip() for a in actions if a.strip()]

    # Fallback: numbered list items (e.g. "1. Do something")
    actions = re.findall(r"^\s*\d+\.\s+(.+?)$", response_text, re.MULTILINE)
    return [a.strip() for a in actions if a.strip()]


def _parse_confidence(response_text: str) -> float:
    """
    Extract a confidence percentage from Claude's response.

    Looks for patterns like "confidence: 85%" or "85% confident".
    Returns 0.5 if none found.

    Args:
        response_text: Raw Claude response text.

    Returns:
        Confidence as a float in [0.0, 1.0].
    """
    match = re.search(r"(\d{1,3})\s*%", response_text)
    if match:
        pct = int(match.group(1))
        return round(min(max(pct, 0), 100) / 100.0, 3)
    return 0.5


def _fallback_actions(severity: str) -> list[str]:
    """Return rule-based fallback actions for the given severity."""
    return _FALLBACK_ACTIONS.get(severity.lower(), _FALLBACK_ACTIONS["low"])


# ─── POST /assistant/analyze ──────────────────────────────────────────────────


@router.post(
    "/analyze",
    response_model=AssistantAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="LLM root cause analysis for a detected anomaly",
)
async def analyze_anomaly(body: AssistantAnalyzeRequest) -> AssistantAnalyzeResponse:
    """
    Call the Claude API to perform root cause analysis on a network anomaly.

    Constructs a structured prompt from the anomaly result and recent telemetry
    context, then calls ``claude-sonnet-4-20250514`` with a 500-token budget.
    Parses recommended actions and confidence from the response.

    If the Claude API call fails for any reason, returns a rule-based fallback
    analysis based on the anomaly severity — the endpoint never propagates API
    errors to the caller.

    Args:
        body: Host ID, anomaly result dict, recent telemetry, and question.

    Returns:
        AssistantAnalyzeResponse with analysis text, recommended actions,
        confidence, and model metadata.
    """
    severity = body.anomaly_result.get("severity", "unknown")
    analysis_ts = datetime.now(timezone.utc)

    try:
        client = _get_client()
        prompt = _build_analyze_prompt(
            host_id=body.host_id,
            anomaly_result=body.anomaly_result,
            recent_telemetry=body.recent_telemetry,
            question=body.question,
        )

        message = await client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_ANALYZE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        analysis_text: str = message.content[0].text
        actions = _parse_actions(analysis_text)
        confidence = _parse_confidence(analysis_text)

        # If Claude didn't provide explicit actions, use rule-based fallback
        if not actions:
            actions = _fallback_actions(severity)

        logger.info(
            "RCA analysis complete | host={host} severity={sev} "
            "confidence={conf:.0%} actions={n_actions}",
            host=body.host_id,
            sev=severity,
            conf=confidence,
            n_actions=len(actions),
        )

        return AssistantAnalyzeResponse(
            host_id=body.host_id,
            analysis=analysis_text,
            anomaly_severity=severity,
            recommended_actions=actions,
            confidence=confidence,
            analysis_timestamp=analysis_ts,
            model_used=_CLAUDE_MODEL,
        )

    except HTTPException:
        raise  # Re-raise API key config errors
    except anthropic.APIStatusError as exc:
        logger.warning(
            "Claude API error — using rule-based fallback | host={host} "
            "status={s} message={m}",
            host=body.host_id,
            s=exc.status_code,
            m=str(exc.message),
        )
    except anthropic.APIConnectionError:
        logger.warning(
            "Claude API unreachable — using rule-based fallback | host={host}",
            host=body.host_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unexpected assistant error — using rule-based fallback | "
            "host={host} error={err}",
            host=body.host_id,
            err=exc,
        )

    # ── Rule-based fallback ───────────────────────────────────────────────────
    actions = _fallback_actions(severity)
    fallback_summary = (
        f"Claude API unavailable. Rule-based analysis for severity '{severity}': "
        f"The anomaly was detected via the ensemble detector. "
        f"The worst-performing feature was "
        f"'{body.anomaly_result.get('worst_feature', 'unknown')}'. "
        f"Recommended actions are based on severity classification."
    )

    return AssistantAnalyzeResponse(
        host_id=body.host_id,
        analysis=fallback_summary,
        anomaly_severity=severity,
        recommended_actions=actions,
        confidence=0.4,  # Low confidence for rule-based fallback
        analysis_timestamp=analysis_ts,
        model_used="rule-based-fallback",
    )


# ─── POST /assistant/chat ─────────────────────────────────────────────────────


@router.post(
    "/chat",
    response_model=AssistantChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Conversational follow-up with the network assistant",
)
async def chat_with_assistant(body: AssistantChatRequest) -> AssistantChatResponse:
    """
    Send a message to the Claude assistant with conversation history context.

    Maintains up to the last ``_MAX_HISTORY_TURNS`` message turns.  The caller
    is responsible for passing the ``conversation_history`` returned by the
    previous call so context is preserved across requests (stateless server).

    Args:
        body: Host ID, new message, and prior conversation history.

    Returns:
        AssistantChatResponse with Claude's reply and the updated history.

    Raises:
        HTTPException 503: ANTHROPIC_API_KEY not configured.
        HTTPException 502: Claude API returned an unexpected error.
    """
    client = _get_client()

    # Trim history to last N turns, then append the new user message
    trimmed_history = list(body.conversation_history[-_MAX_HISTORY_TURNS:])
    trimmed_history.append(ChatMessage(role="user", content=body.message))

    # Build Anthropic-compatible message list
    api_messages = [
        {"role": msg.role, "content": msg.content}
        for msg in trimmed_history
    ]

    try:
        message = await client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_CHAT_SYSTEM_PROMPT,
            messages=api_messages,
        )
        reply: str = message.content[0].text

    except anthropic.APIStatusError as exc:
        logger.error(
            "Claude API error in chat | host={host} status={s}",
            host=body.host_id,
            s=exc.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Claude API error: {exc.message}",
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach the Claude API. Check connectivity.",
        ) from exc

    # Append assistant reply to history
    updated_history = trimmed_history + [ChatMessage(role="assistant", content=reply)]

    logger.debug(
        "Chat turn complete | host={host} history_len={n}",
        host=body.host_id,
        n=len(updated_history),
    )

    return AssistantChatResponse(
        host_id=body.host_id,
        response=reply,
        conversation_history=updated_history,
    )
