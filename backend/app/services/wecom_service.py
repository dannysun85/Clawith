"""WeCom (Enterprise WeChat) service for sending messages via Open API."""

import httpx
from loguru import logger


def normalize_wecom_agent_id(value: object) -> int | None:
    """Return a canonical positive WeCom application AgentID.

    WeCom expects an ASCII decimal integer in ``agentid``.  Keeping this
    parser in the provider service gives configuration, webhook replies, and
    proactive delivery one fail-closed contract.
    """
    agent_id_text = str(value or "").strip()
    if (
        not agent_id_text
        or not agent_id_text.isascii()
        or not agent_id_text.isdigit()
        or len(agent_id_text) > 64
    ):
        return None
    try:
        numeric_agent_id = int(agent_id_text)
    except (ValueError, OverflowError):
        # Python limits decimal integer conversion length.  Provider-facing
        # configuration must fail closed instead of surfacing a 500 response.
        return None
    return numeric_agent_id if numeric_agent_id > 0 else None


async def get_wecom_access_token(corp_id: str, secret: str) -> dict:
    """Get WeCom access_token using corp_id and secret.

    API: https://developer.work.weixin.qq.com/document/14403
    """
    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {
        "corpid": corp_id,
        "corpsecret": secret,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        data = resp.json()

        if data.get("errcode") == 0:
            return {
                "access_token": data.get("access_token"),
                "expires_in": data.get("expires_in"),
            }
        else:
            logger.error(
                "[WeCom] Failed to get access_token error_code={}",
                data.get("errcode", "unknown"),
            )
            return {"errcode": data.get("errcode"), "errmsg": data.get("errmsg")}


async def send_wecom_message(
    corp_id: str,
    secret: str,
    user_id: str,
    message: str,
    agent_id: str | int | None = None,
) -> dict:
    """Send a text message to a WeCom user.

    API: https://developer.work.weixin.qq.com/document/14404

    Args:
        corp_id: WeCom corp ID
        secret: WeCom app secret
        user_id: Recipient's user_id
        message: Message content
        agent_id: Positive numeric WeCom application AgentID

    Returns:
        Dict with errcode on success
    """
    # Validate operator-controlled configuration before making a provider call.
    numeric_agent_id = normalize_wecom_agent_id(agent_id)
    if numeric_agent_id is None:
        return {
            "errcode": -1,
            "errmsg": "a positive numeric agent_id is required for WeCom messages",
        }

    # 1. Get access token
    token_result = await get_wecom_access_token(corp_id, secret)
    access_token = token_result.get("access_token")

    if not access_token:
        return {"errcode": token_result.get("errcode", -1), "errmsg": "Failed to get access_token"}

    # 2. Send message via API
    url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
    params = {"access_token": access_token}

    payload = {
        "touser": user_id,
        "msgtype": "text",
        "agentid": numeric_agent_id,
        "text": {
            "content": message,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, params=params, json=payload)
        data = resp.json()

        if data.get("errcode") == 0:
            logger.info("[WeCom] Message sent")
            return data
        else:
            logger.error(
                "[WeCom] Failed to send message error_code={}",
                data.get("errcode", "unknown"),
            )
            return data
