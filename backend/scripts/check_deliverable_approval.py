"""Exercise deliverable approval validation inside a rollback-only transaction."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from app.database import async_session
from app.models.deliverable import DeliverableRequest
from app.models.tenant import Tenant  # noqa: F401 - register FK target metadata
from app.services.deliverable_artifacts import (
    DeliverableArtifactError,
    approve_deliverable_artifacts,
)


async def check_approval(request_id: uuid.UUID) -> None:
    async with async_session() as db:
        request = await db.get(DeliverableRequest, request_id)
        if request is None:
            raise SystemExit(f"Deliverable request {request_id} was not found")
        try:
            artifacts = await approve_deliverable_artifacts(db, request=request)
        except DeliverableArtifactError as exc:
            print("REJECTED", exc.code, str(exc))
        else:
            print("APPROVABLE", *(artifact.artifact_type for artifact in artifacts))
        finally:
            await db.rollback()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("request_id", type=uuid.UUID)
    arguments = parser.parse_args()
    asyncio.run(check_approval(arguments.request_id))
