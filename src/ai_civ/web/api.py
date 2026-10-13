"""HTTP API of the Mini App: ``/api/me`` plus the routers of the three screens (profile, constitution, schedule).
Every route that touches data depends on ``deps.authenticate`` (see there)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from ai_civ.db.models import CoreFact
from ai_civ.web import character, connections, constitution, dashboard, profile, reminders, schedule, tavern
from ai_civ.web.deps import DB, Auth


class LaunchInfo(BaseModel):
    chat_type: str | None
    start_param: str | None


class MeResponse(BaseModel):
    first_name: str  # from Telegram (signed), for the greeting when the profile has no name yet
    name: str | None  # Core fact "name" (what the avatar knows)
    address_as: str | None
    timezone: str
    launch: LaunchInfo


router = APIRouter(prefix="/api")


@router.get("/me", response_model=MeResponse)
async def me(ctx: Auth, db: DB) -> MeResponse:
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                select(CoreFact.key, CoreFact.value_text).where(
                    CoreFact.valid_to.is_(None), CoreFact.persona_id.is_(None), CoreFact.key.in_(("name", "address_as"))
                )
            )
        ).all()
    facts = {key: value for key, value in rows}
    return MeResponse(
        first_name=ctx.init.first_name,
        name=facts.get("name"),
        address_as=facts.get("address_as"),
        timezone=ctx.timezone,
        launch=LaunchInfo(chat_type=ctx.init.chat_type, start_param=ctx.init.start_param),
    )


router.include_router(profile.router)
router.include_router(constitution.router)
router.include_router(schedule.router)
router.include_router(reminders.router)
router.include_router(connections.router)
router.include_router(character.router)
router.include_router(tavern.router)
router.include_router(dashboard.router)
