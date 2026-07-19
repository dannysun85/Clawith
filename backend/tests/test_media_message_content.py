from app.services.media_message_content import (
    contains_inline_media,
    redact_inline_media_for_token_estimate,
    sanitize_inline_media_content,
)


def test_inline_image_and_video_are_removed_from_persisted_content():
    raw = (
        "[image_data:data:image/png;base64,QUJD]\n"
        "请分析产品\n"
        "[video_data:data:video/mp4;base64,REVG]"
    )

    saved = sanitize_inline_media_content(
        raw,
        display_content="[Attachment: product.png] [Attachment: demo.mp4]\n请分析产品",
        file_names=["workspace/uploads/product.png", "workspace/uploads/demo.mp4"],
    )

    assert saved == (
        "[file:product.png]\n"
        "[file:demo.mp4]\n"
        "[Attachment: product.png] [Attachment: demo.mp4]\n请分析产品"
    )
    assert "base64" not in saved
    assert not contains_inline_media(saved)


def test_channel_without_file_reference_keeps_typed_placeholders():
    saved = sanitize_inline_media_content(
        "[image_data:data:image/jpeg;base64,QUJD]\n"
        "[video_data:https://example.invalid/video.mp4]"
    )

    assert saved == "[image]\n[video]"


def test_filename_is_reduced_to_safe_basename_and_deduplicated():
    saved = sanitize_inline_media_content(
        "hello",
        file_names=["workspace/uploads/a.png", "../a.png", "bad]\nname.mp4"],
    )

    assert saved == "[file:a.png]\n[file:bad)name.mp4]\nhello"


def test_structured_filename_preserves_commas():
    saved = sanitize_inline_media_content(
        "hello",
        file_names=["workspace/uploads/report,final_4875d85abdb4.png"],
    )

    assert saved == "[file:report,final_4875d85abdb4.png]\nhello"


def test_token_estimate_redaction_keeps_text_but_omits_transport_bytes():
    serialized = (
        '{"content":"before [image_data:data:image/jpeg;base64,'
        + "A" * 200_000
        + '] after"}'
    )

    redacted = redact_inline_media_for_token_estimate(serialized)

    assert redacted == (
        '{"content":"before [image_data:data:media;base64,[omitted]] after"}'
    )
    assert len(redacted) < 100
