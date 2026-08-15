"""Exercise durable email persistence against PostgreSQL and a local SMTP capture."""

from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
import socketserver
import threading
import uuid

from sqlalchemy import delete, select

from app.database import async_session
import app.models.identity_governance  # noqa: F401 - register referenced tables
import app.models.tenant  # noqa: F401 - register referenced tables
import app.models.user  # noqa: F401 - register referenced tables
from app.models.outbound_email import OutboundEmailDelivery
from app.services import outbound_email_service
from app.services.system_email_service import SystemEmailConfig


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"220 local-capture ESMTP\r\n")
        self.wfile.flush()
        in_data = False
        message = bytearray()
        while line := self.rfile.readline():
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
            elif command.startswith(("MAIL FROM", "RCPT TO")):
                self.wfile.write(b"250 2.1.0 ok\r\n")
            elif command == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                in_data = True
                message = bytearray()
            elif command == "QUIT":
                self.wfile.write(b"221 2.0.0 bye\r\n")
                self.wfile.flush()
                return
            else:
                self.wfile.write(b"250 ok\r\n")
            self.wfile.flush()


class _CaptureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SMTPHandler)
        self.messages: list[bytes] = []


async def _run(server: _CaptureServer) -> None:
    marker = f"LOCAL-POSTGRES-SMTP-{uuid.uuid4().hex}"
    idempotency_key = f"smoke:{uuid.uuid4()}"
    delivery_id: uuid.UUID | None = None
    config = SystemEmailConfig(
        from_address="system@example.test",
        from_name="Astra local smoke",
        smtp_host="127.0.0.1",
        smtp_port=server.server_address[1],
        smtp_username="",
        smtp_password="",
        smtp_ssl=False,
        smtp_timeout_seconds=5,
    )

    async def _local_config(**_kwargs) -> SystemEmailConfig:
        return config

    original_resolver = outbound_email_service.resolve_email_config_async
    outbound_email_service.resolve_email_config_async = _local_config
    try:
        async with async_session() as db:
            delivery = await outbound_email_service.enqueue_template_email(
                db,
                purpose="password_reset",
                to="smtp-smoke@example.test",
                scenario_key="password_reset",
                variables={
                    "display_name": "Local smoke",
                    "reset_url": f"https://local.invalid/reset-password?token={marker}",
                    "expiry_minutes": "5",
                },
                idempotency_key=idempotency_key,
            )
            delivery_id = delivery.id
            assert marker not in delivery.payload_envelope
            await db.commit()

        assert await outbound_email_service.dispatch_outbound_email(delivery_id) == "smtp_accepted"

        async with async_session() as db:
            persisted = (
                await db.execute(
                    select(OutboundEmailDelivery).where(OutboundEmailDelivery.id == delivery_id)
                )
            ).scalar_one()
            assert persisted.status == "smtp_accepted"
            assert persisted.attempt_count == 1
            assert persisted.smtp_accepted_at is not None
            assert persisted.transport_receipt["evidence_level"] == "smtp_accepted"
            assert marker not in persisted.payload_envelope

        assert len(server.messages) == 1
        captured = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        body = captured.get_body(preferencelist=("plain",)).get_content()
        assert marker in body
    finally:
        outbound_email_service.resolve_email_config_async = original_resolver
        if delivery_id is not None:
            async with async_session() as db:
                await db.execute(
                    delete(OutboundEmailDelivery).where(OutboundEmailDelivery.id == delivery_id)
                )
                await db.commit()


def main() -> None:
    server = _CaptureServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(_run(server))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("outbound_email_postgres_smtp_smoke=passed")


if __name__ == "__main__":
    main()
