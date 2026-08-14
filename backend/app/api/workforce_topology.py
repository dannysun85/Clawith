"""Company Overview workforce-topology API."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.workforce_topology import WorkforceTopologyOut
from app.services.workforce_topology import build_workforce_topology


router = APIRouter(prefix="/workforce", tags=["workforce-topology"])


@router.get("/topology", response_model=WorkforceTopologyOut)
async def get_workforce_topology(
    window_hours: int = Query(default=24, ge=1, le=168),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkforceTopologyOut:
    return await build_workforce_topology(
        db,
        user=current_user,
        window_hours=window_hours,
    )


__all__ = ["router"]
