"""Autonomy boundary enforcement service.

Implements the three-level autonomy system:
  L1 — Auto-execute, notify creator
  L2 — Notify creator, auto-execute
  L3 — Require explicit approval before execution
"""

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from loguru import logger
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import privacy_safe_shape
from app.config import get_settings
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.database import async_session
from app.services.feishu_service import feishu_service


HIGH_RISK_DEFAULT_L3_ACTIONS = {
    "douyin_publish_job",
    "douyin_reply_comment",
    "douyin_external_write",
    "execute_code",
    "manage_agent_capabilities",
    "manage_automation",
    "manage_external_deployment",
    "publish_external_content",
    "send_external_message",
}

APPROVAL_EXECUTION_STALE_AFTER = timedelta(minutes=20)
APPROVAL_AUTOMATIC_EXECUTION_ENABLED = get_settings().APPROVAL_EXECUTION_ENABLED


def _approval_resolution_copy(status: str) -> tuple[str, str]:
    """Return notification copy that matches the effective execution switch."""

    if status != "approved":
        return "rejected", "Approval rejected. The action will not execute."
    if APPROVAL_AUTOMATIC_EXECUTION_ENABLED:
        return (
            "approved — queued for execution",
            "Approval recorded. The secure worker queued the signed action; "
            "no side effect has completed yet.",
        )
    return (
        "approved — execution paused",
        "Approval recorded. Automatic approval execution is paused in this "
        "release; no side effect has run.",
    )
APPROVAL_EXECUTION_HARD_TIMEOUT_SECONDS = 15 * 60
APPROVAL_EXECUTION_POLL_SECONDS = 2.0
APPROVAL_EXECUTION_CONCURRENCY = 4
APPROVAL_DAEMON_MAX_CONSECUTIVE_FAILURES = 5
APPROVAL_PREVIEW_MAX_DEPTH = 6
APPROVAL_PREVIEW_MAX_NODES = 160
APPROVAL_PREVIEW_MAX_STRING = 16_000
APPROVAL_PREVIEW_MAX_BYTES = 64 * 1024
APPROVAL_ARGUMENTS_MAX_BYTES = 128 * 1024
APPROVAL_CIPHERTEXT_MAX_CHARS = 512 * 1024
_PREVIEW_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|access_?key|authorization|bearer|client_?secret|cookie|"
    r"credential|jwt|password|passwd|private_?key|refresh_?token|secret|"
    r"session|signature|token)(_|$)",
    re.IGNORECASE,
)


def _normalized_preview_key(key: object) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _redact_preview_string(value: str) -> str:
    """Bound strings and remove credentials carried by public URLs."""

    bounded = value[:APPROVAL_PREVIEW_MAX_STRING]
    if len(value) > APPROVAL_PREVIEW_MAX_STRING:
        bounded += "…[truncated]"
    if not bounded.lower().startswith(("http://", "https://")):
        return bounded
    try:
        parsed = urlsplit(bounded)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        # Query strings frequently contain expiring media signatures.  The
        # approver needs the destination/path, never the bearer value.
        query = "[redacted]" if parsed.query else ""
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "[invalid URL]"


