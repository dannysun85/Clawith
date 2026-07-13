"""Unified LLM calling service with failover support for all execution paths.

This module provides a shared entry point for all LLM calls across:
- WebSocket chat
- IM channels (Feishu, Slack, Teams, Discord, WeCom, DingTalk)
- Background services (task executor, scheduler, heartbeat, etc.)

All paths now support:
1. Config-level fallback: if primary missing, use fallback directly
2. Runtime failover: if primary fails with retryable error, try fallback once
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.core.logging_config import get_trace_id, privacy_safe_shape
from app.database import async_session
from app.services.credit_service import charge_credits, check_credit_balance
from app.services.provider_pricing import provider_text_credits
from app.services.model_router import resolve_route
from app.services.quota_guard import (
    QuotaExceeded,
    check_agent_llm_quota,
    check_plan_inference_entitlement,
    check_tenant_token_credits,
    consume_agent_llm_quota,
)

from app.services.token_tracker import (
    TokenUsage,
    record_token_usage,
    extract_token_usage,
    estimate_token_usage_from_chars,
)

from .client import LLMError
from .failover import (
    CredentialFailureAction,
    FailoverErrorType,
    classify_error,
    credential_failure_action,
    extract_minimax_code,
    is_rate_limit_error,
)
from .finish import FINISH_PROTOCOL_REMINDER, FINISH_TOOL_DEFINITION, find_finish_call, parse_tool_arguments
from .utils import (
    LLMMessage,
    create_llm_client,
    get_credential_api_key,
    get_max_tokens,
    get_model_api_key,
    get_provider_spec,
)
from .load_balancer import (
    NoCredentialAvailable,
    no_credential_user_message,
    record_credential_call,
    mark_credential_degraded,
    mark_credential_quota_exceeded,
    pick_credential,
)
# Backward compat alias (record_credential_call supersedes increment_credential_usage)
increment_credential_usage = record_credential_call


async def _record_llm_product_issue(
    *,
    category: str,
    error_code: str,
    model,
    agent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    route_meta: "RouteMeta | None",
    severity: str = "error",
) -> None:
    from app.services.production_issue_monitor import record_production_issue

    await record_production_issue(
        source="llm_runtime",
        category=category,
        summary=(
            "Platform model credential route was unavailable"
            if category == "credential"
            else "Model provider operation failed"
        ),
        severity=severity,
        error_code=error_code,
        operation=getattr(route_meta, "action", None) or "chat",
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        trace_id=get_trace_id(),
        metadata={
            "provider": getattr(model, "provider", None),
            "model": getattr(model, "model", None),
            "modality": getattr(route_meta, "modality", None),
            "saas_tier": getattr(route_meta, "saas_tier", None),
            "reason_code": error_code if category == "credential" else None,
        },
    )


async def _apply_credential_failure_policy(
    credential_id: uuid.UUID,
    error: Exception,
    *,
    log_context: str,
    modality: str | None = None,
) -> None:
    """Apply the shared-pool circuit-breaker policy in every LLM path."""

    action = credential_failure_action(error, modality=modality)
    if action is CredentialFailureAction.DEGRADE:
        await mark_credential_degraded(credential_id, immediate=True)
    elif action is CredentialFailureAction.QUOTA_EXCEEDED:
        await mark_credential_quota_exceeded(credential_id)
    elif action is CredentialFailureAction.MODALITY_QUOTA_EXCEEDED:
        from app.services.llm.load_balancer import mark_credential_modality_quota_exceeded

        assert modality is not None
        await mark_credential_modality_quota_exceeded(
            credential_id,
            modality,
            error_code=extract_minimax_code(str(error)) or "2056",
        )
    elif is_rate_limit_error(error):
        logger.warning(f"[{log_context}] Rate limit on credential {credential_id}")
        await asyncio.sleep(1.0)

# NOTE: agent_tools imports are deferred to function bodies to avoid circular
# import: agent_tools → llm.finish → llm/__init__ → caller → agent_tools


async def get_agent_tools_for_llm(*args, **kwargs):
    from app.services.agent_tools import get_agent_tools_for_llm as _impl

    return await _impl(*args, **kwargs)


async def execute_tool(*args, **kwargs):
    from app.services.agent_tools import execute_tool as _impl

    return await _impl(*args, **kwargs)

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.llm import LLMModel


@dataclass
class RouteMeta:
    """SaaS route metadata for a single LLM invocation."""

    saas_tier: str
    modality: str
    action: str = "chat"


@dataclass
class AgentLLMInvocation:
    """Resolved model, billing, and credential context for a background run."""

    model: "LLMModel"
    fallback_model: "LLMModel | None"
    route_meta: RouteMeta | None
    tenant_id: uuid.UUID | None
    api_key: str
    base_url: str | None
    credential_id: uuid.UUID | None


TOOLS_REQUIRING_ARGS = frozenset({
    "write_file", "read_file", "move_file", "delete_file", "read_document",
    "send_message_to_agent", "send_feishu_message", "send_email",
    "execute_code", "execute_code_e2b",
})

MAX_CONSECUTIVE_INVALID_TOOL_CALLS = 3


def _sanitize_tool_calls_for_context(tool_calls: list[dict]) -> tuple[list[dict] | None, str | None]:
    """Return OpenAI-compatible tool calls, or a retry instruction if args are invalid."""
    sanitized: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        tool_name = fn.get("name") or ""
        raw_args = fn.get("arguments", "{}")

        if raw_args is None or raw_args == "":
            args_str = "{}"
        elif isinstance(raw_args, str):
            try:
                json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[LLM] Invalid tool arguments JSON for {}: {} at pos {}",
                    tool_name or "<unknown>",
                    exc.msg,
                    exc.pos,
                )
                return None, (
                    "Your previous tool call arguments were not valid JSON. "
                    f"The affected tool was `{tool_name or 'unknown'}`. "
                    "Retry the tool call now with `function.arguments` as one valid JSON object string. "
                    "Escape all quotes and newlines inside long HTML, CSS, JavaScript, or markdown content. "
                    "Do not explain; only retry with a valid tool call."
                )
            args_str = raw_args
        elif isinstance(raw_args, (dict, list)):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            return None, (
                "Your previous tool call arguments had an unsupported type. "
                f"The affected tool was `{tool_name or 'unknown'}`. "
                "Retry the tool call with `function.arguments` as one valid JSON object string."
            )

        new_tc = {
            "id": tc.get("id", ""),
            "type": tc.get("type") or "function",
            "function": {
                "name": tool_name,
                "arguments": args_str,
            },
        }
        if "_gemini_extra" in tc:
            new_tc["_gemini_extra"] = tc["_gemini_extra"]
        sanitized.append(new_tc)

    return sanitized, None


def _build_ordered_api_messages(
    static_prompt: str,
    dynamic_prompt: str,
    messages: list[dict],
) -> list[LLMMessage]:
    """Build one leading system message and preserve all non-system order."""
    system_parts = [static_prompt] if static_prompt else []
    non_system_messages: list[LLMMessage] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue
        non_system_messages.append(LLMMessage(
            role=role,
            content=msg.get("content"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
            reasoning_content=msg.get("reasoning_content"),
        ))

    return [
        LLMMessage(
            role="system",
            content="\n\n".join(system_parts),
            dynamic_content=dynamic_prompt,
        ),
        *non_system_messages,
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Failover Guard
# ═══════════════════════════════════════════════════════════════════════════════

class FailoverGuard:
    """Guard state for failover decisions."""

    def __init__(self):
        self.tool_executed = False
        self.streaming_started = False
        self.failover_done = False

    def mark_tool_executed(self):
        """Mark that a side-effecting tool has been executed."""
        self.tool_executed = True

    def mark_streaming_started(self):
        """Mark that streaming output has started."""
        self.streaming_started = True

    def mark_failover_done(self):
        """Mark that failover has already happened once."""
        self.failover_done = True

    def can_failover(self) -> bool:
        """Check if failover is allowed based on guard rules."""
        if self.failover_done:
            return False  # Only failover once
        if self.tool_executed:
            return False  # Don't failover after side effects
        if self.streaming_started:
            return False  # Don't failover after streaming started
        return True


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.
    
    Uses unified classification from failover.py.
    """
    if not (result.startswith("[LLM Error]") or result.startswith("[LLM call error]") or result.startswith("[Error]")):
        return False
        
    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _is_llm_error_result(result: str) -> bool:
    """Return whether a model result represents an error rather than content."""
    return result.startswith(("[LLM Error]", "[LLM call error]", "[Error]", "⚠️"))


