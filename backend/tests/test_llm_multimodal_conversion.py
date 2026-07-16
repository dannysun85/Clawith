import pytest

from app.services.llm import caller
from app.services.llm.caller import _convert_messages_for_vision, validate_inline_media_payload
from app.services.llm.client import LLMMessage
from app.services.quota_guard import QuotaExceeded


def test_convert_image_and_video_markers_to_openai_compatible_parts():
    messages = [
        LLMMessage(
            role="user",
            content=(
                "[image_data:data:image/png;base64,QUJD]\n"
                "[video_data:data:video/mp4;base64,REVG]\n"
                "Describe the media."
            ),
        )
    ]

    converted = _convert_messages_for_vision(messages, supports_vision=True)
    parts = converted[0].content

    assert isinstance(parts, list)
    assert parts[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    assert parts[1] == {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,REVG"}}
    assert parts[2] == {"type": "text", "text": "Describe the media."}


def test_multimodal_conversion_preserves_interleaved_reference_semantics():
    messages = [
        LLMMessage(
            role="user",
            content=(
                "产品A"
                "[image_data:data:image/png;base64,QUFB]"
                "，风格参考"
                "[video_data:data:video/mp4;base64,QkJC]"
                "，产品B"
                "[image_data:data:image/jpeg;base64,Q0ND]"
                "，分别保持对应关系。"
            ),
        )
    ]

    converted = _convert_messages_for_vision(messages, supports_vision=True)

    assert converted[0].content == [
        {"type": "text", "text": "产品A"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUFB"}},
        {"type": "text", "text": "，风格参考"},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,QkJC"}},
        {"type": "text", "text": "，产品B"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,Q0ND"}},
        {"type": "text", "text": "，分别保持对应关系。"},
    ]


def test_text_only_fallback_strips_every_interleaved_media_marker():
    messages = [
        LLMMessage(
            role="user",
            content=(
                "A[video_data:data:video/mp4;base64,QkJC]"
                "B[image_data:data:image/png;base64,QUFB]C"
            ),
        )
    ]

    converted = _convert_messages_for_vision(messages, supports_vision=False)

    assert isinstance(converted[0].content, str)
    assert "[image_data:" not in converted[0].content
    assert "[video_data:" not in converted[0].content
    assert "ABC" in converted[0].content
    assert "1 张图片" in converted[0].content
    assert "1 个视频" in converted[0].content


def test_strip_video_marker_when_model_is_text_only():
    messages = [
        LLMMessage(
            role="user",
            content="[video_data:data:video/mp4;base64,REVG]\nDescribe the media.",
        )
    ]

    converted = _convert_messages_for_vision(messages, supports_vision=False)

    assert isinstance(converted[0].content, str)
    assert "[video_data:" not in converted[0].content
    assert "当前模型不支持多模态理解" in converted[0].content


def test_inline_media_payload_limit_is_checked_before_provider_call(monkeypatch):
    monkeypatch.setattr(caller, "MAX_INLINE_MEDIA_BASE64_CHARS", 3)

    with pytest.raises(QuotaExceeded) as exc_info:
        validate_inline_media_payload("[video_data:data:video/mp4;base64,REVG]")

    assert exc_info.value.quota_type == "media_payload"


def test_inline_media_payload_limit_ignores_plain_text(monkeypatch):
    monkeypatch.setattr(caller, "MAX_INLINE_MEDIA_BASE64_CHARS", 1)

    validate_inline_media_payload("ordinary chat content")
