from types import SimpleNamespace

from app.api.dingtalk import _append_missing_image_markers
from app.services.dingtalk_stream import _handler_error_ack


def test_dingtalk_handler_error_ack_keeps_status_without_exception_text():
    stream_module = SimpleNamespace(
        AckMessage=SimpleNamespace(STATUS_SYSTEM_EXCEPTION="SYSTEM_EXCEPTION")
    )
    provider_secret = "provider exception secret=must-not-survive"

    status, message = _handler_error_ack(stream_module)

    assert status == "SYSTEM_EXCEPTION"
    assert message == "handler error"
    assert provider_secret not in message


def test_dingtalk_image_marker_is_injected_exactly_once():
    data_url = "data:image/jpeg;base64,QUJD"
    existing = f"[User sent an image]\n[image_data:{data_url}]"

    result = _append_missing_image_markers(existing, [data_url])

    assert result.count(f"[image_data:{data_url}]") == 1


def test_dingtalk_image_marker_is_added_when_processor_text_has_none():
    data_url = "data:image/jpeg;base64,QUJD"

    result = _append_missing_image_markers("hello", [data_url])

    assert result == f"hello\n[image_data:{data_url}]"
