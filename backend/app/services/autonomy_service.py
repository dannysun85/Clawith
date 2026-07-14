"""Autonomy boundary enforcement service.

Implements the three-level autonomy system:
  L1 — Auto-execute, notify creator
  L2 — Notify creator, auto-execute
  L3 — Require explicit approval before execution
"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import privacy_safe_shape
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.feishu_service import feishu_service


HIGH_RISK_DEFAULT_L3_ACTIONS = {
    "douyin_publish_job",
    "douyin_reply_comment",
    "douyin_external_write",
    "execute_code",
}


def _canonical_tool_arguments(arguments: dict) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_approval_signature(agent_id: uuid.UUID, tool_name: str, arguments: dict) -> str:
    from app.config import get_settings

    payload = f"v1\n{agent_id}\n{tool_name}\n{_canonical_tool_arguments(arguments)}"
    return hmac.new(
        get_settings().SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_tool_approval_details(
    agent_id: uuid.UUID,
    tool_name: str,
    arguments: dict,
    requested_by: uuid.UUID,
) -> dict:
    """Bind the complete immutable tool payload to an HMAC for approval."""

    from app.config import get_settings
    from app.core.security import encrypt_data

    canonical_arguments = _canonical_tool_arguments(arguments)

    return {
        "payload_version": 2,
        "tool": tool_name,
        "args_encrypted": encrypt_data(
            canonical_arguments,
            get_settings().SECRET_KEY,
        ),
        "args_hash": hashlib.sha256(
            canonical_arguments.encode("utf-8")
        ).hexdigest(),
        "args_signature": _tool_approval_signature(agent_id, tool_name, arguments),
        "request_shape": privacy_safe_shape(arguments),
        "requested_by": str(requested_by),
    }


def _verified_tool_arguments(agent_id: uuid.UUID, details: dict) -> tuple[str, dict]:
    from app.config import get_settings
    from app.core.security import decrypt_data

    tool_name = details.get("tool")
    encrypted_arguments = details.get("args_encrypted")
    expected_hash = details.get("args_hash")
    signature = details.get("args_signature")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("Approved tool payload is missing the tool name")
    if not isinstance(encrypted_arguments, str) or not encrypted_arguments:
        raise ValueError("Approved tool payload is incomplete; request a new approval")
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
    actual_hash = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()
    if not isinstance(expected_hash, str) or not hmac.compare_digest(
        expected_hash,
        actual_hash,
    ):
        raise ValueError("Approved tool payload integrity check failed")
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature,
        _tool_approval_signature(agent_id, tool_name, arguments),
    ):
        raise ValueError("Approved tool payload integrity check failed")
    return tool_name, arguments


class AutonomyService:
    """Enforce autonomy boundaries for agent operations."""

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
            # Create approval request and block
            approval = ApprovalRequest(
                agent_id=agent.id,
                action_type=action_type,
                details=details,
            )
            db.add(approval)
            await db.flush()

            logger.info(f"L3: Approval required for {action_type} by agent {agent.id}")
            await self._request_approval(db, agent, approval)

            return {
                "allowed": False,
                "level": "L3",
                "approval_id": str(approval.id),
                "message": "Approval requested from creator",
            }

        return {"allowed": False, "level": "unknown", "message": "Unknown autonomy level"}

    async def resolve_approval(
        self, db: AsyncSession, approval_id: uuid.UUID, user: User, action: str
    ) -> ApprovalRequest:
        """Approve or reject a pending approval request."""
        result = await db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id)
            .with_for_update()
        )
        approval = result.scalar_one_or_none()
        if not approval:
            raise ValueError("Approval not found")

        if approval.status != "pending":
            raise ValueError("Approval already resolved")

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

        if action == "approve" and approval.details and approval.details.get("tool"):
            _verified_tool_arguments(approval.agent_id, approval.details)

        approval.status = "approved" if action == "approve" else "rejected"
        approval.resolved_at = datetime.now(timezone.utc)
        approval.resolved_by = user.id

        # Log
        db.add(AuditLog(
            user_id=user.id,
            agent_id=approval.agent_id,
            action=f"approval_{approval.status}",
            details={"approval_id": str(approval.id), "action_type": approval.action_type},
        ))

        # Post-processing: execute the approved action
        execution_result = None
        if approval.status == "approved" and approval.details:
            execution_result = await self._execute_approved_action(
                approval.agent_id, approval.action_type, approval.details
            )
            logger.info(
                "Post-approval execution action_type={} result_shape={}",
                approval.action_type,
                privacy_safe_shape(execution_result),
            )

        # Web notification to agent creator about the result
        if agent:
            from app.services.notification_service import send_notification
            status_label = "approved" if approval.status == "approved" else "rejected"
            body_text = json.dumps(
                privacy_safe_shape(approval.details),
                ensure_ascii=False,
            )[:200]
            if execution_result:
                body_text = f"Result: {execution_result}"
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
        return approval

    async def _execute_approved_action(
        self, agent_id: uuid.UUID, action_type: str, details: dict
    ) -> str | None:
        """Execute the tool action that was approved.

        Reads the tool name and arguments from the approval details,
        then directly calls the tool executor (bypassing autonomy check).
        """
        tool_name = str(details.get("tool") or "unknown")
        try:
            tool_name, arguments = _verified_tool_arguments(agent_id, details)

            # Import and call the tool's direct executor (no autonomy re-check)
            from app.services.agent_tools import _execute_tool_direct
            result = await _execute_tool_direct(tool_name, arguments, agent_id)
            return result
        except Exception as e:
            logger.error(f"Failed to execute approved action {tool_name}: {e}")
            return f"Execution failed: {e}"

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
            select(ChannelConfig).where(ChannelConfig.agent_id == agent.id)
        )
        channel = channel_result.scalars().first()

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
        """Send L3 approval request to creator via Feishu card + web notification."""
        # Web notification (always)
        from app.services.notification_service import send_notification
        await send_notification(
            db,
            user_id=agent.creator_id,
            type="approval_pending",
            title=f"[{agent.name}] requests approval: {approval.action_type}",
            body=json.dumps(privacy_safe_shape(approval.details), ensure_ascii=False)[:200],
            link=f"/agents/{agent.id}#approvals",
            ref_id=approval.id,
        )

        # Try Feishu notification
        channel_result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.agent_id == agent.id)
        )
        channel = channel_result.scalars().first()

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
                            channel.app_id, channel.app_secret,
                            receive_id,
                            agent.name, approval.action_type,
                            json.dumps(privacy_safe_shape(approval.details), ensure_ascii=False),
                            str(approval.id),
                        )


autonomy_service = AutonomyService()