def _bounded_approval_preview(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> object:
    """Return an action preview without persisted ciphertext or credentials."""

    if budget is None:
        budget = [APPROVAL_PREVIEW_MAX_NODES]
    if budget[0] <= 0:
        return "[preview limit reached]"
    budget[0] -= 1
    if depth >= APPROVAL_PREVIEW_MAX_DEPTH:
        return "[depth limit reached]"
    if isinstance(value, dict):
        preview: dict[str, object] = {}
        for raw_key, child in list(value.items())[:40]:
            key = str(raw_key)[:120]
            if _PREVIEW_SECRET_KEY_RE.search(_normalized_preview_key(key)):
                preview[key] = "[redacted]"
            else:
                preview[key] = _bounded_approval_preview(
                    child,
                    depth=depth + 1,
                    budget=budget,
                )
        if len(value) > 40:
            preview["__truncated__"] = f"{len(value) - 40} more fields"
        return preview
    if isinstance(value, (list, tuple)):
        preview_list = [
            _bounded_approval_preview(child, depth=depth + 1, budget=budget)
            for child in list(value)[:30]
        ]
        if len(value) > 30:
            preview_list.append(f"[{len(value) - 30} more items]")
        return preview_list
    if isinstance(value, str):
        return _redact_preview_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_preview_string(str(value))


def _approval_preview_fits(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> bool:
    if budget is None:
        budget = [APPROVAL_PREVIEW_MAX_NODES]
    if budget[0] <= 0 or depth >= APPROVAL_PREVIEW_MAX_DEPTH:
        return False
    budget[0] -= 1
    if isinstance(value, dict):
        if len(value) > 40:
            return False
        return all(
            len(str(key)) <= 120
            and _approval_preview_fits(child, depth=depth + 1, budget=budget)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        if len(value) > 30:
            return False
        return all(
            _approval_preview_fits(child, depth=depth + 1, budget=budget)
            for child in value
        )
    if isinstance(value, str):
        return len(value) <= APPROVAL_PREVIEW_MAX_STRING
    return value is None or isinstance(value, (bool, int, float))


def _canonical_tool_arguments(arguments: dict) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_approval_signature(
    agent_id: uuid.UUID,
    action_type: str,
    tool_name: str,
    arguments: dict,
    *,
    payload_version: int,
    requested_by: uuid.UUID | None = None,
    origin_session_id: str | None = None,
) -> str:
    from app.config import get_settings

    if payload_version == 2:
        payload = (
            f"v2\n{agent_id}\n{action_type}\n{tool_name}\n"
            f"{_canonical_tool_arguments(arguments)}"
        )
    elif payload_version == 3 and requested_by is not None:
        payload = (
            f"v3\n{agent_id}\n{action_type}\n{tool_name}\n{requested_by}\n"
            f"{origin_session_id or ''}\n{_canonical_tool_arguments(arguments)}"
        )
    else:
        raise ValueError("Approved tool execution context is incomplete")
    return hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_tool_approval_details(
    agent_id: uuid.UUID,
    action_type: str,
    tool_name: str,
    arguments: dict,
    requested_by: uuid.UUID,
    origin_session_id: str | None = None,
) -> dict:
    """Bind the complete immutable tool payload to an HMAC for approval."""

    from app.config import get_settings
    from app.core.security import encrypt_data

    canonical_arguments = _canonical_tool_arguments(arguments)
    if len(canonical_arguments.encode("utf-8")) > APPROVAL_ARGUMENTS_MAX_BYTES:
        raise ValueError("Approval payload is too large")
    if not _approval_preview_fits(arguments):
        raise ValueError("Approval payload is too complex to preview safely")

    normalized_requested_by = uuid.UUID(str(requested_by))
    normalized_origin_session_id = (
        str(uuid.UUID(str(origin_session_id))) if origin_session_id else None
    )

    return {
        "payload_version": 3,
        "action_type": action_type,
        "tool": tool_name,
        "args_encrypted": encrypt_data(
            canonical_arguments,
            get_settings().SECRET_KEY,
        ),
        "args_hash": hashlib.sha256(
            canonical_arguments.encode("utf-8")
        ).hexdigest(),
        "args_signature": _tool_approval_signature(
            agent_id,
            action_type,
            tool_name,
            arguments,
            payload_version=3,
            requested_by=normalized_requested_by,
            origin_session_id=normalized_origin_session_id,
        ),
        "request_shape": privacy_safe_shape(arguments),
        "requested_by": str(normalized_requested_by),
        "origin_session_id": normalized_origin_session_id,
    }


def _verified_tool_arguments(
    agent_id: uuid.UUID,
    details: dict,
    *,
    expected_action_type: str | None = None,
) -> tuple[str, dict]:
    from app.config import get_settings
    from app.core.security import decrypt_data

    payload_version = details.get("payload_version")
    if payload_version not in {2, 3}:
        raise ValueError("Approved tool payload version is unsupported; request a new approval")
    action_type = details.get("action_type")
    tool_name = details.get("tool")
    encrypted_arguments = details.get("args_encrypted")
    expected_hash = details.get("args_hash")
    signature = details.get("args_signature")
    if not isinstance(action_type, str) or not action_type:
        raise ValueError("Approved tool payload is incomplete; request a new approval")
    if expected_action_type is not None and action_type != expected_action_type:
        raise ValueError("Approval action does not match its signed payload")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("Approved tool payload is missing the tool name")
    if not isinstance(encrypted_arguments, str) or not encrypted_arguments:
        raise ValueError("Approved tool payload is incomplete; request a new approval")
    if len(encrypted_arguments) > APPROVAL_CIPHERTEXT_MAX_CHARS:
        raise ValueError("Approved tool payload is too large")
    try:
        canonical_arguments = decrypt_data(
            encrypted_arguments,
            get_settings().SECRET_KEY,
        )
        arguments = json.loads(canonical_arguments)
    except Exception as exc:
        raise ValueError("Approved tool payload cannot be decrypted") from exc
    if not isinstance(arguments, dict):
        raise ValueError("Approved tool payload is incomplete; request a new approval")
    if len(canonical_arguments.encode("utf-8")) > APPROVAL_ARGUMENTS_MAX_BYTES:
        raise ValueError("Approved tool payload is too large")
    actual_hash = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()
    if not isinstance(expected_hash, str) or not hmac.compare_digest(
        expected_hash,
        actual_hash,
    ):
        raise ValueError("Approved tool payload integrity check failed")
    requested_by: uuid.UUID | None = None
    origin_session_id: str | None = None
    if payload_version == 3:
        try:
            requested_by = uuid.UUID(str(details.get("requested_by")))
            origin_session_id = (
                str(uuid.UUID(str(details["origin_session_id"])))
                if details.get("origin_session_id")
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Approved tool execution context is incomplete") from exc
    expected_signature = _tool_approval_signature(
        agent_id,
        action_type,
        tool_name,
        arguments,
        payload_version=payload_version,
        requested_by=requested_by,
        origin_session_id=origin_session_id,
    )
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature,
        expected_signature,
    ):
        raise ValueError("Approved tool payload integrity check failed")
    return tool_name, arguments


def _verified_tool_execution_context(
    agent_id: uuid.UUID,
    details: dict,
    *,
    expected_action_type: str | None = None,
) -> tuple[str, dict, uuid.UUID | None, str | None]:
    """Verify the immutable action plus its effective platform principal."""

    tool_name, arguments = _verified_tool_arguments(
        agent_id,
        details,
        expected_action_type=expected_action_type,
    )
    if details.get("payload_version") != 3:
        if tool_name in {"send_message_to_agent", "send_file_to_agent"}:
            raise ValueError(
                "Legacy A2A approval lacks a signed requester; request a new approval"
            )
        return tool_name, arguments, None, None
    requested_by = uuid.UUID(str(details["requested_by"]))
    origin_session_id = (
        str(uuid.UUID(str(details["origin_session_id"])))
        if details.get("origin_session_id")
        else None
    )
    return tool_name, arguments, requested_by, origin_session_id


def _approval_request_fingerprint(details: dict) -> str:
    """Return the signed payload identity used for active-request deduping."""

    signature = details.get("args_signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("Approved tool payload is incomplete; request a new approval")
    return signature


def public_approval_details(
    agent_id: uuid.UUID,
    action_type: str,
    details: dict | None,
) -> dict:
    """Project private signed details into a bounded, safe approval preview."""

    raw_details = details if isinstance(details, dict) else {}
    try:
        tool_name, arguments = _verified_tool_arguments(
            agent_id,
            raw_details,
            expected_action_type=action_type,
        )
    except ValueError:
        return {
            "payload_state": "invalid",
            "approvable": False,
            "message": "This approval payload is legacy, incomplete, or no longer verifiable. Reject it and request a new approval.",
        }
    if not _approval_preview_fits(arguments):
        return {
            "payload_state": "invalid",
            "approvable": False,
            "message": "This approval cannot be shown completely within the safe preview limit. Split the action and request a new approval.",
        }
    preview = _bounded_approval_preview(arguments)
    if len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > APPROVAL_PREVIEW_MAX_BYTES:
        return {
            "payload_state": "invalid",
            "approvable": False,
            "message": "This approval is too large to preview safely. Split the action and request a new approval.",
        }
    return {
        "payload_state": "verified",
        "approvable": True,
        "tool": tool_name,
        "parameters": preview,
    }


def approval_to_public_dict(approval: ApprovalRequest, *, agent_name: str | None = None) -> dict:
    """Serialize an approval without exposing encrypted payload internals."""

    if approval.agent_id is None:
        projected_details = {
            "payload_state": "invalid",
            "approvable": False,
            "message": (
                "The originating Agent was deleted. This audit record cannot "
                "be executed."
            ),
        }
    else:
        projected_details = public_approval_details(
            approval.agent_id,
            approval.action_type,
            approval.details,
        )
    if approval.execution_status in {"legacy", "invalid"}:
        projected_details = {
            "payload_state": "invalid",
            "approvable": False,
            "message": "This approval predates the secure execution contract. Reject it and request a new approval.",
        }
    return {
        "id": approval.id,
        "agent_id": approval.agent_id,
        "agent_name": agent_name,
        "action_type": approval.action_type,
        "details": projected_details,
        "status": approval.status,
        "created_at": approval.created_at,
        "resolved_at": approval.resolved_at,
        "resolved_by": approval.resolved_by,
        "execution_status": approval.execution_status,
        "execution_claimed_at": approval.execution_claimed_at,
        "execution_finished_at": approval.execution_finished_at,
        "execution_attempts": approval.execution_attempts,
        "execution_result_summary": approval.execution_result_summary or {},
        "execution_error_code": approval.execution_error_code,
        "execution_available": APPROVAL_AUTOMATIC_EXECUTION_ENABLED,
        "execution_paused_reason": (
            None
            if APPROVAL_AUTOMATIC_EXECUTION_ENABLED
            else "automatic_approval_execution_paused_by_release_policy"
        ),
    }


def _approval_action_matches_tool(action_type: str, tool_name: str) -> bool:
    expected: dict[str, set[str]] = {
        "write_workspace_files": {"write_file", "move_file", "edit_file"},
        "delete_files": {"delete_file"},
        "send_feishu_message": {"send_feishu_message"},
        "send_external_message": {
            "send_channel_message",
            "send_platform_message",
            "send_channel_file",
            "send_email",
            "reply_email",
        },
        "manage_automation": {
            "set_trigger",
            "update_trigger",
            "cancel_trigger",
        },
        "send_message_to_agent": {"send_message_to_agent"},
        "send_file_to_agent": {"send_file_to_agent"},
        "web_search": {"web_search"},
        "execute_code": {
            "execute_code",
            "execute_code_e2b",
            "agentbay_code_execute",
            "agentbay_code_write_file",
            "agentbay_code_read_file",
            "agentbay_code_edit_file",
            "agentbay_command_exec",
        },
        "manage_agent_capabilities": {
            "import_mcp_server",
            "install_skill",
        },
        "manage_external_deployment": {
            "vercel_deploy",
            "vercel_set_env",
            "vercel_manage_domain",
            "neon_create_database",
        },
        "publish_external_content": {"publish_page"},
        "douyin_publish_job": {"douyin_run_publish_job"},
        "douyin_reply_comment": {"douyin_reply_comment"},
    }
    return tool_name in expected.get(action_type, set())


class AutonomyService:
    """Enforce autonomy boundaries for agent operations."""

    @staticmethod
    def _runtime_approval_identity(
        action_type: str,
        details: dict,
    ) -> tuple[uuid.UUID, str] | None:
        runtime_scope = details.get("runtime_scope")
        if not isinstance(runtime_scope, dict):
            return None
        run_id_raw = runtime_scope.get("run_id")
        tool_call_id = runtime_scope.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return None
        try:
            run_id = uuid.UUID(str(run_id_raw))
        except (TypeError, ValueError):
            return None
        approval_id = uuid.uuid5(
            run_id,
            f"runtime-approval:{action_type}:{tool_call_id}",
        )
        return approval_id, f"approval:{approval_id}"

    @staticmethod
    def _runtime_resume_details(
        approval: ApprovalRequest,
    ) -> dict | None:
        details = approval.details
        if not isinstance(details, dict):
            return None
        runtime_scope = details.get("runtime_scope")
        if not isinstance(runtime_scope, dict):
            return None
        correlation_id = runtime_scope.get("approval_correlation_id")
        tool_call_id = runtime_scope.get("tool_call_id")
        if (
            not isinstance(correlation_id, str)
            or not correlation_id
            or not isinstance(tool_call_id, str)
            or not tool_call_id
        ):
            return None
        try:
            tenant_id = uuid.UUID(str(runtime_scope.get("tenant_id")))
            run_id = uuid.UUID(str(runtime_scope.get("run_id")))
        except (TypeError, ValueError):
            return None
        return {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "correlation_id": correlation_id,
        }

    async def check_and_enforce(
        self, db: AsyncSession, agent: Agent, action_type: str, details: dict
    ) -> dict:
        """Check if an action is allowed under the agent's autonomy policy.

        Returns:
            {
                "allowed": True/False,
                "level": "L1"/"L2"/"L3",
                "approval_id": uuid (if L3),
                "message": str,
            }
        """
        policy = agent.autonomy_policy or {}
        default_level = "L3" if action_type in HIGH_RISK_DEFAULT_L3_ACTIONS else "L2"
        level = policy.get(action_type, default_level)
        if action_type == "execute_code":
            from app.config import get_settings

            if get_settings().CODE_EXECUTION_REQUIRE_APPROVAL:
                level = "L3"
        runtime_identity = self._runtime_approval_identity(action_type, details)
        if runtime_identity is not None:
            runtime_approval_id, runtime_correlation_id = runtime_identity
            existing_result = await db.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.id == runtime_approval_id
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing is not None:
                if (
                    existing.agent_id != agent.id
                    or existing.action_type != action_type
                ):
                    raise ValueError(
                        "Runtime approval identity does not match the requested action"
                    )
                if existing.status == "approved":
                    return {
                        "allowed": True,
                        "level": "L3",
                        "approval_id": str(existing.id),
                        "approval_status": "approved",
                        "correlation_id": runtime_correlation_id,
                        "message": "Approval granted",
                    }
                return {
                    "allowed": False,
                    "level": "L3",
                    "approval_id": str(existing.id),
                    "approval_status": existing.status,
                    "correlation_id": runtime_correlation_id,
                    "message": (
                        "Approval requested from creator"
                        if existing.status == "pending"
                        else "Approval rejected"
                    ),
                }

        # Log the action regardless of level
        audit = AuditLog(
            agent_id=agent.id,
            action=f"autonomy_check:{action_type}",
            details={"level": level, "request_shape": privacy_safe_shape(details)},
        )
        db.add(audit)

        if level == "L1":
            # Auto-execute, just log
            logger.info(f"L1: Auto-executing {action_type} for agent {agent.id}")
            return {
                "allowed": True,
                "level": "L1",
                "message": "Auto-executed",
            }

        elif level == "L2":
            # Auto-execute but notify creator
            logger.info(f"L2: Executing {action_type} for agent {agent.id} with notification")
            await self._notify_creator(db, agent, action_type, details)
            return {
                "allowed": True,
                "level": "L2",
                "message": "Executed and creator notified",
            }

        elif level == "L3":
            # Persist only a complete signed payload.  Repeated model retries
            # for the same immutable action reuse the active request rather
            # than creating multiple independently executable approvals.
            approval_id = None
            correlation_id = None
            approval_details = details
            if runtime_identity is not None:
                approval_id, correlation_id = runtime_identity
                approval_details = dict(details)
                runtime_scope = dict(approval_details["runtime_scope"])
                runtime_scope["approval_correlation_id"] = correlation_id
                approval_details["runtime_scope"] = runtime_scope
            _verified_tool_arguments(
                agent.id,
                approval_details,
                expected_action_type=action_type,
            )
            request_fingerprint = _approval_request_fingerprint(approval_details)
            existing_result = await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.agent_id == agent.id,
                    ApprovalRequest.request_fingerprint == request_fingerprint,
                    or_(
                        ApprovalRequest.status == "pending",
                        and_(
                            ApprovalRequest.status == "approved",
                            ApprovalRequest.execution_status.in_(("pending", "executing")),
                        ),
                    ),
                )
                .order_by(ApprovalRequest.created_at.desc())
                .limit(1)
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                return {
                    "allowed": False,
                    "level": "L3",
                    "approval_id": str(existing.id),
                    "message": "An identical approval is already active",
                }

            approval = ApprovalRequest(
                id=approval_id,
                agent_id=agent.id,
                action_type=action_type,
                details=approval_details,
                request_fingerprint=request_fingerprint,
            )
            try:
                # The partial unique index is the final concurrency authority.
                # A savepoint keeps the caller's outer transaction usable when
                # another request commits the same signed action first.
                async with db.begin_nested():
                    db.add(approval)
                    await db.flush()
            except IntegrityError:
                existing_result = await db.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.agent_id == agent.id,
                        ApprovalRequest.request_fingerprint == request_fingerprint,
                        or_(
                            ApprovalRequest.status == "pending",
                            and_(
                                ApprovalRequest.status == "approved",
                                ApprovalRequest.execution_status.in_(("pending", "executing")),
                            ),
                        ),
                    )
                    .order_by(ApprovalRequest.created_at.desc())
                    .limit(1)
                )
                existing = existing_result.scalar_one_or_none()
                if existing is None:
                    raise
                return {
                    "allowed": False,
                    "level": "L3",
                    "approval_id": str(existing.id),
                    "message": "An identical approval is already active",
                }

            logger.info(f"L3: Approval required for {action_type} by agent {agent.id}")
            await self._request_approval(db, agent, approval)

            return {
                "allowed": False,
                "level": "L3",
                "approval_id": str(approval.id),
                "approval_status": "pending",
                "correlation_id": correlation_id,
                "message": "Approval requested from creator",
            }

        return {"allowed": False, "level": "unknown", "message": "Unknown autonomy level"}

    async def resolve_approval(
        self,
        db: AsyncSession,
        approval_id: uuid.UUID,
        user: User,
        action: str,
        *,
        expected_agent_id: uuid.UUID | None = None,
    ) -> ApprovalRequest:
        """Commit an approval decision without executing its side effect.

        The dedicated approval worker is the only execution owner.  This
        method intentionally commits the decision before returning so the
        caller's request transaction cannot execute an action whose approval
        state later rolls back.
        """
        if action not in {"approve", "reject"}:
            raise ValueError("Approval action must be 'approve' or 'reject'")
        result = await db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id)
            .with_for_update()
        )
        approval = result.scalar_one_or_none()
        if not approval:
            raise ValueError("Approval not found")

        if expected_agent_id is not None and approval.agent_id != expected_agent_id:
            raise ValueError("Approval does not belong to this Agent")

        if approval.status != "pending":
            raise ValueError("Approval already resolved")
        runtime_resume = self._runtime_resume_details(approval)

        # Permission check: only agent creator or platform admin can resolve
        agent_result = await db.execute(select(Agent).where(Agent.id == approval.agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            raise ValueError("Approval Agent not found")
        same_tenant_org_admin = bool(
            agent
            and user.role == "org_admin"
            and user.tenant_id
            and agent.tenant_id == user.tenant_id
        )
        identity_is_platform_admin = bool(getattr(getattr(user, "identity", None), "is_platform_admin", False))
        if agent and agent.creator_id != user.id and user.role != "platform_admin" and not identity_is_platform_admin and not same_tenant_org_admin:
            raise ValueError("Only the agent creator or platform admin can resolve approvals")

        if action == "approve":
            if (
                runtime_resume is None
                and not APPROVAL_AUTOMATIC_EXECUTION_ENABLED
            ):
                raise ValueError(
                    "Automatic approval execution is paused in this release; "
                    "the action has not been approved or executed"
                )
            if approval.execution_status in {"legacy", "invalid"}:
                raise ValueError("Approval payload is invalid; reject it and request a new approval")
            projection = public_approval_details(
                approval.agent_id,
                approval.action_type,
                approval.details,
            )
            if not projection.get("approvable"):
                raise ValueError("Approval payload cannot be previewed safely; reject it and request a new approval")
            tool_name, _arguments = _verified_tool_arguments(
                approval.agent_id,
                approval.details or {},
                expected_action_type=approval.action_type,
            )
            if not _approval_action_matches_tool(approval.action_type, tool_name):
                raise ValueError("Approval action does not match its signed tool payload")

        approval.status = "approved" if action == "approve" else "rejected"
        approval.resolved_at = datetime.now(timezone.utc)
        approval.resolved_by = user.id
        approval.execution_status = (
            "pending"
            if action == "approve" and runtime_resume is None
            else "not_required"
        )
        approval.execution_claim_token = None
        approval.execution_claimed_at = None
        approval.execution_finished_at = None
        approval.execution_attempts = 0
        approval.execution_result_summary = {}
        approval.execution_error_code = None

        # Log
        db.add(AuditLog(
            user_id=user.id,
            agent_id=approval.agent_id,
            action=f"approval_{approval.status}",
            details={"approval_id": str(approval.id), "action_type": approval.action_type},
        ))

        if runtime_resume is not None:
            from app.services.agent_runtime.adapter import RuntimeCommandIntake
            from app.services.agent_runtime.contracts import ResumeRunCommand

            await db.flush()
            await RuntimeCommandIntake(db).resume_run(
                ResumeRunCommand(
                    tenant_id=runtime_resume["tenant_id"],
                    run_id=runtime_resume["run_id"],
                    idempotency_key=(
                        f"approval:{approval.id}:{approval.status}"
                    ),
                    payload={
                        "resume_type": "user_input",
                        "correlation_id": runtime_resume["correlation_id"],
                        "payload": {
                            "content": (
                                "Workspace deletion approved. Continue the "
                                "pending tool call."
                                if approval.status == "approved"
                                else "Workspace deletion rejected. Do not "
                                "execute the pending tool call."
                            ),
                            "approval_id": str(approval.id),
                            "decision": approval.status,
                        },
                    },
                    actor_user_id=user.id,
                )
            )

        # Web notification to agent creator about the result
        if agent:
            from app.services.notification_service import send_notification
            status_label, body_text = _approval_resolution_copy(approval.status)
            await send_notification(
                db,
                user_id=agent.creator_id,
                type="approval_resolved",
                title=f"[{agent.name}] {approval.action_type} — {status_label}",
                body=body_text,
                link=f"/agents/{agent.id}#approvals",
                ref_id=approval.id,
            )

            # Also notify the user who requested the action (if different from creator)
            requested_by = approval.details.get("requested_by") if approval.details else None
            if requested_by:
                try:
                    requester_id = uuid.UUID(requested_by)
                    if requester_id != agent.creator_id:
                        await send_notification(
                            db,
                            user_id=requester_id,
                            type="approval_resolved",
                            title=f"[{agent.name}] {approval.action_type} — {status_label}",
                            body=body_text,
                            link=f"/agents/{agent.id}#activityLog",
                            ref_id=approval.id,
                        )
                except (ValueError, AttributeError):
                    pass  # Invalid UUID, skip

        await db.flush()
        await db.commit()
        return approval

    async def execute_pending_approval(self, approval_id: uuid.UUID) -> bool:
        """Atomically claim and execute one approved action at most once.

        The claim token fences stale workers.  Once a claim is committed, no
        code path ever resets it to ``pending``.  Unknown/cancelled outcomes
        become ``ambiguous`` and require a brand-new approval instead of an
        automatic replay.
        """

        if not APPROVAL_AUTOMATIC_EXECUTION_ENABLED:
            logger.warning(
                "Approval automatic execution is paused approval_id={}",
                approval_id,
            )
            return False

        claim_token = uuid.uuid4()
        claimed_at = datetime.now(timezone.utc)
        async with async_session() as claim_db:
            claim_result = await claim_db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.status == "approved",
                    ApprovalRequest.execution_status == "pending",
                    ApprovalRequest.execution_attempts == 0,
                    or_(
                        ApprovalRequest.execution_not_before.is_(None),
                        ApprovalRequest.execution_not_before <= claimed_at,
                    ),
                )
                .values(
                    execution_status="executing",
                    execution_claim_token=claim_token,
                    execution_claimed_at=claimed_at,
                    execution_attempts=1,
                    execution_error_code=None,
                )
                .returning(
                    ApprovalRequest.agent_id,
                    ApprovalRequest.action_type,
                    ApprovalRequest.details,
                )
            )
            claimed = claim_result.one_or_none()
            await claim_db.commit()
        if claimed is None:
            return False

        agent_id, action_type, details = claimed
        dispatched = False
        try:
            (
                tool_name,
                arguments,
                requested_by,
                origin_session_id,
            ) = _verified_tool_execution_context(
                agent_id,
                details or {},
                expected_action_type=action_type,
            )
            if not _approval_action_matches_tool(action_type, tool_name):
                raise ValueError("Approval action does not match its signed tool payload")
            if requested_by is not None:
                await self._assert_requester_execution_scope(
                    agent_id,
                    requested_by,
                    origin_session_id,
                )
            # Keep the mutable Agent/Tool lifecycle check immediately before
            # dispatch.  An approval is authority to attempt the signed action,
            # not authority to bypass a later stop, expiry, deletion, or grant
            # revocation.
            await self._assert_execution_permission(agent_id, tool_name)

            from app.services.agent_tools import _execute_approved_tool

            dispatched = True
            async with asyncio.timeout(APPROVAL_EXECUTION_HARD_TIMEOUT_SECONDS):
                outcome = await _execute_approved_tool(
                    tool_name,
                    arguments,
                    agent_id,
                    approval_id=approval_id,
                    approval_claim_token=claim_token,
                    user_id=requested_by,
                    origin_session_id=origin_session_id,
                )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish_approval_execution(
                    approval_id,
                    claim_token,
                    status="ambiguous" if dispatched else "failed",
                    error_code="CancelledDuringDispatch" if dispatched else "CancelledBeforeDispatch",
                )
            )
            raise
        except ValueError as exc:
            await self._finish_approval_execution(
                approval_id,
                claim_token,
                status="failed" if not dispatched else "ambiguous",
                error_code=type(exc).__name__,
            )
            return True
        except Exception as exc:
            await self._finish_approval_execution(
                approval_id,
                claim_token,
                status="ambiguous" if dispatched else "failed",
                error_code=type(exc).__name__,
            )
            logger.error(
                "Approval execution failed approval={} claim={} phase={} error_type={}",
                approval_id,
                claim_token,
                "dispatch" if dispatched else "validation",
                type(exc).__name__,
            )
            return True

        await self._finish_approval_execution(
            approval_id,
            claim_token,
            status=outcome.status,
            result=outcome.result,
            error_code=outcome.error_code,
            outcome_code=outcome.outcome_code,
        )
        return True

    async def _assert_execution_permission(
        self,
        agent_id: uuid.UUID,
        tool_name: str,
    ) -> None:
        """Recheck Agent lifecycle and current Tool grant before dispatch."""

        from app.core.permissions import is_agent_expired
        from app.models.tool import AgentTool, Tool
        from app.services.agent_tools import _code_tool_denial_reason
        from app.services.tool_visibility import tool_enabled_for_agent

        async with async_session() as permission_db:
            agent_result = await permission_db.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if agent is None or agent.tenant_id is None:
                raise ValueError("Approval Agent is no longer available")
            if (
                agent.status not in {"running", "idle"}
                or agent.deletion_requested_at is not None
                or is_agent_expired(agent)
            ):
                raise ValueError("Approval Agent is no longer executable")
            tool_result = await permission_db.execute(
                select(Tool, AgentTool)
                .outerjoin(
                    AgentTool,
                    and_(
                        AgentTool.tool_id == Tool.id,
                        AgentTool.agent_id == agent_id,
                    ),
                )
                .where(Tool.name == tool_name, Tool.enabled.is_(True))
            )
            row = tool_result.one_or_none()
            if row is None:
                raise ValueError("Approved Tool is no longer available")
            tool, assignment = row
            currently_enabled = tool_enabled_for_agent(tool, assignment)
            if not currently_enabled:
                raise ValueError("Approved Tool permission has been revoked")
            if tool.tenant_id is not None and tool.tenant_id != agent.tenant_id:
                raise ValueError("Approved Tool no longer belongs to this company")
            if tool.source == "agent" and (
                assignment is None or tool.tenant_id != agent.tenant_id
            ):
                raise ValueError("Approved Agent-installed Tool is no longer authorized")

        code_denial = await _code_tool_denial_reason(tool_name, agent_id)
        if code_denial:
            raise ValueError(code_denial)

    async def _assert_requester_execution_scope(
        self,
        agent_id: uuid.UUID,
        requested_by: uuid.UUID,
        origin_session_id: str | None,
    ) -> None:
        """Revalidate the signed requester and optional originating session."""

        from app.core.permissions import get_agent_access_level_for_user_id
        from app.models.chat_session import ChatSession

        async with async_session() as permission_db:
            agent_result = await permission_db.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if agent is None or agent.tenant_id is None:
                raise ValueError("Approval Agent is no longer available")
            requester_result = await permission_db.execute(
                select(User).where(User.id == requested_by)
            )
            requester = requester_result.scalar_one_or_none()
            requester_identity = (
                getattr(requester, "identity", None) if requester else None
            )
            if (
                requester is None
                or not requester.is_active
                or requester.tenant_id != agent.tenant_id
                or (
                    requester_identity is not None
                    and not requester_identity.is_active
                )
            ):
                raise ValueError("Approval requester is no longer active")
            if not await get_agent_access_level_for_user_id(
                permission_db,
                requested_by,
                agent,
            ):
                raise ValueError("Approval requester no longer has Agent access")
            if origin_session_id is None:
                return
            session_result = await permission_db.execute(
                select(ChatSession).where(
                    ChatSession.id == uuid.UUID(origin_session_id),
                    ChatSession.user_id == requested_by,
                    or_(
                        ChatSession.agent_id == agent_id,
                        ChatSession.peer_agent_id == agent_id,
                    ),
                )
            )
            if session_result.scalar_one_or_none() is None:
                raise ValueError(
                    "Approval origin session no longer belongs to its requester"
                )

    async def _finish_approval_execution(
        self,
        approval_id: uuid.UUID,
        claim_token: uuid.UUID,
        *,
        status: str,
        result: object | None = None,
        error_code: str | None = None,
        outcome_code: str | None = None,
    ) -> bool:
        """CAS one claim into a terminal state without leaking result data."""

        if status not in {"succeeded", "failed", "ambiguous"}:
            raise ValueError("Invalid approval execution terminal status")
        finished_at = datetime.now(timezone.utc)
        result_summary = {}
        if result is not None:
            result_summary["shape"] = privacy_safe_shape(result)
        if outcome_code:
            result_summary["outcome_code"] = outcome_code[:100]
        async with async_session() as finish_db:
            finish_result = await finish_db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.status == "approved",
                    ApprovalRequest.execution_status == "executing",
                    ApprovalRequest.execution_claim_token == claim_token,
                )
                .values(
                    execution_status=status,
                    execution_finished_at=finished_at,
                    execution_result_summary=result_summary,
                    execution_error_code=(error_code or None),
                )
            )
            updated = finish_result.rowcount == 1
            if updated:
                approval = (
                    await finish_db.execute(
                        select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
                    )
                ).scalar_one()
                finish_db.add(
                    AuditLog(
                        agent_id=approval.agent_id,
                        action=f"approval_execution_{status}",
                        details={
                            "approval_id": str(approval_id),
                            "error_code": error_code,
                        },
                    )
                )
                if status == "ambiguous":
                    await self._project_ambiguous_external_state(
                        finish_db,
                        approval,
                        error_code=error_code,
                    )
            await finish_db.commit()
        if updated:
            await self._notify_execution_terminal_safely(
                approval_id,
                status=status,
            )
        if not updated:
            logger.warning(
                "Approval terminal CAS rejected approval={} claim={} status={}",
                approval_id,
                claim_token,
                status,
            )
        return updated

    async def _project_ambiguous_external_state(
        self,
        db: AsyncSession,
        approval: ApprovalRequest,
        *,
        error_code: str | None,
    ) -> None:
        """Project unknown Douyin dispatches onto their business records.

        A hard timeout or worker crash can happen outside the Douyin service's
        transaction. The durable Approval is then ambiguous, but the linked
        Job/Operation must not keep advertising a retryable pre-dispatch state.
        Stronger official states are deliberately excluded from these updates.
        """

        if approval.action_type not in {
            "douyin_publish_job",
            "douyin_reply_comment",
        }:
            return
        from app.models.douyin import DouyinOperation, DouyinPublishJob

        response_summary = {
            "message": (
                "抖音写入过程在官方结果确认前中断。禁止自动重试；"
                "请先在抖音官方后台核验，确认未生效后再新建审批任务。"
            ),
            "verification_required": True,
            "retry_safe": False,
            "error_code": (error_code or "UnknownDispatchOutcome")[:100],
        }
        if approval.action_type == "douyin_publish_job":
            await db.execute(
                update(DouyinPublishJob)
                .where(
                    DouyinPublishJob.approval_id == approval.id,
                    DouyinPublishJob.status.in_({
                        "approval_required",
                        "preparing_share_package",
                        "creating",
                    }),
                )
                .values(
                    status="verification_required",
                    response_summary=response_summary,
                )
            )
            await db.execute(
                update(DouyinOperation)
                .where(
                    DouyinOperation.approval_id == approval.id,
                    DouyinOperation.status.in_({"pending_approval", "running"}),
                )
                .values(
                    status="verification_required",
                    response_summary=response_summary,
                    finished_at=datetime.now(timezone.utc),
                )
            )
            return
        await db.execute(
            update(DouyinOperation)
            .where(
                DouyinOperation.approval_id == approval.id,
                DouyinOperation.status.in_({"pending_approval", "running"}),
            )
            .values(
                status="verification_required",
                response_summary=response_summary,
                finished_at=datetime.now(timezone.utc),
            )
        )

    async def _notify_execution_terminal(
        self,
        db: AsyncSession,
        approval: ApprovalRequest,
        *,
        status: str,
    ) -> None:
        """Persist bounded terminal notifications without payload or result data."""

        from app.services.notification_service import send_notification

        agent = (
            await db.execute(select(Agent).where(Agent.id == approval.agent_id))
        ).scalar_one_or_none()
        if agent is None:
            return
        labels = {
            "succeeded": "execution succeeded",
            "failed": "execution failed",
            "ambiguous": "execution outcome needs verification",
        }
        bodies = {
            "succeeded": "The approved action completed successfully.",
            "failed": "The approved action was not completed. Submit a new request only after reviewing the failure.",
            "ambiguous": (
                "The worker cannot prove whether the side effect completed. Verify it manually; "
                "the system will not replay this action automatically."
            ),
        }
        outcome_code = (approval.execution_result_summary or {}).get("outcome_code")
        douyin_outcomes = {
            "DouyinUserActionRequired": (
                "publish package ready; user confirmation required",
                "The Douyin publish package is ready. A user must still confirm in Douyin; this is not a public publish success.",
            ),
            "DouyinAcceptedPendingReview": (
                "accepted by Douyin; review pending",
                "Douyin accepted the submission for review. It is not yet confirmed as publicly published.",
            ),
            "DouyinPublishedPendingVerification": (
                "Douyin callback received; verification pending",
                "Douyin reported a publish callback. Final public visibility and data verification are still pending.",
            ),
            "DouyinUserConfirmedPendingVerification": (
                "user confirmed; verification pending",
                "The user confirmed the Douyin action. The system is still waiting for official verification.",
            ),
            "DouyinConfirmed": (
                "Douyin operation confirmed",
                "The approved Douyin operation was confirmed by the official workflow.",
            ),
        }
        if status == "succeeded" and outcome_code in douyin_outcomes:
            labels[status], bodies[status] = douyin_outcomes[outcome_code]
        recipients = {agent.creator_id}
        requested_by = (approval.details or {}).get("requested_by")
        try:
            if requested_by:
                recipients.add(uuid.UUID(str(requested_by)))
        except (TypeError, ValueError):
            pass
        for recipient_id in recipients:
            await send_notification(
                db,
                user_id=recipient_id,
                type="approval_execution_terminal",
                title=f"[{agent.name}] {approval.action_type} — {labels[status]}",
                body=bodies[status],
                link=f"/agents/{agent.id}#approvals",
                ref_id=approval.id,
            )

    async def _notify_execution_terminal_safely(
        self,
        approval_id: uuid.UUID,
        *,
        status: str,
    ) -> None:
        """Send terminal UX feedback without making the durable CAS rollback."""

        try:
            async with async_session() as notification_db:
                approval = (
                    await notification_db.execute(
                        select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
                    )
                ).scalar_one_or_none()
                if approval is None:
                    return
                await self._notify_execution_terminal(
                    notification_db,
                    approval,
                    status=status,
                )
                await notification_db.commit()
        except Exception as exc:
            logger.warning(
                "Approval terminal notification failed approval={} status={} error_type={}",
                approval_id,
                status,
                type(exc).__name__,
            )

    async def reconcile_stale_executions(self) -> int:
        """Fence abandoned dispatches as ambiguous; never make them retryable."""

        cutoff = datetime.now(timezone.utc) - APPROVAL_EXECUTION_STALE_AFTER
        finished_at = datetime.now(timezone.utc)
        async with async_session() as db:
            result = await db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.status == "approved",
                    ApprovalRequest.execution_status == "executing",
                    ApprovalRequest.execution_claimed_at < cutoff,
                )
                .values(
                    execution_status="ambiguous",
                    execution_finished_at=finished_at,
                    execution_error_code="StaleExecutionClaim",
                )
                .returning(ApprovalRequest.id)
            )
            stale_ids = list(result.scalars().all())
            for approval_id in stale_ids:
                approval = (
                    await db.execute(
                        select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
                    )
                ).scalar_one()
                db.add(
                    AuditLog(
                        agent_id=approval.agent_id,
                        action="approval_execution_ambiguous",
                        details={
                            "approval_id": str(approval.id),
                            "error_code": "StaleExecutionClaim",
                        },
                    )
                )
                await self._project_ambiguous_external_state(
                    db,
                    approval,
                    error_code="StaleExecutionClaim",
                )
            count = len(stale_ids)
            await db.commit()
        for approval_id in stale_ids:
            await self._notify_execution_terminal_safely(
                approval_id,
                status="ambiguous",
            )
        if count:
            logger.error("Approval stale claims fenced ambiguous count={}", count)
        return count

    async def process_next_pending_approval(self) -> bool:
        """Find the oldest durable execution request and attempt one claim."""

        async with async_session() as db:
            result = await db.execute(
                select(ApprovalRequest.id)
                .where(
                    ApprovalRequest.status == "approved",
                    ApprovalRequest.execution_status == "pending",
                    ApprovalRequest.execution_attempts == 0,
                    or_(
                        ApprovalRequest.execution_not_before.is_(None),
                        ApprovalRequest.execution_not_before <= datetime.now(timezone.utc),
                    ),
                )
                .order_by(ApprovalRequest.resolved_at, ApprovalRequest.id)
                .limit(1)
            )
            approval_id = result.scalar_one_or_none()
        if approval_id is None:
            return False
        return await self.execute_pending_approval(approval_id)

    async def process_pending_approval_batch(
        self,
        *,
        limit: int = APPROVAL_EXECUTION_CONCURRENCY,
    ) -> int:
        """Run a small bounded batch so one slow tenant cannot block all others."""

        bounded_limit = max(1, min(limit, APPROVAL_EXECUTION_CONCURRENCY))
        async with async_session() as db:
            tenant_rank = func.row_number().over(
                partition_by=Agent.tenant_id,
                order_by=(ApprovalRequest.resolved_at, ApprovalRequest.id),
            ).label("tenant_rank")
            eligible = (
                select(
                    ApprovalRequest.id.label("approval_id"),
                    ApprovalRequest.resolved_at.label("resolved_at"),
                    tenant_rank,
                )
                .join(Agent, Agent.id == ApprovalRequest.agent_id)
                .where(
                    ApprovalRequest.status == "approved",
                    ApprovalRequest.execution_status == "pending",
                    ApprovalRequest.execution_attempts == 0,
                    or_(
                        ApprovalRequest.execution_not_before.is_(None),
                        ApprovalRequest.execution_not_before
                        <= datetime.now(timezone.utc),
                    ),
                )
                .subquery()
            )
            result = await db.execute(
                select(eligible.c.approval_id)
                .order_by(
                    eligible.c.tenant_rank,
                    eligible.c.resolved_at,
                    eligible.c.approval_id,
                )
                .limit(bounded_limit)
            )
            approval_ids = list(result.scalars().all())
        if not approval_ids:
            return 0
        outcomes = await asyncio.gather(
            *(self.execute_pending_approval(approval_id) for approval_id in approval_ids),
            return_exceptions=True,
        )
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        if failures:
            raise RuntimeError(
                f"{len(failures)} approval execution task(s) failed"
            ) from failures[0]
        return sum(bool(outcome) for outcome in outcomes)

    async def _notify_creator(self, db: AsyncSession, agent: Agent,
                               action_type: str, details: dict) -> None:
        """Send L2 notification to agent creator via Feishu + web."""
        # Web notification (always)
        from app.services.notification_service import send_notification
        await send_notification(
            db,
            user_id=agent.creator_id,
            type="autonomy_l2",
            title=f"[{agent.name}] executed: {action_type}",
            body=json.dumps(privacy_safe_shape(details), ensure_ascii=False)[:200],
            link=f"/agents/{agent.id}#activityLog",
        )

        # Try Feishu notification if channel is configured
        channel_result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "feishu",
            )
        )
        channel = channel_result.scalar_one_or_none()

        if channel and channel.app_id and channel.app_secret:
            creator_result = await db.execute(
                select(User).where(User.id == agent.creator_id)
            )
            creator = creator_result.scalar_one_or_none()
            if creator:
                from app.models.identity import IdentityProvider
                from app.models.org import OrgMember

                provider_r = await db.execute(
                    select(IdentityProvider).where(
                        IdentityProvider.provider_type == "feishu",
                        IdentityProvider.tenant_id == creator.tenant_id,
                    )
                )
                provider = provider_r.scalar_one_or_none()
                if provider:
                    member_r = await db.execute(
                        select(OrgMember).where(
                            OrgMember.user_id == creator.id,
                            OrgMember.provider_id == provider.id,
                        )
                    )
                    member = member_r.scalar_one_or_none()
                    if member and (member.external_id or member.open_id):
                        receive_id = member.external_id or member.open_id
                        id_type = "user_id" if member.external_id else "open_id"
                        await feishu_service.send_message(
                            channel.app_id, channel.app_secret,
                            receive_id, "text",
                            json.dumps({"text": f"[{agent.name}] executed: {action_type}"}),
                            receive_id_type=id_type,
                        )

    async def _request_approval(self, db: AsyncSession, agent: Agent,
                                 approval: ApprovalRequest) -> None:
        """Durably create the request notification, then try external delivery.

        The approval row and in-app notification must commit before any Feishu
        side effect.  Otherwise a later database failure could leave a phantom
        card whose approval ID never existed.  Feishu is best-effort after the
        durable in-app request and cannot roll that request back.
        """
        preview = public_approval_details(
            agent.id,
            approval.action_type,
            approval.details,
        )
        preview_text = json.dumps(preview, ensure_ascii=False)[:1000]
        # Web notification (always)
        from app.services.notification_service import send_notification
        await send_notification(
            db,
            user_id=agent.creator_id,
            type="approval_pending",
            title=f"[{agent.name}] requests approval: {approval.action_type}",
            body=preview_text,
            link=f"/agents/{agent.id}#approvals",
            ref_id=approval.id,
        )

        # This service is called with a dedicated approval session.  Commit the
        # durable request and web notification before crossing the network.
        await db.commit()

        # Try Feishu notification only after the commit.  A delivery failure is
        # visible in logs while the already-persisted web approval remains the
        # source of truth.
        try:
            channel_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent.id,
                    ChannelConfig.channel_type == "feishu",
                )
            )
            channel = channel_result.scalar_one_or_none()

            if channel and channel.app_id and channel.app_secret:
                creator_result = await db.execute(
                    select(User).where(User.id == agent.creator_id)
                )
                creator = creator_result.scalar_one_or_none()
                if creator:
                    from app.models.identity import IdentityProvider
                    from app.models.org import OrgMember

                    provider_r = await db.execute(
                        select(IdentityProvider).where(
                            IdentityProvider.provider_type == "feishu",
                            IdentityProvider.tenant_id == creator.tenant_id,
                        )
                    )
                    provider = provider_r.scalar_one_or_none()
                    if provider:
                        member_r = await db.execute(
                            select(OrgMember).where(
                                OrgMember.user_id == creator.id,
                                OrgMember.provider_id == provider.id,
                            )
                        )
                        member = member_r.scalar_one_or_none()
                        if member and (member.external_id or member.open_id):
                            receive_id = member.external_id or member.open_id
                            await feishu_service.send_approval_card(
                                channel.app_id,
                                channel.app_secret,
                                receive_id,
                                agent.name,
                                approval.action_type,
                                preview_text,
                                str(approval.id),
                            )
        except Exception as exc:
            logger.warning(
                "Approval Feishu notification failed after durable commit "
                "approval={} error_type={}",
                approval.id,
                type(exc).__name__,
            )

