"""Safe, non-generating verification for provider credential-pool entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import httpx

from app.services.llm.utils import get_credential_api_key
from app.services.volcengine_agent_plan import (
    PROVIDER as VOLCENGINE_AGENT_PLAN_PROVIDER,
    normalize_base_url as normalize_volcengine_agent_plan_base_url,
)


@dataclass(frozen=True)
class CredentialProbeRequest:
    url: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class CredentialVerificationResult:
    ok: bool
    provider_status: int | None = None
    model_count: int | None = None
    message: str | None = None


_OPENAI_COMPATIBLE_BASE_URLS = {
    "minimax": "https://api.minimaxi.com/v1",
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "kimi": "https://api.moonshot.cn/v1",
}


def _models_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("缺少供应商 base_url")
    return normalized if normalized.endswith("/models") else f"{normalized}/models"


def build_credential_probe_request(
    *,
    provider: str,
    base_url: str | None,
    api_key: str,
) -> CredentialProbeRequest:
    """Build a read-only model-list request without embedding the key in the URL."""

    normalized_provider = provider.strip().lower()
    if normalized_provider == "anthropic":
        return CredentialProbeRequest(
            url=_models_url(base_url or "https://api.anthropic.com/v1"),
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    if normalized_provider == "gemini":
        return CredentialProbeRequest(
            url=_models_url(base_url or "https://generativelanguage.googleapis.com/v1beta"),
            headers={"x-goog-api-key": api_key},
        )
    if normalized_provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        normalized_base = normalize_volcengine_agent_plan_base_url(base_url)
        return CredentialProbeRequest(
            url=f"{normalized_base}/contents/generations/tasks?page_num=1&page_size=1",
            headers={"Authorization": f"Bearer {api_key}"},
        )

    default_base_url = _OPENAI_COMPATIBLE_BASE_URLS.get(normalized_provider)
    if normalized_provider == "custom" and not base_url:
        raise ValueError("custom provider 必须配置 base_url 才能验证")
    return CredentialProbeRequest(
        url=_models_url(base_url or default_base_url or ""),
        headers={"Authorization": f"Bearer {api_key}"},
    )


def _model_count(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("data", "models"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    if isinstance(payload.get("items"), list):
        return len(payload["items"])
    return None


async def verify_provider_credential(credential) -> CredentialVerificationResult:
    """Verify a key with a read-only model-list request; never generates content."""

    try:
        request = build_credential_probe_request(
            provider=credential.provider,
            base_url=getattr(credential, "base_url", None),
            api_key=get_credential_api_key(credential),
        )
    except (TypeError, ValueError):
        return CredentialVerificationResult(ok=False, message="验证配置不完整")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(request.url, headers=dict(request.headers))
    except httpx.HTTPError:
        return CredentialVerificationResult(ok=False, message="无法连接供应商验证接口")

    if response.is_success:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return CredentialVerificationResult(
            ok=True,
            provider_status=response.status_code,
            model_count=_model_count(payload),
            message="验证成功",
        )
    return CredentialVerificationResult(
        ok=False,
        provider_status=response.status_code,
        message=f"供应商验证失败（HTTP {response.status_code}）",
    )
