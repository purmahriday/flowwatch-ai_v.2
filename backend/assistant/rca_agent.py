"""
FlowWatch AI — LLM Root Cause Analysis Agent
=============================================
Core agent that accepts anomaly context and recent telemetry, builds a
structured prompt, calls the Anthropic Claude API, and returns a parsed
``RCAResponse`` with per-section breakdowns.

Falls back to a rule-based ``RCAResponse`` if the Claude API is unavailable
so the analysis endpoint is always operable.

Classes
-------
RCAResponse   — Dataclass returned by ``RCAAgent.analyze()``
ChatResponse  — Dataclass returned by ``RCAAgent.chat()``
RCAAgent      — Main agent class (async)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import anthropic
from loguru import logger

# ─── Model configuration ──────────────────────────────────────────────────────

_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_DEFAULT_MAX_TOKENS = 800
_MAX_HISTORY_TURNS = 10

# System prompt enforces a 4-section structured response so the parser can
# reliably split "what is happening", "root cause", "actions", and "severity".
_ANALYZE_SYSTEM_PROMPT = (
    "You are a senior network operations engineer with 15 years experience at a major cloud provider. "
    "You are analyzing real-time network telemetry anomalies.\n\n"
    "When diagnosing anomalies follow these rules:\n"
    "- Latency spike only → consider: network congestion, routing loop, QoS issue, upstream provider problem\n"
    "- Packet loss only → consider: physical layer issue, firewall dropping packets, buffer overflow, link degradation\n"
    "- DNS failure only → consider: DNS server down, DNS poisoning attack, resolver misconfiguration, upstream DNS outage\n"
    "- Jitter only → consider: wireless interference, inconsistent routing, competing traffic, buffer bloat\n"
    "- CASCADE (all metrics) → consider: DDoS attack, major routing failure, ISP outage, hardware failure\n\n"
    "Always structure response EXACTLY as:\n"
    "1. What is happening:\n"
    "[2 sentences max — plain English summary]\n\n"
    "2. Root cause assessment:\n"
    "[2-3 sentences — most likely cause based on which specific metrics are anomalous]\n\n"
    "3. Immediate actions:\n"
    "- [specific actionable step with actual command or check if applicable]\n"
    "- [specific actionable step]\n"
    "- [specific actionable step]\n\n"
    "4. Severity justification:\n"
    "[1 sentence — why this severity level]\n\n"
    "Be specific. Reference actual metric values. Never be generic. "
    "If only latency is bad do NOT mention DNS."
)

_CHAT_SYSTEM_PROMPT = (
    "You are a senior network operations engineer helping investigate "
    "network anomalies. Be concise and technical. "
    "Answer questions based on the conversation history provided."
)

# Rule-based fallback actions keyed by severity level
_FALLBACK_ACTIONS: dict[str, list[str]] = {
    "critical": [
        "Immediately escalate to the on-call network engineer.",
        "Capture a packet trace on the affected host.",
        "Check upstream router and switch error counters.",
    ],
    "high": [
        "Schedule a network diagnostic within the next hour.",
        "Review host-level CPU, memory, and NIC statistics.",
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


# ─── Response dataclasses ─────────────────────────────────────────────────────


@dataclass
class RCAResponse:
    """
    Structured root cause analysis returned by ``RCAAgent.analyze()``.

    Attributes:
        host_id:               Target host identifier.
        analysis:              Full Claude response text (or fallback summary).
        severity:              Anomaly severity: critical / high / medium / low.
        what_is_happening:     Parsed section 1 — brief situation summary.
        root_cause:            Parsed section 2 — root cause assessment.
        immediate_actions:     Parsed section 3 — bullet-point action list.
        severity_justification: Parsed section 4 — one-line severity rationale.
        confidence:            Float in [0, 1] derived from combined anomaly score.
        model_used:            Claude model ID or ``"rule-based-fallback"``.
        analysis_timestamp:    UTC datetime when analysis was produced.
        tokens_used:           Total prompt + completion tokens consumed.
        latency_ms:            Wall-clock milliseconds for the Claude API call.
    """

    host_id: str
    analysis: str
    severity: str
    what_is_happening: str
    root_cause: str
    immediate_actions: list[str]
    severity_justification: str
    confidence: float
    model_used: str
    analysis_timestamp: datetime
    tokens_used: int
    latency_ms: float


@dataclass
class ChatResponse:
    """
    Response from ``RCAAgent.chat()``.

    Attributes:
        response:             Claude's reply text.
        conversation_history: Updated history list including the new turn,
                              each entry is ``{"role": str, "content": str}``.
    """

    response: str
    conversation_history: list[dict] = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _parse_sections(text: str) -> tuple[str, str, list[str], str]:
    """
    Split Claude's 4-section structured response into discrete components.

    Expects numbered section headings (``1.``, ``2.``, ``3.``, ``4.``).
    Degrades gracefully when the response does not perfectly follow the
    template.

    Args:
        text: Raw Claude response text.

    Returns:
        Tuple of (what_is_happening, root_cause, immediate_actions, severity_justification).
    """
    # Find every "N. " section heading and record its position + number
    heading_re = re.compile(r"^\s*(\d+)\.\s+", re.MULTILINE)
    matches = [(m.start(), int(m.group(1))) for m in heading_re.finditer(text)]

    sections: dict[int, str] = {}
    for idx, (pos, num) in enumerate(matches):
        end = matches[idx + 1][0] if idx + 1 < len(matches) else len(text)
        raw = text[pos:end]
        # Strip the "N. " prefix from the start of this slice
        content = heading_re.sub("", raw, count=1).strip()
        sections[num] = content

    what_is_happening = sections.get(1, text[:300].strip())
    root_cause = sections.get(2, "Root cause analysis not available.")
    actions_text = sections.get(3, "")
    severity_just = sections.get(4, "Severity determined by anomaly score.")

    # Extract bullet points (•, -, *) from section 3
    bullet_re = re.compile(r"^\s*[-•*]\s+(.+?)$", re.MULTILINE)
    actions = [m.group(1).strip() for m in bullet_re.finditer(actions_text)]

    if not actions:
        # Try numbered items inside section 3
        num_re = re.compile(r"^\s*\d+\.\s+(.+?)$", re.MULTILINE)
        actions = [m.group(1).strip() for m in num_re.finditer(actions_text)]

    if not actions and actions_text:
        # Last resort: non-empty lines
        actions = [ln.strip() for ln in actions_text.splitlines() if ln.strip()]

    return what_is_happening, root_cause, actions, severity_just


def _build_analyze_prompt(
    host_id: str,
    anomaly_result: dict,
    recent_telemetry: list[dict],
    question: Optional[str],
) -> str:
    """
    Construct the user-facing prompt for the RCA analysis call.

    Includes host metadata, combined + per-model anomaly scores, the top
    contributing features, and a compact table of the last 5 telemetry records
    to give Claude temporal trend context.

    Args:
        host_id:          Target host identifier.
        anomaly_result:   CombinedAnomalyResult serialised as a dict.
        recent_telemetry: Recent ProcessedRecord dicts (last 5 used).
        question:         Optional natural-language operator question.

    Returns:
        Formatted prompt string.
    """
    severity = anomaly_result.get("severity", "unknown")
    combined_score = anomaly_result.get("combined_score", 0.0)
    detection_method = anomaly_result.get("detection_method", "unknown")
    worst_feature = anomaly_result.get("worst_feature", "unknown")
    top_features = anomaly_result.get("top_contributing_features", [])
    timestamp = anomaly_result.get("timestamp", "unknown")

    lstm = anomaly_result.get("lstm_result", {})
    lstm_score = lstm.get("anomaly_score", 0.0)
    per_feature_errors = lstm.get("per_feature_errors", {})

    if_result = anomaly_result.get("if_result", {})
    if_score = if_result.get("anomaly_score", 0.0)

    # Compact telemetry table — last 5 records, oldest → newest
    rows: list[str] = []
    for i, rec in enumerate(recent_telemetry[-5:], 1):
        ts = str(rec.get("timestamp", "unknown"))[-8:]
        rows.append(
            f"  [{i}] t={ts} "
            f"lat={rec.get('latency_ms', 0):.1f}ms "
            f"loss={rec.get('packet_loss_pct', 0):.1f}% "
            f"dns={rec.get('dns_failure_rate', 0):.3f} "
            f"jitter={rec.get('jitter_ms', 0):.1f}ms "
            f"health={rec.get('composite_health_score', 0):.3f}"
        )
    telemetry_block = "\n".join(rows) if rows else "  (no recent records)"

    feat_block = "\n".join(
        f"  {feat}: {err:.6f}"
        for feat, err in sorted(per_feature_errors.items(), key=lambda x: x[1], reverse=True)
    ) if per_feature_errors else "  (not available)"

    q = question or "What is causing this anomaly and what should I do?"

    return f"""NETWORK ANOMALY REPORT
