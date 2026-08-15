"""Durable system-email delivery, privacy and local SMTP contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
import socketserver
import threading
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.outbound_email_crypto import (
    open_outbound_email_payload,
    seal_outbound_email_payload,
)
from app.models.outbound_email import OutboundEmailDelivery
from app.services import outbound_email_service
from app.services.system_email_service import SystemEmailConfig


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RecordingDB:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        now = datetime.now(UTC)
        for value in self.added:
            value.id = value.id or uuid.uuid4()
            value.created_at = value.created_at or now
            value.updated_at = value.updated_at or now


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 local-capture ESMTP\r\n")
        self.wfile.flush()
        in_data = False
        message = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if in_data:
                if line in {b".\r\n", b".\n"}:
                    self.server.messages.append(bytes(message))  # type: ignore[attr-defined]
                    self.wfile.write(b"250 2.0.0 captured\r\n")
                    self.wfile.flush()
                    in_data = False
                    continue
                message.extend(line)
                continue
            command = line.decode("utf-8", errors="replace").strip().upper()
            if command.startswith("EHLO"):
                self.wfile.write(b"250-local-capture\r\n250 SIZE 10485760\r\n")
            elif command.startswith("HELO"):
                self.wfile.write(b"250 local-capture\r\n")
            elif command.startswith("MAIL FROM") or command.startswith("RCPT TO"):
                self.wfile.write(b"250 2.1.0 ok\r\n")
            elif command == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                in_data = True
                message = bytearray()
            elif command == "QUIT":
                self.wfile.write(b"221 2.0.0 bye\r\n")
                self.wfile.flush()
                break
            else:
                self.wfile.write(b"250 ok\r\n")
            self.wfile.flush()


class _CaptureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SMTPHandler)
        self.messages: list[bytes] = []


class _SharedDeliverySession:
    def __init__(self, delivery):
        self.delivery = delivery

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    @asynccontextmanager
    async def begin(self):
        yield self

    async def execute(self, _statement):
        return _ScalarResult(self.delivery)


def _delivery(payload: dict[str, str]) -> OutboundEmailDelivery:
    now = datetime.now(UTC)
    return OutboundEmailDelivery(
        id=uuid.uuid4(),
        purpose="password_reset",
        recipient_hash="a" * 64,
        recipient_mask="a***@example.com",
        payload_envelope=seal_outbound_email_payload(payload),
        status="queued",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )


def test_outbound_email_envelope_authenticates_ciphertext(monkeypatch):
    monkeypatch.setattr(
        "app.core.outbound_email_crypto.get_settings",
        lambda: SimpleNamespace(SECRET_KEY="unit-test-envelope-key"),
    )
    payload = {
        "to": "alice@example.com",
        "subject": "Reset",
        "body": "token=super-secret-reset-token",
    }
    envelope = seal_outbound_email_payload(payload)

    assert "super-secret-reset-token" not in envelope
    assert open_outbound_email_payload(envelope) == payload
    signature_index = len("enc:outbound-email:v1:")
    replacement = "0" if envelope[signature_index] != "0" else "1"
    tampered = envelope[:signature_index] + replacement + envelope[signature_index + 1 :]
    with pytest.raises(ValueError, match="authentication failed"):
        open_outbound_email_payload(tampered)


@pytest.mark.asyncio
async def test_enqueue_without_config_is_durable_and_never_exposes_payload(monkeypatch):
    monkeypatch.setattr(
        "app.core.outbound_email_crypto.get_settings",
        lambda: SimpleNamespace(SECRET_KEY="unit-test-envelope-key"),
    )
    monkeypatch.setattr(
        outbound_email_service,
        "render_email_template",
        AsyncMock(return_value=("Invite", "https://app.example/invite?code=SECRET")),
    )
    monkeypatch.setattr(
        outbound_email_service,
        "resolve_email_config_async",
        AsyncMock(return_value=None),
    )
    db = _RecordingDB()

    delivery = await outbound_email_service.enqueue_template_email(
        db,
        purpose="company_invitation",
        to="Alice@Example.com",
        scenario_key="company_invitation",
        variables={},
        tenant_id=uuid.uuid4(),
    )

    assert delivery.status == "blocked_configuration"
    assert delivery.last_error_code == "email_configuration_unavailable"
    assert delivery.next_attempt_at is not None
    assert delivery.recipient_mask == "a***@example.com"
    assert delivery.recipient_hash != "alice@example.com"
    assert "SECRET" not in delivery.payload_envelope
    public = outbound_email_service.delivery_public_payload(delivery)
    assert public and "payload_envelope" not in public


@pytest.mark.asyncio
async def test_dispatch_reaches_real_local_smtp_capture_and_records_only_smtp_acceptance(monkeypatch):
    monkeypatch.setattr(
        "app.core.outbound_email_crypto.get_settings",
        lambda: SimpleNamespace(SECRET_KEY="unit-test-envelope-key"),
    )
    server = _CaptureServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = SystemEmailConfig(
            from_address="system@example.com",
            from_name="Astra",
            smtp_host="127.0.0.1",
            smtp_port=server.server_address[1],
            smtp_username="",
            smtp_password="",
            smtp_ssl=False,
            smtp_timeout_seconds=5,
        )
        delivery = _delivery(
            {
                "to": "alice@example.com",
                "subject": "Reset your password",
                "body": "https://app.example/reset-password?token=LOCAL-CAPTURE-TOKEN",
            }
        )
        monkeypatch.setattr(
            outbound_email_service,
            "resolve_email_config_async",
            AsyncMock(return_value=config),
        )
        monkeypatch.setattr(
            outbound_email_service,
            "async_session",
            lambda: _SharedDeliverySession(delivery),
        )

        result = await outbound_email_service.dispatch_outbound_email(delivery.id)

        assert result == "smtp_accepted"
        assert delivery.status == "smtp_accepted"
        assert delivery.smtp_accepted_at is not None
        assert delivery.transport_receipt["evidence_level"] == "smtp_accepted"
        assert "recipient_delivered" not in delivery.transport_receipt
        assert len(server.messages) == 1
        captured = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        captured_body = captured.get_body(preferencelist=("plain",)).get_content()
        assert "LOCAL-CAPTURE-TOKEN" in captured_body
        assert "LOCAL-CAPTURE-TOKEN" not in delivery.payload_envelope
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_dispatch_redacts_transport_failure_and_schedules_retry(monkeypatch):
    monkeypatch.setattr(
        "app.core.outbound_email_crypto.get_settings",
        lambda: SimpleNamespace(SECRET_KEY="unit-test-envelope-key"),
    )
    delivery = _delivery(
        {"to": "alice@example.com", "subject": "Subject", "body": "body SECRET-TOKEN"}
    )
    config = SystemEmailConfig(
        from_address="system@example.com",
        from_name="Astra",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="system@example.com",
        smtp_password="SMTP-SECRET",
        smtp_ssl=True,
        smtp_timeout_seconds=5,
    )
    monkeypatch.setattr(
        outbound_email_service,
        "resolve_email_config_async",
        AsyncMock(return_value=config),
    )
    monkeypatch.setattr(
        outbound_email_service,
        "_send_email_with_config_sync",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("SMTP-SECRET SECRET-TOKEN")),
    )
    monkeypatch.setattr(
        outbound_email_service,
        "async_session",
        lambda: _SharedDeliverySession(delivery),
    )

    result = await outbound_email_service.dispatch_outbound_email(delivery.id)

    assert result == "retry_wait"
    assert delivery.status == "retry_wait"
    assert delivery.last_error_code == "smtp_transport_unavailable"
    assert "SECRET" not in delivery.last_error_code
    assert delivery.next_attempt_at is not None