autonomy_service = AutonomyService()


async def start_approval_execution_daemon() -> None:
    """Continuously execute durable approvals on the dedicated worker role."""

    if not APPROVAL_AUTOMATIC_EXECUTION_ENABLED:
        logger.info("Approval automatic execution is paused by release policy")
        while True:
            await asyncio.sleep(3600)

    logger.info(
        "Approval execution daemon started poll_interval={}s stale_after={}s",
        APPROVAL_EXECUTION_POLL_SECONDS,
        int(APPROVAL_EXECUTION_STALE_AFTER.total_seconds()),
    )
    last_reconcile = datetime.min.replace(tzinfo=timezone.utc)
    consecutive_failures = 0
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now - last_reconcile >= timedelta(minutes=1):
                await autonomy_service.reconcile_stale_executions()
                last_reconcile = now
            processed = await autonomy_service.process_pending_approval_batch()
            consecutive_failures = 0
            if processed == 0:
                await asyncio.sleep(APPROVAL_EXECUTION_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            logger.error(
                "Approval execution daemon iteration failed error_type={} consecutive_failures={}",
                type(exc).__name__,
                consecutive_failures,
            )
            if consecutive_failures >= APPROVAL_DAEMON_MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError("Approval execution daemon exceeded its failure threshold") from exc
            await asyncio.sleep(APPROVAL_EXECUTION_POLL_SECONDS)