def _get_model_timeout(model: "LLMModel") -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _usage_from_response_or_estimate(response, api_messages: list[LLMMessage]) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    round_chars = sum(len(m.content or '') if isinstance(m.content, str) else 0 for m in api_messages)
    round_chars += len(response.content or '')
    return estimate_token_usage_from_chars(round_chars)


def _is_persisted_model(model: object) -> bool:
    """Return true for real DB-backed LLMModel objects, false for unit-test fakes."""
    return isinstance(getattr(model, "id", None), uuid.UUID)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_agent_config(agent_id) -> tuple[int, str | None]:
    """Get agent config: max_tool_rounds and token limit status."""
    if not agent_id:
        return 50, None

    try:
        from app.models.agent import Agent as AgentModel
        async with async_session() as _db:
            _ar = await _db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _ar.scalar_one_or_none()
            if _agent:
                max_rounds = _agent.max_tool_rounds or 50
                if _agent.max_tokens_per_day and _agent.tokens_used_today >= _agent.max_tokens_per_day:
                    return max_rounds, f"⚠️ Daily token usage has reached the limit ({_agent.tokens_used_today:,}/{_agent.max_tokens_per_day:,}). Please try again tomorrow or ask admin to increase the limit."
                if _agent.max_tokens_per_month and _agent.tokens_used_month >= _agent.max_tokens_per_month:
                    return max_rounds, f"⚠️ Monthly token usage has reached the limit ({_agent.tokens_used_month:,}/{_agent.max_tokens_per_month:,}). Please ask admin to increase the limit."
                return max_rounds, None
    except Exception:
        pass
    return 50, None


async def _get_user_name(user_id) -> str | None:
    """Get user's display name for personalized context."""
    if not user_id:
        return None
    try:
        from app.models.user import User as _UserModel
        from app.models.agent import Agent as _AgentModel
        async with async_session() as _udb:
            _ur = await _udb.execute(select(_UserModel).where(_UserModel.id == user_id))
            _u = _ur.scalar_one_or_none()
            if _u:
                return _u.display_name or _u.username
            # Check Agent name fallback
            _ar = await _udb.execute(select(_AgentModel).where(_AgentModel.id == user_id))
            _a = _ar.scalar_one_or_none()
            if _a:
                return _a.name
    except Exception:
        pass
    return None


def _convert_messages_for_vision(
    api_messages: list, supports_vision: bool
) -> list:
    """Convert image markers to vision format if supported, or strip them."""
    import re as _re_v
    import copy

    # Deep copy to avoid modifying the original list in place
    new_messages = copy.deepcopy(api_messages)

    if supports_vision:
        # Vision format: convert image markers in strings to OpenAI Vision API list format
        for i, msg in enumerate(new_messages):
            if msg.role != "user" or not msg.content or not isinstance(msg.content, str):
                continue
            
            content_str = msg.content
            pattern = r'\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]'
            images = _re_v.findall(pattern, content_str)
            
            if not images:
                continue

            text = _re_v.sub(pattern, '', content_str).strip()
            parts = [{"type": "image_url", "image_url": {"url": img}} for img in images]
            if text:
                # Per OpenAI spec, text part should come after image parts
                parts.append({"type": "text", "text": text})
            
            new_messages[i] = type(msg)(role=msg.role, content=parts, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id)
    else:
        # Non-vision format: ensure content is a string for all roles, stripping image data.
        _img_marker_pattern = r'\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]'
        for i, msg in enumerate(new_messages):
            
            if isinstance(msg.content, list):
                # It's a list, join all text parts. This handles user messages
                # with vision content and tool messages from vision_inject.
                text_parts = [part.get("text", "") for part in msg.content if part.get("type") == "text"]
                content_str = "\n".join(text_parts).strip()
                new_messages[i] = type(msg)(role=msg.role, content=content_str, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id)

            elif isinstance(msg.content, str) and "[image_data:" in msg.content:
                # It's a string with image markers, strip them
                _n_imgs = len(_re_v.findall(_img_marker_pattern, msg.content))
                cleaned = _re_v.sub(_img_marker_pattern, '', msg.content).strip()
                if _n_imgs > 0:
                    cleaned += f"\n[用户发送了 {_n_imgs} 张图片，但当前模型不支持视觉，无法查看图片内容]"
                new_messages[i] = type(msg)(role=msg.role, content=cleaned, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id)

    return new_messages


