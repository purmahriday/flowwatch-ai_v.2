"""
FlowWatch AI — LLM Assistant Route Handlers
============================================
Provides Claude-powered root cause analysis (RCA) and a conversational
follow-up interface for network anomaly investigation.

All Claude API logic lives in ``backend.assistant.rca_agent.RCAAgent``.
These route handlers are thin wrappers: they pull context from
``app.state``, delegate to the agent, and map the result to the
Pydantic response schema.

Endpoints
---------
POST /assistant/analyze
    Runs RCA on a detected anomaly.  Pulls recent telemetry for the host
    from the in-memory store (falls back to the request body if no records
    are cached).  Returns a full RCA with recommended actions and confidence.
    Falls back to rule-based recommendations if the Claude API fails.

POST /assistant/chat
    Stateless conversational endpoint.  The caller passes the full
    ``conversation_history`` from the previous response so context is
    preserved across requests without server-side session state.

Claude model: claude-sonnet-4-20250514
API key:      ANTHROPIC_API_KEY environment variable
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from loguru import logger

from backend.api.schemas import (
    AssistantAnalyzeRequest,
    AssistantAnalyzeResponse,
    AssistantChatRequest,
    AssistantChatResponse,
    ChatMessage,
)
from backend.assistant.rca_agent import RCAAgent

router = APIRouter()


# ─── POST /assistant/analyze ──────────────────────────────────────────────────


@router.post(
    "/analyze",
    response_model=AssistantAnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="LLM root cause analysis for a detected anomaly",
)
async def analyze_anomaly(
    body: AssistantAnalyzeRequest,
    request: Request,
) -> AssistantAnalyzeResponse:
    """
    Call the Claude API to perform root cause analysis on a network anomaly.

    Pulls the most recent telemetry for ``body.host_id`` from
    ``app.state.telemetry_store`` when available; otherwise uses the
    ``recent_telemetry`` list in the request body.

    Delegates all prompt construction, API calls, response parsing, and
    fallback logic to ``RCAAgent.analyze()``.  The endpoint never propagates
    API errors to the caller — it always returns a valid response.

    Args:
        body:    Host ID, anomaly result dict, recent telemetry, and question.
        request: FastAPI request (provides access to ``app.state`` stores).

    Returns:
        AssistantAnalyzeResponse with analysis text, recommended actions,
        confidence, token usage, and model metadata.

    Raises:
        HTTPException 503: ANTHROPIC_API_KEY is not configured.
    """
    # Pull stored telemetry for richer context; fall back to request body
    host_store: dict = getattr(request.app.state, "telemetry_store", {})
    stored = list(host_store.get(body.host_id, []))
    if stored:
        # Convert ProcessedRecord Pydantic objects to plain dicts
        recent_telemetry = [
            r.model_dump() if hasattr(r, "model_dump") else dict(r)
            for r in stored[-10:]
        ]
    else:
        recent_telemetry = body.recent_telemetry

    try:
        agent = RCAAgent()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    result = await agent.analyze(
        host_id=body.host_id,
        anomaly_result=body.anomaly_result,
        recent_telemetry=recent_telemetry,
        question=body.question,
    )

    logger.info(
        "RCA analysis returned | host={host} severity={sev} "
        "model={model} tokens={t} latency={l:.0f}ms",
        host=result.host_id,
        sev=result.severity,
        model=result.model_used,
        t=result.tokens_used,
        l=result.latency_ms,
    )

    return AssistantAnalyzeResponse(
        host_id=result.host_id,
        analysis=result.analysis,
        anomaly_severity=result.severity,
        recommended_actions=result.immediate_actions,
        confidence=result.confidence,
        analysis_timestamp=result.analysis_timestamp,
        model_used=result.model_used,
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

    The server is stateless — the caller must pass the ``conversation_history``
    returned by the previous ``/chat`` response to maintain context.
    ``RCAAgent.chat()`` trims history to the last 10 turns automatically.

    Args:
        body: Host ID, new message text, and prior conversation history.

    Returns:
        AssistantChatResponse with Claude's reply and the updated history.

    Raises:
        HTTPException 503: ANTHROPIC_API_KEY is not configured.
        HTTPException 502: Claude API returned an unrecoverable error.
    """
    try:
        agent = RCAAgent()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    # Convert Pydantic ChatMessage list to plain dicts for the agent
    history_dicts = [
        {"role": msg.role, "content": msg.content}
        for msg in body.conversation_history
    ]

    try:
        result = await agent.chat(
            message=body.message,
            conversation_history=history_dicts,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Chat agent error | host={host} err={err}",
            host=body.host_id,
            err=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Assistant error: {exc}",
        ) from exc

    updated_history = [
        ChatMessage(role=m["role"], content=m["content"])
        for m in result.conversation_history
    ]

    logger.debug(
        "Chat turn complete | host={host} history_len={n}",
        host=body.host_id,
        n=len(updated_history),
    )

    return AssistantChatResponse(
        host_id=body.host_id,
        response=result.response,
        conversation_history=updated_history,
    )
