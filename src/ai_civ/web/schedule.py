"""Screen "Расписание": the HARD schedule (``civ.events``), created only here or confirmed in the bot, never invented by the model.

  * a recurring event is ONE row with an RRULE (``recurrence``: validated here, expanded in the event's own time zone);
  * one occurrence of a series is removed by an EXCEPTION row (``parent_event_id`` + ``original_start``, status ``cancelled``);
  * editing / deleting uses optimistic concurrency: the page sends the ``updated_at`` it saw (``expected_updated_at``), a stale
    page gets ``409 conflict``; the row is locked ``FOR UPDATE`` so two parallel requests are ordered, not interleaved;
  * a change of start / zone / rule of a series drops its exception rows (they point at occurrences that no longer exist);
  * time zones: the event keeps the zone it was planned in; the response says when that differs from the profile's zone
    (``timezone_mismatch``) so the UI can warn instead of silently shifting somebody's day.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.bot.personas import clean_line
from ai_civ.db.enums import EventStatus, EventVisibility
from ai_civ.db.models import Event, Persona, User
from ai_civ.web import recurrence
from ai_civ.web.deps import DB, ApiError, Auth, conflict
from ai_civ.web.profile import valid_iana_zone

router = APIRouter(tags=["schedule"])

MAX_WINDOW = timedelta(days=400)
MAX_DURATION = timedelta(days=31)
MAX_TITLE, MAX_DESCRIPTION = 200, 2000
CREATED_VIA = "mini_app"

EventId = Annotated[uuid.UUID, Path()]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time with a UTC offset is required (e.g. 2026-10-03T09:00:00+02:00)")
    return value


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=MAX_TITLE * 3)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION * 2)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    timezone: str = Field(max_length=64)
    visibility: EventVisibility = EventVisibility.busy_only
    persona_id: uuid.UUID | None = None
    recurrence_rule: str | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = clean_line(value, MAX_TITLE)
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return None if value is None or not value.strip() else value.strip()[:MAX_DESCRIPTION]

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        return valid_iana_zone(value)

    @model_validator(mode="after")
    def _consistent(self) -> EventIn:
        check_period(self.starts_at, self.ends_at)
        if self.recurrence_rule is not None:
            self.recurrence_rule = check_rule(self.recurrence_rule, self.starts_at, self.timezone)
        return self


class EventPatch(BaseModel):
    """Only the fields that are SENT change (``recurrence_rule: null`` removes the repetition)."""

    model_config = ConfigDict(extra="forbid")

    expected_updated_at: datetime
    title: str | None = Field(default=None, max_length=MAX_TITLE * 3)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION * 2)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    all_day: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)
    visibility: EventVisibility | None = None
    persona_id: uuid.UUID | None = None
    recurrence_rule: str | None = None

    @field_validator("expected_updated_at", "starts_at", "ends_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_line(value, MAX_TITLE)
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        return None if value is None else valid_iana_zone(value)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_start: datetime  # the ``original_start`` of the occurrence as the page received it

    @field_validator("original_start")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return _aware(value)


class OccurrenceOverride(BaseModel):
    """Change ONE occurrence of a series (an exception row). Only what is sent differs from the series; the time zone and the
    repetition belong to the whole series and cannot be sent here. Moving: send ``starts_at`` (the duration is kept unless
    ``ends_at`` is sent too)."""

    model_config = ConfigDict(extra="forbid")

    original_start: datetime  # which occurrence: its ``original_start`` as the page received it
    title: str | None = Field(default=None, max_length=MAX_TITLE * 3)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION * 2)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    all_day: bool | None = None
    visibility: EventVisibility | None = None
    persona_id: uuid.UUID | None = None  # null removes the topic from this occurrence

    @field_validator("original_start", "starts_at", "ends_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_line(value, MAX_TITLE)
        if not value:
            raise ValueError("must not be blank")
        return value


def check_period(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ValueError("the event must end after it starts")
    if ends_at - starts_at > MAX_DURATION:
        raise ValueError("an event longer than 31 days is not an event")


def check_rule(raw: str, starts_at: datetime, timezone: str) -> str:
    try:
        return recurrence.validate(raw, starts_at, timezone)
    except recurrence.RuleError as exc:
        raise ValueError(str(exc)) from None


class EventOut(BaseModel):
    id: uuid.UUID  # the row: the series master for generated occurrences
    series_id: uuid.UUID | None  # exception rows: the series they belong to (``id`` is then the exception's own row)
    kind: str  # "single" | "series" (a generated occurrence of a recurring event) | "exception" (a changed occurrence)
    title: str
    description: str | None
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    timezone: str
    status: str
    visibility: str
    persona_id: uuid.UUID | None
    recurrence_rule: str | None  # only on the master / single rows
    original_start: datetime | None  # series / exception: which occurrence of the series this is (UTC instant)
    series_starts_at: datetime  # start of the ROW itself (the first occurrence of a series): the edit form of a series needs it
    updated_at: datetime  # of the ROW: what the page sends back as ``expected_updated_at``
    timezone_mismatch: bool  # the event's zone differs from the profile's zone


class EventsOut(BaseModel):
    profile_timezone: str
    occurrences: list[EventOut]


class PatchResult(BaseModel):
    event: EventOut
    exceptions_reset: int  # cancelled occurrences dropped because the series start / zone / rule changed


class CancelResult(BaseModel):
    created: bool  # True = the occurrence was removed by this call; False = it already was


class PersonaOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def to_out(row: Event, *, kind: str, profile_tz: str, starts_at: datetime | None = None, original_start: datetime | None = None) -> EventOut:
    start = row.starts_at if starts_at is None else starts_at
    return EventOut(
        id=row.id,
        series_id=row.parent_event_id,
        kind=kind,
        title=row.title,
        description=row.description,
        starts_at=_utc(start),
        ends_at=_utc(start + (row.ends_at - row.starts_at)),
        all_day=row.all_day,
        timezone=row.timezone,
        status=row.status.value,
        visibility=row.visibility.value,
        persona_id=row.persona_id,
        recurrence_rule=row.recurrence_rule,
        original_start=None if original_start is None else _utc(original_start),
        series_starts_at=_utc(row.starts_at),
        updated_at=row.updated_at,
        timezone_mismatch=row.timezone != profile_tz,
    )


async def _profile_tz(session: AsyncSession, fallback: str) -> str:
    return (await session.execute(select(User.timezone))).scalar_one_or_none() or fallback


async def _check_persona(session: AsyncSession, persona_id: uuid.UUID | None) -> None:
    if persona_id is None:
        return
    found = (await session.execute(select(Persona.id).where(Persona.id == persona_id, Persona.is_active.is_(True)))).scalar_one_or_none()
    if found is None:
        raise ApiError(422, "invalid_request", "Такой темы нет.")


async def _lock(session: AsyncSession, event_id: uuid.UUID) -> Event:
    row = (await session.execute(select(Event).where(Event.id == event_id).with_for_update())).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "Такого события уже нет.")
    return row


def _expect(row: Event, expected: datetime) -> None:
    if row.updated_at != expected:
        raise conflict("Событие только что изменилось. Обновил данные — проверьте и повторите.", current_updated_at=row.updated_at.isoformat())


# ---------------------------------------------------------------- reading


@router.get("/personas", response_model=list[PersonaOut])
async def list_personas(ctx: Auth, db: DB) -> list[PersonaOut]:
    """Topics an event can belong to (a workout -> the self-development topic)."""
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = (await session.execute(select(Persona).where(Persona.is_active.is_(True)).order_by(Persona.title))).scalars().all()
    return [PersonaOut(id=p.id, code=p.code, title=p.title) for p in rows]


@router.get("/events", response_model=EventsOut)
async def list_events(
    ctx: Auth,
    db: DB,
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
) -> EventsOut:
    """Occurrences overlapping [start, end): single events, generated occurrences of series (minus cancelled ones), exceptions."""
    for value in (start, end):
        if value.tzinfo is None:
            raise ApiError(422, "invalid_request", "Время нужно передавать со смещением, например 2026-10-03T00:00:00Z.")
    if end <= start or end - start > MAX_WINDOW:
        raise ApiError(422, "invalid_request", "Окно должно быть от одного дня до 400 дней.")
    async with db.tenant_session(ctx.tenant_id) as session:
        profile_tz = await _profile_tz(session, ctx.timezone)
        singles = (
            await session.execute(
                select(Event).where(
                    Event.parent_event_id.is_(None), Event.recurrence_rule.is_(None), Event.starts_at < end, Event.ends_at > start
                )
            )
        ).scalars().all()
        masters = (
            await session.execute(
                select(Event).where(Event.parent_event_id.is_(None), Event.recurrence_rule.is_not(None), Event.starts_at < end)
            )
        ).scalars().all()
        exception_rows: list[Event] = []
        if masters:
            exception_rows = list(
                (await session.execute(select(Event).where(Event.parent_event_id.in_([m.id for m in masters])))).scalars()
            )

    out: list[EventOut] = [to_out(e, kind="single", profile_tz=profile_tz) for e in singles]
    replaced = {(e.parent_event_id, _utc(e.original_start)) for e in exception_rows if e.original_start is not None}
    for master in masters:
        assert master.recurrence_rule is not None
        try:
            starts = recurrence.occurrences(
                master.recurrence_rule, master.starts_at, master.timezone, master.ends_at - master.starts_at, start, end
            )
        except (recurrence.RuleError, ValueError, OverflowError):
            continue  # a rule that no longer expands (written by something else) hides that series instead of breaking the page
        for moment in starts:
            if (master.id, _utc(moment)) not in replaced:
                out.append(to_out(master, kind="series", profile_tz=profile_tz, starts_at=moment, original_start=moment))
    for exc in exception_rows:
        if exc.status is not EventStatus.cancelled and exc.starts_at < end and exc.ends_at > start:
            out.append(to_out(exc, kind="exception", profile_tz=profile_tz, original_start=exc.original_start))
    out.sort(key=lambda o: (o.starts_at, o.title))
    return EventsOut(profile_timezone=profile_tz, occurrences=out)


# ---------------------------------------------------------------- writing


@router.post("/events", response_model=EventOut, status_code=201)
async def create_event(body: EventIn, ctx: Auth, db: DB) -> EventOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        await _check_persona(session, body.persona_id)
        row = Event(
            user_id=ctx.tenant_id,
            persona_id=body.persona_id,
            title=body.title,
            description=body.description,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            all_day=body.all_day,
            timezone=body.timezone,
            visibility=body.visibility,
            recurrence_rule=body.recurrence_rule,
            created_via=CREATED_VIA,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return to_out(row, kind="series" if row.recurrence_rule else "single", profile_tz=await _profile_tz(session, ctx.timezone))


@router.patch("/events/{event_id}", response_model=PatchResult)
async def patch_event(event_id: EventId, body: EventPatch, ctx: Auth, db: DB) -> PatchResult:
    sent = body.model_fields_set - {"expected_updated_at"}
    if not sent:
        raise ApiError(422, "invalid_request", "Нечего менять.")
    async with db.tenant_session(ctx.tenant_id) as session:
        row = await _lock(session, event_id)
        _expect(row, body.expected_updated_at)
        if row.parent_event_id is not None:
            return await _patch_exception(session, row, body, sent, ctx.timezone)
        for name in ("title", "all_day", "timezone", "starts_at", "ends_at", "visibility"):
            if name in sent and getattr(body, name) is None:
                raise ApiError(422, "invalid_request", f"Поле {name} нельзя очистить.")
        before = (row.starts_at, row.timezone, row.recurrence_rule)
        values = {name: getattr(body, name) for name in sent if name not in {"recurrence_rule", "description"}}
        if "description" in sent:
            values["description"] = None if not (body.description or "").strip() else body.description.strip()[:MAX_DESCRIPTION]
        starts_at = values.get("starts_at", row.starts_at)
        ends_at = values.get("ends_at", row.ends_at)
        timezone = values.get("timezone", row.timezone)
        rule = (
            row.recurrence_rule
            if "recurrence_rule" not in sent
            else (None if body.recurrence_rule is None else body.recurrence_rule)
        )
        try:
            check_period(starts_at, ends_at)
            if rule is not None:
                rule = check_rule(rule, starts_at, timezone)
        except ValueError as exc:
            raise ApiError(422, "invalid_request", str(exc)) from None
        values["recurrence_rule"] = rule
        if "persona_id" in sent:
            await _check_persona(session, body.persona_id)
        for name, value in values.items():
            setattr(row, name, value)
        reset = 0
        if (starts_at, timezone, rule) != before:  # the occurrences the exceptions point at may not exist any more
            reset = (await session.execute(delete(Event).where(Event.parent_event_id == row.id))).rowcount or 0
        await session.flush()
        await session.refresh(row)
        profile_tz = await _profile_tz(session, ctx.timezone)
        return PatchResult(event=to_out(row, kind="series" if row.recurrence_rule else "single", profile_tz=profile_tz), exceptions_reset=reset)


async def _patch_exception(session: AsyncSession, row: Event, body: EventPatch, sent: set[str], profile_tz_fallback: str) -> PatchResult:
    """Edit one CHANGED occurrence (an exception row). The zone and the repetition belong to the whole series."""
    if sent & {"timezone", "recurrence_rule"}:
        raise ApiError(400, "exception_field", "Часовой пояс и повторение меняются у всей серии, а не у одного повторения.")
    if row.status is EventStatus.cancelled:
        raise ApiError(400, "occurrence_cancelled", "Это повторение убрано. Верните его и измените заново.")
    for name in ("title", "all_day", "starts_at", "ends_at", "visibility"):
        if name in sent and getattr(body, name) is None:
            raise ApiError(422, "invalid_request", f"Поле {name} нельзя очистить.")
    values = {name: getattr(body, name) for name in sent if name != "description"}
    if "description" in sent:
        values["description"] = None if not (body.description or "").strip() else body.description.strip()[:MAX_DESCRIPTION]
    try:
        check_period(values.get("starts_at", row.starts_at), values.get("ends_at", row.ends_at))
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
    if "persona_id" in sent:
        await _check_persona(session, body.persona_id)
    for name, value in values.items():
        setattr(row, name, value)
    await session.flush()
    await session.refresh(row)
    return PatchResult(event=to_out(row, kind="exception", profile_tz=await _profile_tz(session, profile_tz_fallback), original_start=row.original_start), exceptions_reset=0)


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(event_id: EventId, ctx: Auth, db: DB, expected_updated_at: Annotated[datetime, Query()]) -> None:
    """Delete an event (a series with its exceptions, or a single event). Deleting one exception row restores that occurrence."""
    if expected_updated_at.tzinfo is None:
        raise ApiError(422, "invalid_request", "Время нужно передавать со смещением.")
    async with db.tenant_session(ctx.tenant_id) as session:
        row = await _lock(session, event_id)
        _expect(row, expected_updated_at)
        await session.execute(delete(Event).where(Event.id == row.id))  # exceptions go with it (ON DELETE CASCADE)


async def _series_occurrence(session: AsyncSession, event_id: uuid.UUID, original_start: datetime) -> Event:
    """The series master, locked (this serialises every change of one series' occurrences), after checking that
    ``original_start`` really is one of its occurrences."""
    master = await _lock(session, event_id)
    if master.recurrence_rule is None or master.parent_event_id is not None:
        raise ApiError(400, "not_a_series", "У этого события нет повторений.")
    if not recurrence.is_occurrence(master.recurrence_rule, master.starts_at, master.timezone, original_start):
        raise ApiError(422, "invalid_request", "В серии нет повторения в это время.")
    return master


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: EventId, ctx: Auth, db: DB) -> EventOut:
    """One row as it is stored: a single event, a series master (with its rule) or an exception row."""
    async with db.tenant_session(ctx.tenant_id) as session:
        row = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
        if row is None:
            raise ApiError(404, "not_found", "Такого события уже нет.")
        kind = "exception" if row.parent_event_id else ("series" if row.recurrence_rule else "single")
        return to_out(row, kind=kind, profile_tz=await _profile_tz(session, ctx.timezone), original_start=row.original_start)


@router.post("/events/{event_id}/occurrences/override", response_model=EventOut, status_code=201)
async def override_occurrence(event_id: EventId, body: OccurrenceOverride, ctx: Auth, db: DB) -> EventOut:
    """Change or move ONE occurrence of a series: an exception row (status planned) replaces it. Create-only: if that occurrence
    already has an exception (changed on another device, or removed) the answer is 409 and the page refreshes; an existing
    exception is edited with PATCH and its own ``expected_updated_at``."""
    sent = body.model_fields_set - {"original_start"}
    if not sent:
        raise ApiError(422, "invalid_request", "Нечего менять.")
    for name in ("title", "all_day", "starts_at", "ends_at", "visibility"):
        if name in sent and getattr(body, name) is None:
            raise ApiError(422, "invalid_request", f"Поле {name} нельзя очистить.")
    try:
        async with db.tenant_session(ctx.tenant_id) as session:
            master = await _series_occurrence(session, event_id, body.original_start)
            existing = (
                await session.execute(
                    select(Event).where(Event.parent_event_id == master.id, Event.original_start == body.original_start)
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise conflict(
                    "Это повторение уже изменено. Обновил данные — проверьте и повторите.",
                    exception_id=str(existing.id),
                    current_updated_at=existing.updated_at.isoformat(),
                )
            duration = master.ends_at - master.starts_at
            starts_at = body.starts_at or body.original_start
            ends_at = body.ends_at or (starts_at + duration)
            try:
                check_period(starts_at, ends_at)
            except ValueError as exc:
                raise ApiError(422, "invalid_request", str(exc)) from None
            persona_id = body.persona_id if "persona_id" in sent else master.persona_id
            await _check_persona(session, persona_id)
            description = master.description
            if "description" in sent:
                description = None if not (body.description or "").strip() else body.description.strip()[:MAX_DESCRIPTION]
            row = Event(
                user_id=ctx.tenant_id,
                persona_id=persona_id,
                title=body.title or master.title,
                description=description,
                starts_at=starts_at,
                ends_at=ends_at,
                all_day=master.all_day if body.all_day is None else body.all_day,
                timezone=master.timezone,
                status=EventStatus.planned,
                recurrence_rule=None,
                parent_event_id=master.id,
                original_start=body.original_start,
                visibility=body.visibility or master.visibility,
                created_via=CREATED_VIA,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return to_out(row, kind="exception", profile_tz=await _profile_tz(session, ctx.timezone), original_start=row.original_start)
    except IntegrityError:  # the unique index on (series, original_start): impossible under the series lock, kept as a seat belt
        raise conflict("Это повторение уже изменено. Обновил данные — проверьте и повторите.") from None


@router.post("/events/{event_id}/occurrences/cancel", response_model=CancelResult)
async def cancel_occurrence(event_id: EventId, body: CancelIn, ctx: Auth, db: DB) -> CancelResult:
    """Remove ONE occurrence of a series: an exception row (status cancelled) takes its place; an occurrence that was changed
    before is removed too (its exception turns cancelled). Idempotent: ``created`` says whether this call removed it."""
    async with db.tenant_session(ctx.tenant_id) as session:
        master = await _series_occurrence(session, event_id, body.original_start)  # serialises concurrent changes of the series
        inserted = (
            await session.execute(
                pg_insert(Event)
                .values(
                    user_id=ctx.tenant_id,
                    persona_id=master.persona_id,
                    title=master.title,
                    description=master.description,
                    starts_at=body.original_start,
                    ends_at=body.original_start + (master.ends_at - master.starts_at),
                    all_day=master.all_day,
                    timezone=master.timezone,
                    status=EventStatus.cancelled,
                    recurrence_rule=None,
                    parent_event_id=master.id,
                    original_start=body.original_start,
                    visibility=master.visibility,
                    created_via=CREATED_VIA,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "parent_event_id", "original_start"],
                    index_where=text("parent_event_id IS NOT NULL"),
                    set_={"status": EventStatus.cancelled},
                    where=Event.status != EventStatus.cancelled,  # already removed: no row comes back, nothing is touched
                )
                .returning(Event.id)
            )
        ).scalar_one_or_none()
        return CancelResult(created=inserted is not None)