def _check_tool_requires_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """Check if tool requires arguments and return (should_execute, result_or_error)."""
    if not args and tool_name in TOOLS_REQUIRING_ARGS:
        return False, f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments."
    if tool_name in {"execute_code", "execute_code_e2b"}:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return False, (
                f"Error: {tool_name} requires a non-empty string `code` parameter. "
                "Retry the tool call with both `language` and `code`; do not call it again with the same arguments."
            )
    return True, ""


def _allowed_tool_names(tools_for_llm: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools_for_llm or []:
        name = ((tool.get("function") or {}).get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _build_runtime_capability_manifest(
    tools_for_llm: list[dict] | None,
    *,
    supports_vision: bool = False,
) -> str:
    """Describe the exact runtime tool surface as an authoritative prompt fact."""
    names = sorted(_allowed_tool_names(tools_for_llm))
    return (
        "\n\n## Runtime tool capabilities (authoritative)\n"
        "The following JSON array is the complete set of tools enabled for this "
        "turn. Check it before claiming that a capability exists or is missing. "
        "Never invent, rename, or assume tools outside this list:\n"
        f"{json.dumps(names, ensure_ascii=False)}\n"
        f"Native vision input for this turn: {str(bool(supports_vision)).lower()}.\n"
        "A generic file-reading tool does not inspect pixels in an image. If native "
        "vision is false and no enabled tool explicitly understands or edits images, "
        "state that limitation and never claim to have seen or used a reference image.\n"
        "Only report an external action or generated file after a successful tool result. "
        "Reuse artifact paths and URLs exactly as returned; never invent or rename them. "
        "For PowerPoint conversion, use convert_html_to_pptx only when that exact tool name is listed."
    )


def _tool_not_enabled_message(tool_name: str) -> str:
    return (
        f"Tool `{tool_name}` is not enabled for this agent. "
        "Do not call it again. Use only the tools currently available to you, "
        "or explain that the required capability is not enabled."
    )


async def _process_tool_call(
    tc: dict,
    api_messages: list,
    agent_id,
    user_id,
    session_id: str,
    supports_vision: bool,
    on_tool_call,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
    route_meta: RouteMeta | None = None,
    on_code_output=None,
) -> str:
    """Process a single tool call and return result."""
    fn = tc["function"]
    tool_name = fn["name"]
    raw_args = fn.get("arguments", "{}")
    logger.info(
        "[LLM] Calling tool: {} argument_shape={}",
        tool_name,
        privacy_safe_shape(raw_args),
    )

    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        args = {}

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        return error_msg

    if tool_name not in allowed_tool_names:
        result = _tool_not_enabled_message(tool_name)
        logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
        if on_tool_call:
            try:
                await on_tool_call({
                    "name": tool_name,
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "done",
                    "result": result,
                    "reasoning_content": full_reasoning_content
                })
            except Exception:
                pass
        api_messages.append(LLMMessage(
            role="tool",
            tool_call_id=tc["id"],
            content=result,
        ))
        return ""

    # Notify client about tool call (in-progress)
    if on_tool_call:
        try:
            await on_tool_call({
                "name": tool_name,
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "running",
                "reasoning_content": full_reasoning_content
            })
        except Exception:
            pass

    # Execute tool — pass on_output for execute_code streaming
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    result = await execute_tool(
        tool_name, args,
        agent_id=agent_id,
        user_id=user_id or agent_id,
        session_id=session_id,
        saas_tier=route_meta.saas_tier if route_meta else None,
        on_output=_on_output,
    )
    logger.debug(f"[LLM] Tool result chars={len(result)}")

    # ── Vision injection for screenshot tools ──
    tool_content: str | list = str(result)
    if supports_vision and agent_id:
        try:
            from app.services.vision_inject import try_inject_screenshot_vision
            settings = get_settings()
            ws_path = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR) / str(agent_id)
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = vision_content
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client about tool call result
    if on_tool_call:
        try:
            await on_tool_call({
                "name": tool_name,
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "done",
                "result": result,
                "reasoning_content": full_reasoning_content
            })
        except Exception:
            pass
    
    api_messages.append(LLMMessage(
        role="tool",
        tool_call_id=tc["id"],
        content=tool_content,
    ))
    return ""



# ═══════════════════════════════════════════════════════════════════════════════
# Core LLM Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def resolve_model_key(model: "LLMModel") -> tuple[str, str | None, uuid.UUID | None]:
    """Resolve (api_key, base_url, credential_id) for a model.

    Platform model (tenant_id=null): pick from the credential pool (load balanced).
    Tenant model (tenant_id set): use the model's own api_key_encrypted (single key).
    Raises NoCredentialAvailable if the platform pool has no healthy credential.
    """
    if not _is_persisted_model(model):
        return get_model_api_key(model) or "test-key", getattr(model, "base_url", None), None
    if getattr(model, "tenant_id", None) is None:
        try:
            cred = await pick_credential(model.provider, model.modality)
        except NoCredentialAvailable:
            spec = get_provider_spec(model.provider)
            if spec and not spec.requires_api_key:
                return "", model.base_url, None
            raise
        return get_credential_api_key(cred), cred.base_url or model.base_url, cred.id
    return get_model_api_key(model), model.base_url, None


async def ensure_agent_billing_route(
    agent_id: uuid.UUID | str | None,
    model: "LLMModel",
    route_meta: RouteMeta | None,
) -> tuple["LLMModel", RouteMeta | None]:
    """Recover the SaaS route when a caller omitted billing metadata.

    Older background/channel entry points passed a tenant-scoped concrete model
    directly to ``call_llm``.  For subscribed agents that silently bypassed the
    platform account pool and Credits settlement.  Keep legacy deployments
    working, but automatically route any agent for which ``resolve_agent_model``
    returns SaaS metadata.
    """
    if route_meta is not None or not agent_id or not _is_persisted_model(model):
        return model, route_meta

    from app.models.agent import Agent

    try:
        normalized_agent_id = agent_id if isinstance(agent_id, uuid.UUID) else uuid.UUID(str(agent_id))
    except (TypeError, ValueError, AttributeError):
        return model, route_meta

    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.id == normalized_agent_id))
        agent = result.scalar_one_or_none()

    if not agent:
        return model, route_meta

    primary_model, fallback_model, resolved_meta = await resolve_agent_model(agent)
    routed_model = primary_model or fallback_model
    if not routed_model or not resolved_meta:
        return model, route_meta

    logger.warning(
        "[LLM Billing] Recovered missing route metadata for agent {}: {}/{} -> {}",
        normalized_agent_id,
        resolved_meta.saas_tier,
        resolved_meta.modality,
        getattr(routed_model, "model", "unknown"),
    )
    return routed_model, resolved_meta


