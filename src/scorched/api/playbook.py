from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.playbook import get_playbook, update_playbook
from .deps import require_owner_pin

router = APIRouter(prefix="/playbook", tags=["playbook"])


class PlaybookResponse(BaseModel):
    version: int
    updated_at: str
    content: str


class PlaybookUpdateResponse(PlaybookResponse):
    pass


@router.get("", response_model=PlaybookResponse)
async def read_playbook(db: AsyncSession = Depends(get_db)):
    pb = await get_playbook(db)
    return PlaybookResponse(
        version=pb.version,
        updated_at=pb.updated_at.isoformat(),
        content=pb.content,
    )


@router.post("/update", response_model=PlaybookUpdateResponse, dependencies=[Depends(require_owner_pin)])
async def force_update_playbook(db: AsyncSession = Depends(get_db)):
    """Manually trigger a playbook update based on current closed trade history."""
    from ..tz import market_today
    pb = await update_playbook(db, market_today())
    return PlaybookUpdateResponse(
        version=pb.version,
        updated_at=pb.updated_at.isoformat(),
        content=pb.content,
    )
