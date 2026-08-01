"""One-call LLM provider boundary for checkpointed Runtime nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
import uuid

from app.services.token_tracker import TokenUsage, record_token_usage

from .caller import (
    AgentLLMInvocation,
    _convert_messages_for_vision,
    _get_model_timeout,
    _sanitize_tool_calls_for_context,
    _usage_from_response_or_estimate,
    get_llm_request_options,
    record_agent_llm_invocation_failure,
    release_llm_round_credits,
    reserve_llm_round_credits,
    settle_agent_llm_invocation,
    settle_llm_round_credits,
)
from .client import (
    LLMMessage,
    extract_embedded_reasoning,
    llm_provider_may_have_accepted,
    normalize_llm_finish_reason,
    normalize_textual_tool_protocol,
)
from .failover import (
    is_auth_error,
    is_billing_or_quota_error,
    is_rate_limit_error,
)
from .utils import create_llm_client, get_max_tokens, get_model_api_key

if TYPE_CHECKING:
    from app.models.llm import LLMModel


@dataclass(frozen=True, slots=True)
class LLMCompletionStep:
    """One normalized provider response with no tool or lifecycle side effects."""

    content: str | None
    tool_calls: tuple[dict, ...]
    reasoning_content: str | None
    retry_instruction: str | None
    usage: TokenUsage
    retry_tool_name: str | None = None
    finish_reason: str | None = None


async def complete_llm_once(
    model: LLMModel,
    messages: list[LLMMessage],
    *,
    tools: list[dict] | None = None,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    supports_vision: bool = False,
    invocation: AgentLLMInvocation | None = None,
) -> LLMCompletionStep:
    """Call one pinned model exactly once and normalize its tool proposals.

    This function never executes tools, retries, appends repair prompts, or
    advances a lifecycle. Those decisions belong to the durable Graph.
    """
    api_messages = _convert_messages_for_vision(messages, supports_vision)
    effective_model = invocation.model if invocation is not None else model
    max_tokens = get_max_tokens(
        effective_model.provider,
        effective_model.model,
        getattr(effective_model, "max_output_tokens", None),
    )
    client = create_llm_client(
        provider=effective_model.provider,
        api_key=(invocation.api_key if invocation is not None else get_model_api_key(effective_model)),
        model=effective_model.model,
        base_url=(invocation.base_url if invocation is not None else effective_model.base_url),
        timeout=_get_model_timeout(effective_model),
    )
    reservation_id = None
    try:
        if invocation is not None:
            reservation_id = await reserve_llm_round_credits(
                tenant_id=invocation.tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                model=effective_model,
                route_meta=invocation.route_meta,
                messages=api_messages,
                tools=tools or None,
                max_tokens=max_tokens,
            )
        response = await client.complete(
            messages=api_messages,
            tools=tools or None,
            temperature=effective_model.temperature,
            max_tokens=max_tokens,
            **get_llm_request_options(effective_model),
        )
    except BaseException as exc:
        provider_outcome_ambiguous = llm_provider_may_have_accepted(client)
        if isinstance(exc, Exception):
            # A transient error after request start is not replay-safe. Only
            # explicit account/capacity rejection may switch provider now.
            exc.provider_outcome_ambiguous = provider_outcome_ambiguous  # type: ignore[attr-defined]
            exc.route_failover_safe = (  # type: ignore[attr-defined]
                not provider_outcome_ambiguous
                and (
                    is_auth_error(exc)
                    or is_billing_or_quota_error(exc)
                    or is_rate_limit_error(exc)
                )
            )
        if invocation is not None:
            await release_llm_round_credits(
                reservation_id,
                model=effective_model,
                route_meta=invocation.route_meta,
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=invocation.tenant_id,
                provider_failed=not provider_outcome_ambiguous,
            )
            if isinstance(exc, Exception):
                await record_agent_llm_invocation_failure(
                    invocation,
                    exc,
                    agent_id=agent_id,
                    user_id=user_id,
                )
        raise
    finally:
        await client.close()

    usage = _usage_from_response_or_estimate(response, api_messages)
    if invocation is not None:
        await settle_llm_round_credits(
            reservation_id,
            usage=usage,
            model=effective_model,
            route_meta=invocation.route_meta,
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=invocation.tenant_id,
        )
        if agent_id is not None:
            await settle_agent_llm_invocation(
                invocation,
                agent_id=agent_id,
                user_id=user_id,
                usage=usage,
            )
    if agent_id is not None and usage.total_tokens > 0:
        await record_token_usage(agent_id, usage)

    content, reasoning_content = extract_embedded_reasoning(
        response.content,
        response.reasoning_content,
    )
    textual_tool_calls: list[dict] = []
    textual_retry_instruction = None
    if not response.tool_calls:
        content, textual_tool_calls, textual_retry_instruction = (
            normalize_textual_tool_protocol(content, tools)
        )

    proposed_tool_calls = response.tool_calls or textual_tool_calls
    sanitized_tool_calls: list[dict] | None = []
    retry_instruction = None
    retry_tool_name = None
    if proposed_tool_calls:
        sanitized_tool_calls, retry_instruction, retry_tool_name = (
            _sanitize_tool_calls_for_context(proposed_tool_calls)
        )
    if textual_retry_instruction is not None:
        retry_instruction = textual_retry_instruction
        retry_tool_name = None
    return LLMCompletionStep(
        content=content,
        tool_calls=tuple(sanitized_tool_calls or ()),
        reasoning_content=reasoning_content,
        retry_instruction=retry_instruction,
        usage=usage,
        retry_tool_name=retry_tool_name,
        finish_reason=normalize_llm_finish_reason(
            response.finish_reason,
            tuple(sanitized_tool_calls or ()),
        ),
    )


__all__ = ["LLMCompletionStep", "complete_llm_once"]