async def _prepare_llm_billing_context(
    agent_id: uuid.UUID | str | None,
    model: "LLMModel",
    route_meta: RouteMeta | None,
) -> uuid.UUID | None:
    """Validate entitlements/credits and return the tenant to settle."""
    persisted_model = _is_persisted_model(model)
    if persisted_model:
        await check_plan_inference_entitlement(
            agent_id,
            modality=(
                route_meta.modality
                if route_meta is not None
                else getattr(model, "modality", None)
            ),
            saas_tier=route_meta.saas_tier if route_meta is not None else None,
        )

    if not (agent_id and route_meta and persisted_model):
        return None

    from app.models.agent import Agent

    try:
        normalized_agent_id = agent_id if isinstance(agent_id, uuid.UUID) else uuid.UUID(str(agent_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise QuotaExceeded(
            "Agent billing context is invalid.",
            quota_type="billing_context",
        ) from exc

    async with async_session() as db:
        result = await db.execute(select(Agent.tenant_id).where(Agent.id == normalized_agent_id))
        tenant_id = result.scalar_one_or_none()

    if tenant_id is None:
        raise QuotaExceeded(
            "Agent billing context is unavailable.",
            quota_type="billing_context",
        )

    await check_tenant_token_credits(tenant_id)
    await check_credit_balance(
        tenant_id,
        route_meta.action,
        route_meta.modality,
        route_meta.saas_tier,
    )
    await check_agent_llm_quota(normalized_agent_id, model_tier=route_meta.saas_tier)
    return tenant_id


async def _record_llm_usage_and_charge(
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    model: "LLMModel",
    usage: TokenUsage,
    route_meta: RouteMeta | None,
) -> None:
    """Record completed provider usage and charge tenant Credits."""
    if not agent_id or not tenant_id:
        return

    saas_tier = route_meta.saas_tier if route_meta else getattr(model, "tier", None)
    modality = route_meta.modality if route_meta else getattr(model, "modality", None)
    credit_delta = provider_text_credits(model.provider, model.model, usage) if route_meta else None

    await consume_agent_llm_quota(agent_id, model_tier=saas_tier)
    if route_meta:
        await charge_credits(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action=route_meta.action,
            modality=modality,
            saas_tier=saas_tier,
            provider=model.provider,
            model=model.model,
            delta=credit_delta,
        )


async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_thinking=None,
    supports_vision=False,
    max_tool_rounds_override: int | None = None,
    skip_tools: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    system_prompt_suffix: str | None = None,
    route_meta: RouteMeta | None = None,
) -> str:
    """Call LLM via unified client with function-calling tool loop."""
    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id)
    if _token_limit_msg:
        return _token_limit_msg

    # Subscription inference-capability gate (模块四 7.4): reject if the
    # tenant's plan disallows the routed SaaS tier/modality. Concrete model rows
    # remain platform routing details. Returns a user-facing string
    # (call_llm's contract); "⚠️" is non-retryable so failover won't engage.
    try:
        model, route_meta = await ensure_agent_billing_route(agent_id, model, route_meta)
        _tenant_id = await _prepare_llm_billing_context(agent_id, model, route_meta)
    except QuotaExceeded as _ent_err:
        return f"⚠️ {_ent_err.message}"

    if max_tool_rounds_override and max_tool_rounds_override < _max_tool_rounds:
        _max_tool_rounds = max_tool_rounds_override

    # Get user's name for personalized context
    if current_user_name_override:
        _user_name = current_user_name_override
    else:
        _user_name = await _get_user_name(user_id)

    # Auto-assign fallback tool call logger if none provided but conversation context exists
    if on_tool_call is None and session_id:
        from app.services.chat_session_service import save_tool_call_log
        async def _default_on_tool_call(data: dict):
            if data.get("status") == "done" and agent_id:
                await save_tool_call_log(
                    agent_id=agent_id,
                    user_id=user_id or agent_id,
                    conversation_id=session_id,
                    tool_name=data.get("name", ""),
                    arguments=data.get("args"),
                    result=data.get("result"),
                    status="done",
                    tool_call_id=data.get("call_id"),
                    reasoning_content=data.get("reasoning_content"),
                )
        on_tool_call = _default_on_tool_call

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context
    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, role_description, current_user_name=_user_name)
    if system_prompt_suffix:
        dynamic_prompt += system_prompt_suffix

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn; keep the runtime-level `finish`
    # tool available so every turn still has an explicit stop signal.
    if skip_tools:
        tools_for_llm = [FINISH_TOOL_DEFINITION]
    else:
        from app.services.agent_tools import AGENT_TOOLS
        tools_for_llm = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
    dynamic_prompt += _build_runtime_capability_manifest(
        tools_for_llm,
        supports_vision=supports_vision,
    )
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    # Convert messages to LLMMessage format
    api_messages = _build_ordered_api_messages(static_prompt, dynamic_prompt, messages)

    # Vision format conversion
    api_messages = _convert_messages_for_vision(api_messages, supports_vision)

    # Create the unified LLM client
    # Resolve API key: platform model → credential pool; tenant model → its own key.
    try:
        _api_key, _base_url, _cred_id = await resolve_model_key(model)
    except NoCredentialAvailable as exc:
        await _record_llm_product_issue(
            category="credential",
            error_code=exc.reason_code.value,
            model=model,
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=_tenant_id,
            route_meta=route_meta,
            severity=("critical" if exc.reason_code.value == "all_unhealthy" else "error"),
        )
        return f"⚠️ {no_credential_user_message(exc)}"
    provider_spec = get_provider_spec(model.provider)
    if not _api_key and (provider_spec is None or provider_spec.requires_api_key):
        return "⚠️ 未配置 API key"
    try:
        client = create_llm_client(
            provider=model.provider,
            api_key=_api_key,
            model=model.model,
            base_url=_base_url,
            timeout=_get_model_timeout(model),
        )
    except Exception as e:
        return f"[Error] Failed to create LLM client: {e}"

    max_tokens = get_max_tokens(model.provider, model.model, getattr(model, 'max_output_tokens', None))
    _accumulated_usage = TokenUsage()
    _unsaved_usage = TokenUsage()
    _usage_finalized = False

    async def _finalize_llm_usage(*, billable: bool) -> None:
        nonlocal _unsaved_usage, _usage_finalized
        if _usage_finalized:
            return
        _usage_finalized = True
        if agent_id and _unsaved_usage.total_tokens > 0:
            await record_token_usage(agent_id, _unsaved_usage)
            _unsaved_usage = TokenUsage()
        if _accumulated_usage.total_tokens <= 0:
            return
        if _cred_id:
            await record_credential_call(
                _cred_id,
                tokens_used=_accumulated_usage.total_tokens,
            )
        if billable:
            await _record_llm_usage_and_charge(
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=_tenant_id,
                model=model,
                usage=_accumulated_usage,
                route_meta=route_meta,
            )

    # Tool-calling loop
    consecutive_invalid_tool_calls = 0
    for round_i in range(_max_tool_rounds):
        # Dynamic tool-call limit warning
        _warn_threshold_80 = int(_max_tool_rounds * 0.8)
        _warn_threshold_96 = _max_tool_rounds - 2
        if round_i == _warn_threshold_80:
            api_messages.append(LLMMessage(
                role="user",
                content=(
                    f"⚠️ 你已使用 {round_i}/{_max_tool_rounds} 轮工具调用。"
                    "如果当前任务尚未完成，请尽快使用 upsert_focus_item 保存进度，"
                    "并使用 set_trigger 设置续接触发器，在剩余轮次中做好收尾。"
                ),
            ))
        elif round_i == _warn_threshold_96:
            api_messages.append(LLMMessage(
                role="user",
                content="🚨 仅剩 2 轮工具调用。请立即使用 upsert_focus_item 保存进度并设置续接触发器。",
            ))

        # Check token usage limit mid-loop (every 3 rounds)
        if round_i > 0 and round_i % 3 == 0:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
                _unsaved_usage = TokenUsage()
                _, _token_limit_msg = await _get_agent_config(agent_id)
                if _token_limit_msg:
                    logger.warning(f"[LLM] Token limit exceeded mid-loop agent={agent_id}")
                    await _finalize_llm_usage(billable=False)
                    await client.close()
                    return _token_limit_msg

        try:
            # Use streaming API for real-time responses
            async def _buffer_chunk(_text: str) -> None:
                # Final user-facing text must come through finish(content=...).
                return None

            response = await client.stream(
                messages=api_messages,
                tools=tools_for_llm if tools_for_llm else None,
                temperature=model.temperature,
                max_tokens=max_tokens,
                on_chunk=_buffer_chunk,
                on_tool_delta=on_tool_delta,
                on_thinking=on_thinking,
            )
        except asyncio.CancelledError:
            await _finalize_llm_usage(billable=False)
            await client.close()
            raise
        except LLMError as e:
            logger.error(
                "[LLM] provider operation failed provider={} model={} error_type={} error_code={}",
                getattr(model, "provider", "?"),
                getattr(model, "model", "?"),
                type(e).__name__,
                extract_minimax_code(str(e)) or "unknown",
            )
            await _record_llm_product_issue(
                category="llm_provider",
                error_code=extract_minimax_code(str(e)) or type(e).__name__,
                model=model,
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=_tenant_id,
                route_meta=route_meta,
            )
            if _cred_id:
                await _apply_credential_failure_policy(
                    _cred_id,
                    e,
                    log_context="LLM",
                    modality=getattr(route_meta, "modality", None) or getattr(model, "modality", None),
                )
            await _finalize_llm_usage(billable=False)
            await client.close()
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.error(
                "[LLM] unexpected provider failure provider={} model={} error_type={} error_code={}",
                getattr(model, "provider", "?"),
                getattr(model, "model", "?"),
                type(e).__name__,
                extract_minimax_code(str(e)) or "unknown",
            )
            await _record_llm_product_issue(
                category="llm_provider",
                error_code=extract_minimax_code(str(e)) or type(e).__name__,
                model=model,
                agent_id=agent_id,
                user_id=user_id,
                tenant_id=_tenant_id,
                route_meta=route_meta,
            )
            if _cred_id:
                await _apply_credential_failure_policy(
                    _cred_id,
                    e,
                    log_context="LLM",
                    modality=getattr(route_meta, "modality", None) or getattr(model, "modality", None),
                )
            await _finalize_llm_usage(billable=False)
            await client.close()
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        # Track tokens for this round
        _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
        _accumulated_usage.add(_usage_this_round)
        _unsaved_usage.add(_usage_this_round)

        # Most hosted providers must finish explicitly via finish(content=...).
        # Ollama also serves models without reliable function calling; its
        # provider capability permits a non-empty plain-text final response so
        # a simple greeting cannot spin until the tool-round limit is reached.
        if not response.tool_calls:
            provider_spec = get_provider_spec(getattr(model, "provider", ""))
            if (
                response.content
                and provider_spec
                and provider_spec.accepts_plain_text_final
            ):
                await _finalize_llm_usage(billable=True)
                await client.close()
                return response.content
            if response.content:
                api_messages.append(LLMMessage(role="assistant", content=response.content))
            api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
            continue

        # Execute tool calls
        logger.info(f"[LLM] Round {round_i+1}: {len(response.tool_calls)} tool call(s)")
        sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
        if retry_instruction:
            consecutive_invalid_tool_calls += 1
            if consecutive_invalid_tool_calls >= MAX_CONSECUTIVE_INVALID_TOOL_CALLS:
                logger.error(
                    "[LLM] Circuit breaker stopped {} consecutive invalid tool calls",
                    consecutive_invalid_tool_calls,
                )
                await _finalize_llm_usage(billable=False)
                await client.close()
                return (
                    "⚠️ 工具参数连续 3 次无效，本次执行已自动停止且不会扣除对话 Credits。"
                    "请缩短单次写入内容或重新发起任务。"
                )
            api_messages.append(LLMMessage(role="user", content=retry_instruction))
            continue
        consecutive_invalid_tool_calls = 0

        finish_call = find_finish_call(sanitized_tool_calls)
        if finish_call:
            if finish_call.valid:
                await _finalize_llm_usage(billable=True)
                await client.close()
                return finish_call.content

            api_messages.append(LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            ))
            api_messages.append(LLMMessage(
                role="tool",
                content=finish_call.error or "`finish` was invalid.",
                tool_call_id=finish_call.call_id,
            ))
            continue

        # Add assistant message with tool calls
        api_messages.append(LLMMessage(
            role="assistant",
            content=response.content or None,
            tool_calls=sanitized_tool_calls,
            reasoning_content=response.reasoning_content,
        ))

        full_reasoning_content = response.reasoning_content or ""

        for tc in sanitized_tool_calls or []:
            try:
                tool_error = await _process_tool_call(
                    tc=tc,
                    api_messages=api_messages,
                    agent_id=agent_id,
                    user_id=user_id,
                    session_id=session_id,
                    supports_vision=supports_vision,
                    on_tool_call=on_tool_call,
                    on_code_output=on_code_output,
                    full_reasoning_content=full_reasoning_content,
                    allowed_tool_names=allowed_tool_names,
                    route_meta=route_meta,
                )
            except asyncio.CancelledError:
                await _finalize_llm_usage(billable=False)
                await client.close()
                raise
            except Exception as e:
                logger.exception(
                    "[LLM] Tool execution failed after provider usage error_type={}",
                    type(e).__name__,
                )
                await _finalize_llm_usage(billable=False)
                await client.close()
                return f"[Error] Tool execution failed: {type(e).__name__}: {str(e)[:200]}"
            if tool_error:
                api_messages.append(LLMMessage(
                    role="tool",
                    content=tool_error,
                    tool_call_id=tc.get("id", ""),
                ))

    # Settle provider usage even when the model never emits a valid finish call.
    await _finalize_llm_usage(billable=False)
    await client.close()
    return "[Error] Too many tool call rounds"


