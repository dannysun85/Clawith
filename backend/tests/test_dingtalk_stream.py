from types import SimpleNamespace

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