======================
Host:             {host_id}
Timestamp:        {timestamp}
Severity:         {severity.upper()}
Combined Score:   {combined_score:.4f}  (LSTM×0.6={lstm_score:.3f}  IF×0.4={if_score:.3f})
Detection Method: {detection_method}
Worst Feature:    {worst_feature}
Top IF Features:  {', '.join(top_features) if top_features else 'n/a'}

LSTM Per-Feature Reconstruction Errors:
{feat_block}

Recent Telemetry (oldest → newest):
{telemetry_block}

OPERATOR QUESTION: {q}
"""


def _rule_based_response(
    host_id: str,
    anomaly_result: dict,
    analysis_ts: datetime,
) -> RCAResponse:
    """
    Construct a rule-based ``RCAResponse`` used when the Claude API is
    unavailable.

    Args:
        host_id:       Target host identifier.
        anomaly_result: Anomaly context dict.
        analysis_ts:   Timestamp to stamp the response with.

    Returns:
        RCAResponse populated with deterministic rule-based content.
    """
    severity = anomaly_result.get("severity", "low")
    worst = anomaly_result.get("worst_feature", "unknown")
    combined = anomaly_result.get("combined_score", 0.0)
    actions = _FALLBACK_ACTIONS.get(severity.lower(), _FALLBACK_ACTIONS["low"])
    summary = (
        f"[Rule-based fallback] Anomaly detected on {host_id} "
        f"with severity '{severity}' (score {combined:.3f}). "
        f"Worst performing feature: {worst}."
    )
    return RCAResponse(
        host_id=host_id,
        analysis=summary,
        severity=severity,
        what_is_happening=f"Anomaly detected on {host_id} with severity '{severity}'.",
        root_cause=(
            f"The ensemble detector flagged this event (score {combined:.3f}). "
            f"The worst-performing feature was '{worst}'. "
            "Claude API unavailable for deep analysis."
        ),
        immediate_actions=actions,
        severity_justification=f"Severity '{severity}' assigned by ML ensemble score.",
        confidence=round(min(combined, 1.0), 3),
        model_used="rule-based-fallback",
        analysis_timestamp=analysis_ts,
        tokens_used=0,
        latency_ms=0.0,
    )


# ─── RCAAgent ─────────────────────────────────────────────────────────────────


class RCAAgent:
    """
    LLM-powered root cause analysis agent using the Anthropic Claude API.

    Attributes:
        model:      Claude model ID used for all inference calls.
        max_tokens: Maximum completion tokens per API call.
    """

    def __init__(self) -> None:
        """
        Initialise the agent.

        Reads ``ANTHROPIC_API_KEY`` from the environment.  Raises
        ``ValueError`` if the key is absent so mis-configuration is caught
        early rather than at first inference call.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Copy .env.example to .env and fill in the key."
            )
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model: str = _DEFAULT_MODEL
        self.max_tokens: int = _DEFAULT_MAX_TOKENS

    # ── Public interface ──────────────────────────────────────────────────────

    async def analyze(
        self,
        host_id: str,
        anomaly_result: dict,
        recent_telemetry: list[dict],
        question: Optional[str] = None,
    ) -> RCAResponse:
        """
        Perform root cause analysis on a detected anomaly using Claude.

        Builds a structured prompt from the anomaly context and recent
        telemetry trend, calls the Claude API, and parses the 4-section
        structured response into an ``RCAResponse``.

        Falls back to ``_rule_based_response()`` if the API call fails for
        any reason — this endpoint must never propagate infrastructure errors.

        Args:
            host_id:          Target host identifier, e.g. ``host-01``.
            anomaly_result:   ``CombinedAnomalyResult`` serialised as a dict
                              (e.g. via ``dataclasses.asdict``).
            recent_telemetry: Recent ``ProcessedRecord`` dicts for context.
            question:         Optional natural-language operator question.
                              Defaults to a standard triage question.

        Returns:
            RCAResponse with parsed sections, confidence, and token metadata.
        """
        analysis_ts = datetime.now(timezone.utc)
        t0 = time.monotonic()

        try:
            prompt = _build_analyze_prompt(
                host_id=host_id,
                anomaly_result=anomaly_result,
                recent_telemetry=recent_telemetry,
                question=question,
            )

            message = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_ANALYZE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            latency_ms = (time.monotonic() - t0) * 1000.0
            analysis_text: str = message.content[0].text
            tokens_used: int = message.usage.input_tokens + message.usage.output_tokens

            what, root, actions, sev_just = _parse_sections(analysis_text)
            severity = anomaly_result.get("severity", "unknown")
            combined_score = anomaly_result.get("combined_score", 0.5)

            logger.info(
                "RCA complete | host={h} severity={s} tokens={t} latency={l:.0f}ms",
                h=host_id,
                s=severity,
                t=tokens_used,
                l=latency_ms,
            )

            return RCAResponse(
                host_id=host_id,
                analysis=analysis_text,
                severity=severity,
                what_is_happening=what,
                root_cause=root,
                immediate_actions=actions,
                severity_justification=sev_just,
                confidence=round(min(combined_score, 1.0), 3),
                model_used=self.model,
                analysis_timestamp=analysis_ts,
                tokens_used=tokens_used,
                latency_ms=round(latency_ms, 2),
            )

        except anthropic.APIStatusError as exc:
            logger.warning(
                "Claude API error — rule-based fallback | host={h} status={s}",
                h=host_id,
                s=exc.status_code,
            )
        except anthropic.APIConnectionError:
            logger.warning(
                "Claude API unreachable — rule-based fallback | host={h}", h=host_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unexpected agent error — rule-based fallback | host={h} err={e}",
                h=host_id,
                e=exc,
            )

        return _rule_based_response(host_id, anomaly_result, analysis_ts)

    async def chat(
        self,
        message: str,
        conversation_history: list[dict],
        host_context: Optional[dict] = None,
    ) -> ChatResponse:
        """
        Send a message to Claude with prior conversation history context.

        Maintains up to the last ``_MAX_HISTORY_TURNS`` turns.  The caller
        owns history state and must pass the ``conversation_history`` from the
        previous ``ChatResponse`` to preserve continuity across calls.

        Args:
            message:              New user message text.
            conversation_history: Prior turns as ``[{"role": str, "content": str}]``.
            host_context:         Optional host metadata dict to include in the
                                  system prompt for grounding (e.g. recent anomaly
                                  or telemetry snapshot).

        Returns:
            ChatResponse with Claude's reply and updated conversation history.

        Raises:
            anthropic.APIStatusError:    Claude returned an API-level error.
            anthropic.APIConnectionError: Could not reach the Claude API.
        """
        system_prompt = _CHAT_SYSTEM_PROMPT
        if host_context:
            ctx_lines = "\n".join(f"  {k}: {v}" for k, v in host_context.items())
            system_prompt = (
                f"{_CHAT_SYSTEM_PROMPT}\n\n"
                f"Current host context:\n{ctx_lines}"
            )

        # Trim to last N turns then append the new user message
        trimmed = list(conversation_history[-_MAX_HISTORY_TURNS:])
        trimmed.append({"role": "user", "content": message})

        api_messages = [{"role": m["role"], "content": m["content"]} for m in trimmed]

        response_msg = await self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=api_messages,
        )
        reply: str = response_msg.content[0].text

        updated_history = trimmed + [{"role": "assistant", "content": reply}]

        logger.debug(
            "Chat turn complete | history_len={n}", n=len(updated_history)
        )

        return ChatResponse(response=reply, conversation_history=updated_history)

    async def batch_analyze(
        self,
        anomalies: list[dict],
    ) -> list[RCAResponse]:
        """
        Analyze up to 5 anomalies concurrently using ``asyncio.gather``.

        Each dict in ``anomalies`` must contain the keyword arguments accepted
        by ``analyze()``: ``host_id``, ``anomaly_result``, ``recent_telemetry``,
        and optionally ``question``.

        Args:
            anomalies: List of anomaly context dicts (max 5 processed).

        Returns:
            List of ``RCAResponse`` objects in the same order as the input.
            Anomalies beyond index 4 are silently ignored.
        """
        batch = anomalies[:5]  # cap at 5 concurrent requests
        tasks = [
            self.analyze(
                host_id=item["host_id"],
                anomaly_result=item["anomaly_result"],
                recent_telemetry=item.get("recent_telemetry", []),
                question=item.get("question"),
            )
            for item in batch
        ]
        results: list[RCAResponse] = await asyncio.gather(*tasks)
        return list(results)