async def call_llm_with_failover(
    primary_model,
    fallback_model,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_tool_call=None,
    on_tool_delta=None,
    supports_vision=False,
    on_failover=None,
    skip_tools: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    system_prompt_suffix: str | None = None,
    route_meta: RouteMeta | None = None,
) -> str:
    """Call LLM with automatic failover support."""
    guard = FailoverGuard()

    # Config-level fallback: if no primary, use fallback directly
    if primary_model is None and fallback_model is not None:
        logger.info("[Failover] Primary model not configured, using fallback directly")
        primary_model = fallback_model
        fallback_model = None

    if primary_model is None:
        return "⚠️ 未配置 LLM 模型"

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_tool_call(data: dict):
        if data.get("status") == "done":
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=supports_vision,
        skip_tools=skip_tools,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        system_prompt_suffix=system_prompt_suffix,
        route_meta=route_meta,
    )

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        if _is_llm_error_result(primary_result):
            logger.warning(
                "[Failover] Skipped: primary model returned a non-retryable error "
                "result_shape={}",
                privacy_safe_shape(primary_result),
            )
        else:
            logger.debug("[Failover] Primary model completed successfully; no failover needed")
        return primary_result

    # Check guard conditions
    if not guard.can_failover():
        if guard.tool_executed:
            logger.warning("[Failover] Blocked: side-effecting tool already executed")
        elif guard.streaming_started:
            logger.warning("[Failover] Blocked: streaming already started")
        elif guard.failover_done:
            logger.warning("[Failover] Blocked: failover already done once")
        return primary_result

    # No fallback available
    if fallback_model is None:
        logger.warning("[Failover] No fallback model available")
        return primary_result

    # Runtime failover: retry with fallback model
    logger.info(f"[Failover] Retrying with fallback model: {fallback_model.provider}/{fallback_model.model}")

    if on_failover:
        try:
            await on_failover(f"Switched to fallback model: {fallback_model.model}")
        except Exception:
            pass

    guard.mark_failover_done()

    # Call fallback with fresh callbacks
    fallback_guard = FailoverGuard()
    fallback_guard.mark_failover_done()

    async def _fallback_on_chunk(text: str):
        fallback_guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _fallback_on_tool_call(data: dict):
        if data.get("status") == "done":
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=getattr(fallback_model, 'supports_vision', False),
        skip_tools=skip_tools,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        system_prompt_suffix=system_prompt_suffix,
        route_meta=route_meta,
    )

    # Combine error messages if fallback also failed
    if _is_llm_error_result(fallback_result):
        return f"⚠️ 调用模型出错: Primary: {primary_result[:80]} | Fallback: {fallback_result[:80]}"

    return fallback_result


