"""Thin async client for Douyin official OpenAPI.

The client intentionally keeps endpoints centralized. API handlers, Agent tools,
and tests must not build official Douyin requests directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.services.douyin.errors import (
    DouyinAuthError,
    DouyinError,
    DouyinNotConfiguredError,
    DouyinOfficialError,
    DouyinPermissionError,
    DouyinRateLimitError,
)


class DouyinOpenAPIClient:
    """Official Douyin OpenAPI client with normalized errors."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        client_key: str | None = None,
        client_secret: str | None = None,
    ):
        settings = get_settings()
        self.client_key = client_key if client_key is not None else settings.DOUYIN_CLIENT_KEY
        self.client_secret = client_secret if client_secret is not None else settings.DOUYIN_CLIENT_SECRET
        self.base_url = (base_url or settings.DOUYIN_API_BASE_URL or "https://open.douyin.com").rstrip("/")
        self.timeout = settings.DOUYIN_REQUEST_TIMEOUT_SECONDS
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.client_key and self.client_secret)

    def _require_configured(self) -> None:
        if not self.configured:
            raise DouyinNotConfiguredError("Douyin OpenAPI credentials are not configured", code="not_configured")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        headers: dict[str, str] = {}
        if access_token:
            headers["access-token"] = access_token
        if form is not None:
            headers["content-type"] = "application/x-www-form-urlencoded"

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, follow_redirects=False)
            close_client = True

        try:
            response = await client.request(
                method,
                path,
                headers=headers,
                data=form,
                json=json_body,
                params=params,
            )
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise DouyinOfficialError("Douyin OpenAPI request timed out", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise DouyinOfficialError("Douyin OpenAPI request failed", code="network_error") from exc
        except ValueError as exc:
            raise DouyinOfficialError("Douyin OpenAPI returned non-JSON response", code="invalid_json") from exc
        finally:
            if close_client:
                await client.aclose()

        if response.status_code >= 500:
            raise DouyinOfficialError(
                "Douyin OpenAPI server error",
                code=response.status_code,
                status_code=response.status_code,
            )
        if response.status_code == 429:
            raise DouyinRateLimitError("Douyin OpenAPI rate limited the request", code="rate_limited", status_code=429)
        if response.status_code >= 400:
            raise DouyinOfficialError(
                "Douyin OpenAPI HTTP error",
                code=response.status_code,
                status_code=response.status_code,
            )

        self._raise_for_official_error(payload)
        return payload

    def _raise_for_official_error(self, payload: dict) -> None:
        data = payload.get("data") if isinstance(payload, dict) else None
        extra = payload.get("extra") if isinstance(payload, dict) else None
        code = None
        desc = None
        log_id = None
        for source in (data, extra, payload):
            if not isinstance(source, dict):
                continue
            if code in (None, 0, "0"):
                code = source.get("error_code", code)
            desc = source.get("description") or source.get("message") or desc
            log_id = source.get("log_id") or source.get("logid") or log_id

        if code in (None, 0, "0"):
            return

        message = desc or "Douyin OpenAPI returned an error"
        code_str = str(code)
        if code_str in {"10008", "2190008", "10010", "10007", "28001003", "28001008"}:
            raise DouyinAuthError(message, code=code_str, log_id=log_id)
        if code_str in {"2190004", "2190015", "2100004", "28001014", "28001018", "28001019"}:
            raise DouyinPermissionError(message, code=code_str, log_id=log_id)
        if code_str in {"2190008", "2100009", "2190016", "28003017"}:
            raise DouyinRateLimitError(message, code=code_str, log_id=log_id)
        raise DouyinOfficialError(message, code=code_str, log_id=log_id)

    async def exchange_code(self, code: str) -> dict:
        """Exchange OAuth code for user access token."""
        self._require_configured()
        payload = await self._request(
            "POST",
            "/oauth/access_token/",
            form={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        data = payload.get("data") or {}
        return self._normalize_token_payload(data)

    async def refresh_access_token(self, refresh_token: str) -> dict:
        """Refresh or extend the user access token."""
        self._require_configured()
        payload = await self._request(
            "POST",
            "/oauth/refresh_token/",
            form={
                "client_key": self.client_key,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        data = payload.get("data") or {}
        return self._normalize_token_payload(data)

    async def get_client_token(self) -> dict:
        """Fetch an app-level client_token for non-user-authorized APIs."""
        self._require_configured()
        payload = await self._request(
            "POST",
            "/oauth/client_token/",
            json_body={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "grant_type": "client_credential",
            },
        )
        data = payload.get("data") or {}
        token = data.get("access_token") or data.get("client_token")
        if not token:
            raise DouyinAuthError("Douyin client_token response is missing access_token")
        return {
            "client_token": token,
            "expires_in": int(data.get("expires_in") or 0),
        }

    async def get_open_ticket(self, client_token: str) -> dict:
        """Fetch open_ticket used for H5 share schema signing."""
        payload = await self._request("GET", "/open/getticket/", access_token=client_token)
        data = payload.get("data") or {}
        ticket = data.get("ticket") or data.get("open_ticket")
        if not ticket:
            raise DouyinOfficialError("Douyin open_ticket response is missing ticket")
        return {
            "ticket": ticket,
            "expires_in": int(data.get("expires_in") or 0),
        }

    async def create_share_id(self, client_token: str, *, need_callback: bool = True, default_hashtag: str | None = None) -> dict:
        """Create a one-hour share_id for H5/SDK user-confirmed publishing."""
        params: dict[str, Any] = {"need_callback": str(need_callback).lower()}
        if default_hashtag:
            params["default_hashtag"] = default_hashtag
        payload = await self._request("POST", "/share-id/", access_token=client_token, params=params)
        data = payload.get("data") or {}
        extra = payload.get("extra") or {}
        share_id = data.get("share_id")
        if not share_id:
            raise DouyinOfficialError("Douyin share_id response is missing share_id")
        return {
            "share_id": str(share_id),
            "official_log_id": extra.get("log_id") or extra.get("logid"),
            "expires_in": 3600,
        }

    async def get_user_info(self, access_token: str, open_id: str) -> dict:
        payload = await self._request(
            "GET",
            "/oauth/userinfo/",
            access_token=access_token,
            params={"open_id": open_id},
        )
        data = payload.get("data") or {}
        return {
            "open_id": data.get("open_id") or open_id,
            "union_id": data.get("union_id"),
            "nickname": data.get("nickname") or data.get("display_name"),
            "avatar_url": data.get("avatar") or data.get("avatar_url"),
            "account_type": data.get("account_type"),
        }

    async def create_video(self, access_token: str, payload: dict) -> dict:
        """Create a Douyin video from an official video_id already uploaded to Douyin."""
        official = await self._request("POST", "/video/create/", access_token=access_token, json_body=payload)
        data = official.get("data") or {}
        extra = official.get("extra") or {}
        return {
            "item_id": data.get("item_id"),
            "video_id": payload.get("video_id"),
            "official_log_id": extra.get("log_id") or extra.get("logid"),
            "official_error_code": str(data.get("error_code") or extra.get("error_code") or 0),
            "raw_status": "created_reviewing",
        }

    async def reply_comment(self, access_token: str, payload: dict) -> dict:
        official = await self._request("POST", "/video/comment/reply/", access_token=access_token, json_body=payload)
        data = official.get("data") or {}
        extra = official.get("extra") or {}
        return {
            "comment_id": payload.get("comment_id"),
            "reply_id": data.get("reply_id") or data.get("comment_id"),
            "official_log_id": extra.get("log_id") or extra.get("logid"),
            "official_error_code": str(data.get("error_code") or extra.get("error_code") or 0),
        }

    async def fetch_video_metrics(self, access_token: str, params: dict) -> dict:
        return await self._request("GET", "/data/external/item/base/", access_token=access_token, params=params)

    async def fetch_comments(self, access_token: str, params: dict) -> dict:
        return await self._request("GET", "/video/comment/list/", access_token=access_token, params=params)

    def _normalize_token_payload(self, data: dict) -> dict:
        now = datetime.now(timezone.utc)
        expires_in = int(data.get("expires_in") or 0)
        refresh_expires_in = int(data.get("refresh_expires_in") or 0)
        scopes = data.get("scope") or ""
        if isinstance(scopes, str):
            scope_list = [part.strip() for part in scopes.replace(" ", ",").split(",") if part.strip()]
        else:
            scope_list = list(scopes or [])
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        open_id = data.get("open_id")
        if not access_token or not refresh_token or not open_id:
            raise DouyinAuthError("Douyin token response is missing access_token, refresh_token, or open_id")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "open_id": open_id,
            "scope": scope_list,
            "access_token_expires_at": now + timedelta(seconds=expires_in) if expires_in else None,
            "refresh_token_expires_at": now + timedelta(seconds=refresh_expires_in) if refresh_expires_in else None,
        }


def summarize_error(exc: Exception) -> dict:
    if isinstance(exc, DouyinError):
        return exc.redacted_summary()
    return {"message": str(exc)[:300], "code": type(exc).__name__}
