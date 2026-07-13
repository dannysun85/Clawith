"""Douyin capability and policy helpers."""

from app.config import get_settings


CAPABILITY_SCOPE_GROUPS = {
    "data_read": {
        "label": "数据读取",
        "scopes": {"data.external.user", "data.external.item"},
    },
    "collaborative_publish": {
        "label": "协作发布",
        "scopes": {"h5.share", "aweme.share", "aweme.forward", "open.get.ticket"},
    },
    "direct_publish": {
        "label": "后台发布（专项）",
        "scopes": {"video.create", "video.create.bind"},
    },
    "comment_manage": {
        "label": "评论管理",
        "scopes": {"video.comment"},
    },
}


def configured_scopes() -> list[str]:
    settings = get_settings()
    raw = settings.DOUYIN_SCOPES or ""
    scopes = [part.strip() for part in raw.replace(" ", ",").split(",") if part.strip()]
    return list(dict.fromkeys(scopes))


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.DOUYIN_CLIENT_KEY and settings.DOUYIN_CLIENT_SECRET)


def callback_url() -> str:
    settings = get_settings()
    if settings.DOUYIN_REDIRECT_URI:
        return settings.DOUYIN_REDIRECT_URI
    public = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{public}/api/douyin/oauth/callback" if public else "/api/douyin/oauth/callback"


def capability_status(scopes: list[str] | set[str]) -> list[dict]:
    granted = set(scopes or [])
    rows = []
    for key, meta in CAPABILITY_SCOPE_GROUPS.items():
        required = set(meta["scopes"])
        has_any = bool(required & granted)
        rows.append(
            {
                "key": key,
                "label": meta["label"],
                "required_scopes": sorted(required),
                "status": "ready" if has_any else "missing",
            }
        )
    return rows


def has_capability(scopes: list[str] | set[str], capability: str) -> bool:
    meta = CAPABILITY_SCOPE_GROUPS.get(capability)
    if not meta:
        return False
    return bool(set(scopes or []) & set(meta["scopes"]))


def direct_publish_enabled() -> bool:
    return bool(get_settings().DOUYIN_DIRECT_PUBLISH_ENABLED)