# ═══════════════════════════════════════════════════════════════════════════════
# High-level Agent Call Functions
# ═══════════════════════════════════════════════════════════════════════════════

async def resolve_agent_model(
    agent: "Agent",
    tier: str | None = None,
    modality: str | None = None,
) -> tuple["LLMModel | None", "LLMModel | None", RouteMeta | None]:
    """Resolve the concrete LLM model(s) for an agent.

    If a SaaS tier is provided (or the agent has preferred_tier), use the
    model_routes table. Otherwise fall back to legacy primary_model_id /
    fallback_model_id.
    """
    from app.models.llm import LLMModel

    effective_tier = tier
    effective_modality = modality

    # Explicit per-chat selections remain strict and are checked by
    # resolve_route. Persisted preferences may predate the active plan, so only
    # that stored default is normalized to keep background channels usable
    # after a downgrade without granting a disallowed tier.
    if effective_tier is None:
        from app.services.agent_plan_selection import (
            InvalidAgentPlanSelection,
            resolve_agent_plan_selection,
        )
        from app.services.entitlements import get_tenant_entitlements

        entitlements = await get_tenant_entitlements(agent.tenant_id) if agent.tenant_id else None
        if entitlements:
            try:
                effective_tier, stored_modality = resolve_agent_plan_selection(
                    entitlements,
                    agent.preferred_tier,
                    agent.preferred_modality,
                    strict=False,
                )
            except InvalidAgentPlanSelection as exc:
                raise QuotaExceeded(str(exc), quota_type=exc.quota_type) from exc
            effective_modality = effective_modality or stored_modality
        else:
            # Preserve the legacy concrete-model path for deployments that
            # have not enabled subscriptions/model routes yet.
            effective_tier = agent.preferred_tier
            effective_modality = effective_modality or agent.preferred_modality or "text"
    else:
        effective_modality = effective_modality or agent.preferred_modality or "text"

    if effective_tier:
        route = await resolve_route(
            agent.tenant_id,
            effective_tier,
            effective_modality,
            allow_fallback=True,
        )
        return (
            route.model,
            route.fallback_model,
            RouteMeta(saas_tier=route.saas_tier, modality=route.modality, action="chat"),
        )

    # Legacy path
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        async with async_session() as db:
            result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
            primary_model = result.scalar_one_or_none()

    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        async with async_session() as db:
            result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
            fallback_model = result.scalar_one_or_none()

    return primary_model, fallback_model, None


