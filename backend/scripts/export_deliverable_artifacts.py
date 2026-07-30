"""Export immutable deliverable snapshots for local quality inspection."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.models.tenant import Tenant  # noqa: F401 - register FK target metadata
from app.services.deliverable_artifacts import read_deliverable_artifact_snapshot
from app.services.storage import get_storage_backend
from app.services.storage_runtime.agent_files import agent_storage_key


async def export_artifacts(
    request_id: uuid.UUID,
    output_dir: Path,
    workspace_paths: list[str],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    storage = get_storage_backend()
    async with async_session() as db:
        request = await db.get(DeliverableRequest, request_id)
        if request is None:
            raise SystemExit(f"Deliverable request not found: {request_id}")
        result = await db.execute(
            select(DeliverableArtifactRevision)
            .where(DeliverableArtifactRevision.request_id == request_id)
            .order_by(
                DeliverableArtifactRevision.artifact_type,
                DeliverableArtifactRevision.revision_number,
            )
        )
        artifacts = list(result.scalars())
        if not artifacts and not workspace_paths:
            raise SystemExit(f"No artifacts registered for request {request_id}")
        for artifact in artifacts:
            data = await read_deliverable_artifact_snapshot(storage, artifact=artifact)
            destination = output_dir / Path(artifact.workspace_path).name
            destination.write_bytes(data)
            print(
                artifact.artifact_type,
                artifact.revision_number,
                artifact.status,
                destination,
                len(data),
                artifact.content_hash,
            )
        for workspace_path in workspace_paths:
            data = await storage.read_bytes(
                agent_storage_key(request.agent_id, workspace_path)
            )
            destination = output_dir / Path(workspace_path).name
            destination.write_bytes(data)
            print("workspace", destination, len(data))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("request_id", type=uuid.UUID)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--workspace-path",
        action="append",
        default=[],
        help="Also export an Agent workspace file such as workspace/.../slides.html",
    )
    arguments = parser.parse_args()
    asyncio.run(
        export_artifacts(
            arguments.request_id,
            arguments.output_dir,
            arguments.workspace_path,
        )
    )