async def prepare_agent_llm_invocation(
    agent: "Agent",
    *,
    action: str = "chat",
) -> AgentLLMInvocation | None:
    """Resolve and preflight a background LLM run through the SaaS route."""
    primary_model, fallback_model, route_meta = await resolve_agent_model(agent)
    if primary_model is None and fallback_model is not None:
        primary_model = fallback_model
        fallback_model = None
    if primary_model is None:
        return None

    if route_meta is not None:
        route_meta = replace(route_meta, action=action)

    tenant_id = await _prepare_llm_billing_context(agent.id, primary_model, route_meta)
    api_key, base_url, credential_id = await resolve_model_key(primary_model)
    return AgentLLMInvocation(
        model=primary_model,
        fallback_model=fallback_model,
        route_meta=route_meta,
        tenant_id=tenant_id,
        api_key=api_key,
        base_url=base_url,
        credential_id=credential_id,
    )


async def settle_agent_llm_invocation(
    invocation: AgentLLMInvocation,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    usage: TokenUsage,
) -> None:
    """Record account-pool usage and settle Credits for a background run."""
    if usage.total_tokens <= 0:
        return
    if invocation.credential_id:
        await record_credential_call(
            invocation.credential_id,
            tokens_used=usage.total_tokens,
        )
    await _record_llm_usage_and_charge(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id=invocation.tenant_id,
        model=invocation.model,
        usage=usage,
        route_meta=invocation.route_meta,
    )


async def call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    supports_vision: bool = False,
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.models.agent import Agent
    from app.core.permissions import is_agent_expired

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    # Resolve primary/fallback model via SaaS tier route or legacy model IDs
    try:
        primary_model, fallback_model, route_meta = await resolve_agent_model(agent)
    except QuotaExceeded as e:
        return f"⚠️ {e.message}"

    # Config-level fallback: primary missing -> use fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None
        logger.warning(f"[call_agent_llm] Primary model unavailable, using fallback: {getattr(primary_model, 'model', '?')}")

    if not primary_model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

    # Build conversation messages
    messages: list[dict] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    # Use unified call_llm_with_failover
    try:
        reply = await call_llm_with_failover(
            primary_model=primary_model,
            fallback_model=fallback_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            supports_vision=supports_vision or getattr(primary_model, 'supports_vision', False),
            route_meta=route_meta,
        )
        return reply
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[call_agent_llm] Unexpected error error_type={type(e).__name__}")
        return f"⚠️ 调用模型出错: {error_msg[:150]}"


async def call_agent_llm_with_tools(
    db: AsyncSession,
    agent_id: uuid.UUID,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int = 50,
    session_id: str = "",
) -> str:
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.models.agent import Agent

    # Load agent and models
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ Agent not found"

    # Resolve models via SaaS tier route or legacy model IDs
    try:
        primary_model, fallback_model, route_meta = await resolve_agent_model(agent)
    except QuotaExceeded as e:
        return f"⚠️ {e.message}"

    # Config-level fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None

    if not primary_model:
        return f"⚠️ {agent.name} has no LLM model configured"

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
        _usage_finalized = False
        _cred_id = None
        client = None
        tool_executed = False
        tenant_id = await _prepare_llm_billing_context(agent_id, model, route_meta)

        async def _finalize_background_usage() -> None:
            nonlocal _unsaved_usage, _usage_finalized
            if _usage_finalized:
                return
            _usage_finalized = True
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
                _unsaved_usage = TokenUsage()
            if _accumulated_usage.total_tokens <= 0:
                return
            if _cred_id:
                await record_credential_call(_cred_id, tokens_used=_accumulated_usage.total_tokens)
            await _record_llm_usage_and_charge(
                agent_id=agent_id,
                user_id=agent.creator_id,
                tenant_id=tenant_id,
                model=model,
                usage=_accumulated_usage,
                route_meta=route_meta,
            )

        try:
            _api_key, _base_url, _cred_id = await resolve_model_key(model)
        except NoCredentialAvailable as exc:
            await _record_llm_product_issue(
                category="credential",
                error_code=exc.reason_code.value,
                model=model,
                agent_id=agent_id,
                user_id=agent.creator_id,
                tenant_id=tenant_id,
                route_meta=route_meta,
                severity=("critical" if exc.reason_code.value == "all_unhealthy" else "error"),
            )
            return f"⚠️ {no_credential_user_message(exc)}", False, tool_executed
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=_api_key,
                model=model.model,
                base_url=_base_url,
                timeout=_get_model_timeout(model),
            )

            max_tokens = get_max_tokens(
                model.provider, model.model,
                getattr(model, 'max_output_tokens', None)
            )

            # Tool-calling loop
            api_messages = list(messages)
            for round_i in range(max_rounds):
                # Check token usage limit mid-loop (every 3 rounds)
                if round_i > 0 and round_i % 3 == 0:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                        _unsaved_usage = TokenUsage()
                        _, _token_limit_msg = await _get_agent_config(agent_id)
                        if _token_limit_msg:
                            logger.warning(
                                f"[call_agent_llm_with_tools] Token limit exceeded mid-loop agent={agent_id}"
                            )
                            await _finalize_background_usage()
                            await client.close()
                            return _token_limit_msg, False, tool_executed

                try:
                    response = await client.complete(
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as e:
                    logger.error(
                        "[call_agent_llm_with_tools] agent={} provider={} model={} "
                        "error_type={} error_code={}",
                        agent_id,
                        getattr(model, "provider", "?"),
                        getattr(model, "model", "?"),
                        type(e).__name__,
                        extract_minimax_code(str(e)) or "unknown",
                    )
                    await _record_llm_product_issue(
                        category="llm_provider",
                        error_code=extract_minimax_code(str(e)) or type(e).__name__,
                        model=model,
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        tenant_id=tenant_id,
                        route_meta=route_meta,
                    )
                    if _cred_id:
                        await _apply_credential_failure_policy(
                            _cred_id,
                            e,
                            log_context="call_agent_llm_with_tools",
                            modality=getattr(route_meta, "modality", None) or getattr(model, "modality", None),
                        )
                    raise

                # Track tokens for this round
                _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
                _accumulated_usage.add(_usage_this_round)
                _unsaved_usage.add(_usage_this_round)

                if not response.tool_calls:
                    if response.content:
                        api_messages.append(LLMMessage(role="assistant", content=response.content))
                    api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                    continue

                # Execute tool calls
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue

                finish_call = find_finish_call(sanitized_tool_calls)
                if finish_call:
                    if finish_call.valid:
                        await _finalize_background_usage()
                        await client.close()
                        return finish_call.content, True, tool_executed
                    api_messages.append(LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    ))
                    api_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=finish_call.call_id,
                        content=finish_call.error or "`finish` was invalid.",
                    ))
                    continue

                api_messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=sanitized_tool_calls,
                    reasoning_content=response.reasoning_content,
                ))

                for tc in sanitized_tool_calls or []:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError:
                        args = {}

                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
                        result = _tool_not_enabled_message(tool_name)
                    else:
                        should_execute, argument_error = _check_tool_requires_args(tool_name, args)
                        if not should_execute:
                            result = argument_error
                        else:
                            result = await execute_tool(
                                tool_name, args,
                                agent_id=agent_id,
                                user_id=agent.creator_id,
                                session_id=session_id,
                                saas_tier=route_meta.saas_tier if route_meta else None,
                            )
                    api_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=tc["id"],
                        content=str(result),
                    ))

            await _finalize_background_usage()
            await client.close()
            return "[Error] Too many tool call rounds", False, tool_executed

        except asyncio.CancelledError:
            await _finalize_background_usage()
            if client is not None:
                await client.close()
            raise
        except Exception as e:
            await _finalize_background_usage()
            if client is not None:
                await client.close()
            return f"[Error] {e}", False, tool_executed

    # Try primary model
    try:
        reply, success, primary_tool_executed = await _try_model(primary_model)
    except QuotaExceeded as e:
        return f"⚠️ {e.message}"
    if success:
        return reply

    # Primary failed - check if retryable
    error_type = classify_error(Exception(reply))
    if error_type == FailoverErrorType.NON_RETRYABLE or not fallback_model:
        return reply

    if primary_tool_executed:
        logger.warning("[call_agent_llm_with_tools] Blocked fallback: side-effecting tool already executed")
        return reply

    # Try fallback model
    logger.info(f"[call_agent_llm_with_tools] Retrying with fallback: {fallback_model.model}")
    try:
        reply2, success2, _fallback_tool_executed = await _try_model(fallback_model)
    except QuotaExceeded as e:
        return f"⚠️ {e.message}"
    if success2:
        return reply2

    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"


__all__ = [
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "resolve_agent_model",
    "ensure_agent_billing_route",
    "prepare_agent_llm_invocation",
    "settle_agent_llm_invocation",
    "AgentLLMInvocation",
    "RouteMeta",
    "FailoverGuard",
    "is_retryable_error",
]
