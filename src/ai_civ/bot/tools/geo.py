"""Geo notes and places (v2.0, PR 4.4, migration 0021): ``save_geo_note`` + the handler of a sent location.

What the bot can and cannot do (it never knows where the user IS unless the user sends a location): a note is kept for a PLACE ("магазин", "зал", "офис");
when the user sends a location (a point on the map or the current position) the code measures the distance to every place of the user (haversine, plain Python,
no GIS) and shows the active notes of the place the user is in, each with a button «✅ Выполнено». Nothing is promised that the bot cannot do: no automatic
reminder fires by itself, the card says "покажу, когда пришлёшь геопозицию".

The division of labour is the one of every tool here (the model proposes, the code decides):
  * ``save_geo_note(place, content)`` is OFFERED only when CODE finds a location-bound reminder in the user's own words ("напомни в магазине купить X",
    "заметка для зала: ...", "когда буду в офисе — забрать документы"): ``wants_geo_note``;
  * the place must be the user's own word (inflection tolerant: «в магазине» ~ «магазин»), the note must be grounded in the message (every number stands in it,
    every longer word starts like a word of it: the model may not invent what to remember), and it is bound to a saved place by name or synonym, otherwise it
    keeps the name and is bound the first time that place is saved (``/place Название`` as a REPLY to a location, or the button «💾 Это «магазин»» under a
    location that matches no place);
  * what the user reads is a fixed text written by code; the model's words next to the call are never sent.
A point note is made by ``/geonote текст`` as a reply to a location. Coordinates are stored only for places and notes the user made on purpose; a bare location
message is never stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import diplomat, texts
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult, UserTurn
from ai_civ.bot.tools.workout import norm_key, stated_numbers
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.models import GeoNote, Place
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "save_geo_note"
EARTH_RADIUS_M = 6_371_008.8
MAX_CONTENT_CHARS = 200
MAX_PLACE_CHARS = 60
MAX_PENDING_BUTTONS = 3
MIN_RADIUS, MAX_RADIUS = 10, 5000
SIX_PLACES = Decimal("0.000001")

# ---------------------------------------------------------------- distance (pure Python, no GIS)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The great-circle distance in metres between two points given in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def coordinate(value: Any, limit: int) -> Decimal | None:
    """A latitude / longitude as Telegram sends it, rounded to the 6 decimals the database keeps; None if it is not a finite number inside ``±limit``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = Decimal(str(value))
    if not number.is_finite() or abs(number) > limit:
        return None
    return number.quantize(SIX_PLACES)


# ---------------------------------------------------------------- names (inflection tolerant, code not model)

_ENDINGS = frozenset(
    {"", "а", "я", "у", "ю", "ы", "и", "е", "ь", "й", "ом", "ем", "ой", "ей", "ов", "ев", "ами", "ах", "ё"}
    | {"ый", "ий", "ого", "его", "ому", "ему", "ым", "им", "ая", "яя", "ую", "юю", "ое", "ее", "ые", "ие", "ых", "их", "ок", "ка", "ку", "ке", "ком", "ки"}
)


def same_word(a: str, b: str) -> bool:
    """One word in two cases of the Russian declension («магазин» ~ «магазине», «зал» ~ «зале», «дом» ~ «домой»): the shorter is the stem of the longer (3+ letters)
    or they share a stem of 4+ letters, and what is left of each is a case ending."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 3 and long_.startswith(short) and long_[len(short) :] in _ENDINGS:
        return True
    common = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        common += 1
    return common >= 4 and a[common:] in _ENDINGS and b[common:] in _ENDINGS


def same_place(a: str, b: str) -> bool:
    """Two normal-form (``norm_key``) place names that mean the same: the same words, each in any case."""
    left, right = a.split(), b.split()
    return bool(left) and len(left) == len(right) and all(same_word(x, y) for x, y in zip(left, right, strict=True))


def _in_text(phrase: str, text_key: str) -> bool:
    words = text_key.split()
    return bool(phrase) and all(any(same_word(part, word) for word in words) for part in phrase.split())


def name_hash(name: str) -> str:
    return hashlib.sha1(norm_key(name).encode("utf-8")).hexdigest()[:8]


def _matches(place: Place, wanted_key: str) -> bool:
    return any(same_place(norm_key(label), wanted_key) for label in (place.name, *place.aliases))


# ---------------------------------------------------------------- the cue check (code, not prompt)

_PLACE_WORD = r"(?:в|на|у|около|возле|рядом\W+с|поблизости\W+от)\W+[^\W\d]\w+"
_CUES = re.compile(
    rf"\bнапомн\w*\W+(?:\w+\W+){{0,4}}?{_PLACE_WORD}"
    rf"|\bзаметк\w*\W+(?:\w+\W+){{0,2}}?(?:для|на|в)\W+[^\W\d]\w+"
    rf"|\bкогда\W+(?:\w+\W+){{0,2}}?(?:буду|окажусь|приду|приеду|окажемся|будем|зайду|зайдём|зайдем)\W+(?:\w+\W+){{0,2}}?{_PLACE_WORD}"
    rf"|\b(?:как\W+только|когда)\W+(?:\w+\W+){{0,2}}?(?:приду|приеду|окажусь|зайду)\b"
    r"|\bremind\W+me\W+(?:\w+\W+){0,3}?(?:at|in|near|when)\b"
    r"|\bwhen\W+i(?:\W+am|'m|\W+get|\W+arrive)\W+(?:\w+\W+){0,2}?(?:at|to|in)\b",
    re.IGNORECASE,
)


def wants_geo_note(message: str) -> bool:
    """True when the USER's own words ask for a note that belongs to a place. ``ё`` is folded into ``е``."""
    return _CUES.search(message.replace("ё", "е").replace("Ё", "Е")) is not None


# ---------------------------------------------------------------- the function the model sees

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Save a note that belongs to a PLACE because the user asked to be reminded there ('напомни в магазине купить молоко', 'заметка для зала: проверить вес на жиме', "
        "'когда буду в офисе - забрать документы'). `place` is the place as the user named it (a short noun, e.g. 'магазин', 'зал', 'офис'), `content` is WHAT to remember, "
        "copied from the user's words. Never invent or add anything the user did not say; every number must be one the user wrote. You cannot know where the user is: "
        "the SYSTEM shows the note when the user sends a location, and tells the user so. Call it once per note."
    ),
    parameters={
        "type": "object",
        "properties": {
            "place": {"type": "string", "description": f"The place the user named, a short noun phrase in the user's language, at most {MAX_PLACE_CHARS} characters."},
            "content": {"type": "string", "description": f"What to remember there, one short line in the user's words, at most {MAX_CONTENT_CHARS} characters."},
        },
        "required": ["place", "content"],
    },
)

INSTRUCTIONS = (
    "## Place notes\n"
    "The user may be asking to be reminded of something at a place: then call save_geo_note (once per note) with the place and what to remember, in the user's own words.\n"
    "You cannot see where the user is and nothing fires by itself: the bot shows the note when the user SENDS a location near the place. The SYSTEM tells the user\n"
    "exactly that in a card under your reply. Do not promise that you will remind or notify them, never say it was saved or recorded, and do not repeat the note:\n"
    "reply briefly (one or two sentences)."
)


def render_note(saved: Sequence[SavedNote]) -> str:
    lines = [
        "## Place notes (made by the system)",
        "The system has saved the note(s) and tells the user how they will be shown (when a location is sent near the place) in a card right under your reply.",
        "Do not repeat the note, do not say it was saved or recorded and do not promise to remind: reply briefly to the user's message.",
    ]
    lines += [f"- {item.place}" for item in saved]
    return "\n".join(lines)


# ---------------------------------------------------------------- validating a call


@dataclass(frozen=True)
class NoteSpec:
    place: str
    content: str


def _one_line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())
    if not line or len(line) > limit or re.search(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩﻿]", line):
        return None
    return line


_FILLER = frozenset({"надо", "нужно", "чтобы", "когда", "если", "после", "перед", "также", "еще", "свой", "свою", "свои", "это", "этот", "эту", "там", "тут", "здесь", "пока", "нужна", "нужен", "привет", "здравствуй", "будущий", "будущая", "будущее", "дорогой", "дорогая"})


def grounded(content: str, user_text: str, share: float = 1.0) -> bool:
    """The note says only what the user said: every number of it stands in the message, and every word of 4+ letters (but the few filler words a note may add) starts
    (its first 4 letters) like a word of the message."""
    said = stated_numbers(user_text)
    if not stated_numbers(content) <= said:
        return False
    starts = {word[:4] for word in norm_key(user_text).split()}
    words = [word for word in norm_key(content).split() if len(word) >= 4 and not word.isdigit() and word not in _FILLER]
    return sum(1 for word in words if word[:4] in starts) >= share * len(words)  # ``share`` < 1 lets a longer text carry a greeting the user did not write


def parse_note(args: dict[str, Any], user_text: str) -> NoteSpec | None:
    """None = the call is unusable and dropped silently: an unreadable place or content, a place that is not the user's own word, a note that says more than the user said."""
    place = _one_line(args.get("place"), MAX_PLACE_CHARS)
    content = _one_line(args.get("content"), MAX_CONTENT_CHARS)
    if place is None or content is None:
        return None
    place_key = norm_key(place)
    if not place_key or not any(ch.isalpha() for ch in content) or not _in_text(place_key, norm_key(user_text)):
        return None
    if not grounded(content, user_text):
        return None
    return NoteSpec(place, content)


# ---------------------------------------------------------------- the cards (what the user reads: written by code)

NOTE_CALLBACK = "gn"
PLACE_CALLBACK = "gs"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NOTE_RE = re.compile(rf"^gn:({_UUID}):d$")
PLACE_RE = re.compile(r"^gs:([0-9a-f]{8}):(-?\d{1,9}):(-?\d{1,9})$")


def note_data(note_id: uuid.UUID) -> str:
    return f"{NOTE_CALLBACK}:{note_id}:d"  # 41 bytes of Telegram's 64


def parse_note_data(data: str) -> uuid.UUID | None:
    match = NOTE_RE.match(data)
    return None if match is None else uuid.UUID(match.group(1))


def place_data(name: str, latitude: Decimal, longitude: Decimal) -> str:
    return f"{PLACE_CALLBACK}:{name_hash(name)}:{int(latitude * 1_000_000)}:{int(longitude * 1_000_000)}"  # at most 3+8+1+10+1+11 = 34 bytes


def parse_place_data(data: str) -> tuple[str, Decimal, Decimal] | None:
    match = PLACE_RE.match(data)
    if match is None:
        return None
    latitude, longitude = coordinate(Decimal(match.group(2)) / 1_000_000, 90), coordinate(Decimal(match.group(3)) / 1_000_000, 180)
    return None if latitude is None or longitude is None else (match.group(1), latitude, longitude)


@dataclass(frozen=True)
class NoteView:
    id: uuid.UUID
    content: str


@dataclass(frozen=True)
class Located:
    place: str | None  # the place the user is in (None: none matched)
    notes: tuple[NoteView, ...]
    more: int  # notes that were not shown (over the limit)
    pending: tuple[str, ...]  # names of notes still waiting for their place (only when no place matched)


def note_lines(notes: Sequence[NoteView], more: int) -> list[str]:
    lines = [f"{n}. {note.content}" for n, note in enumerate(notes, 1)]
    if more:
        lines.append(texts.GEO_MORE.format(n=more))
    return lines


def done_markup(notes: Sequence[NoteView], extra: Sequence[InlineKeyboardButton] = ()) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(text=texts.GEO_DONE_BUTTON.format(n=n), callback_data=note_data(note.id))] for n, note in enumerate(notes, 1)]
    rows += [[button] for button in extra]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def location_card(located: Located, latitude: Decimal, longitude: Decimal) -> tuple[str, InlineKeyboardMarkup | None]:
    if located.place is not None and located.notes:
        return "\n".join([texts.GEO_NEAR.format(place=located.place), *note_lines(located.notes, located.more)]), done_markup(located.notes)
    if located.place is not None:
        return texts.GEO_HERE.format(place=located.place), None
    if located.notes:
        return "\n".join([texts.GEO_NEAR_NOTES, *note_lines(located.notes, located.more)]), done_markup(located.notes)
    buttons = [
        InlineKeyboardButton(text=texts.GEO_SAVE_BUTTON.format(name=name), callback_data=place_data(name, latitude, longitude))
        for name in located.pending[:MAX_PENDING_BUTTONS]
    ]
    return texts.GEO_UNKNOWN, (InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]) if buttons else None)


# ---------------------------------------------------------------- the engine


@dataclass(frozen=True)
class SavedNote:
    place: str
    content: str
    bound: bool  # the place is saved: the note is bound to it already


@dataclass(frozen=True)
class PlaceSaved:
    outcome: str  # created | exists | limit | bad_name
    name: str = ""
    radius: int = 0
    bound: int = 0


@dataclass(frozen=True)
class Done:
    outcome: str  # done | settled | gone
    text: str | None = None
    markup: InlineKeyboardMarkup | None = None


def saved_text(item: SavedNote) -> str:
    text = texts.GEO_NOTE_SAVED.format(place=item.place, content=item.content)
    return text if item.bound else text + texts.GEO_NOTE_PLACE_UNKNOWN.format(place=item.place)


class Geo:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ the tool

    async def save_notes(self, tenant_id: uuid.UUID, turn: UserTurn, calls: Sequence[ToolCall]) -> list[SavedNote]:
        """The notes made for the usable calls of ONE message (at most ``GEO_MAX_CALLS``). Never raises (except cancellation): an unusable call is skipped,
        a database failure rolls the whole message back and gives an empty list (logged). A replayed message gets its own notes back (the card of a first
        attempt that died before it was sent), never a second copy."""
        specs: list[NoteSpec] = []
        for call in calls:
            if call.name != TOOL_NAME:
                continue
            try:
                spec = parse_note(call.args, turn.text)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("save_geo_note: unreadable arguments %r", call.args, exc_info=True)
                spec = None
            if spec is None:
                log.info("save_geo_note: dropped a call with unusable arguments")
                continue
            if all((norm_key(spec.place), norm_key(spec.content)) != (norm_key(s.place), norm_key(s.content)) for s in specs):
                specs.append(spec)
            if len(specs) == self.settings.geo_max_calls:
                break
        if not specs:
            return []
        try:
            return await asyncio.wait_for(self._save_notes(tenant_id, turn, specs), timeout=self.settings.geo_timeout_seconds)
        except Exception:  # noqa: BLE001 - nothing saved, no card: the reply still goes out
            log.warning("save_geo_note: could not save the notes of tenant %s", tenant_id, exc_info=True)
            return []

    async def _save_notes(self, tenant_id: uuid.UUID, turn: UserTurn, specs: Sequence[NoteSpec]) -> list[SavedNote]:
        async with self.db.tenant_session(tenant_id) as session:
            earlier = (await session.execute(select(GeoNote).where(GeoNote.source_message_id == turn.message_id))).scalars().all()
            if earlier:
                return [SavedNote(n.place_name or "", n.content, n.place_id is not None) for n in earlier if n.status == "active"]
            places = list((await session.execute(select(Place))).scalars().all())
            active = list((await session.execute(select(GeoNote).where(GeoNote.status == "active"))).scalars().all())
            saved: list[SavedNote] = []
            for spec in specs:
                if len(active) >= self.settings.geo_max_notes:
                    log.info("save_geo_note: tenant %s reached %d active notes", tenant_id, len(active))
                    break
                key = norm_key(spec.place)
                place = next((p for p in places if _matches(p, key)), None)
                label = place.name if place is not None else spec.place
                if any(norm_key(n.content) == norm_key(spec.content) and same_place(norm_key(n.place_name or ""), norm_key(label)) for n in active):
                    continue  # the same note is already waiting
                note = GeoNote(
                    user_id=tenant_id,
                    place_id=None if place is None else place.id,
                    place_name=label,
                    content=spec.content,
                    source_message_id=turn.message_id,
                )
                session.add(note)
                active.append(note)
                saved.append(SavedNote(label, spec.content, place is not None))
            await session.flush()
            return saved

    # ------------------------------------------------------------ a sent location

    async def locate(self, tenant_id: uuid.UUID, latitude: Decimal, longitude: Decimal) -> Located:
        """Which place is the user in, and which active notes belong here? Haversine in Python over the user's own places and notes (capped). The notes that are
        shown get ``notified_at``; a note that waited for its place by name is bound to the place the user is in when the names match. Raises on a database failure."""
        s = self.settings
        lat, lon = float(latitude), float(longitude)
        async with self.db.tenant_session(tenant_id) as session:
            places = (await session.execute(select(Place).limit(s.geo_max_places))).scalars().all()
            notes = (
                await session.execute(select(GeoNote).where(GeoNote.status == "active").order_by(GeoNote.created_at, GeoNote.id).limit(s.geo_max_notes).with_for_update())
            ).scalars().all()
            inside = sorted(
                ((haversine_m(lat, lon, float(p.latitude), float(p.longitude)), p) for p in places), key=lambda pair: pair[0]
            )
            here = next((p for distance, p in inside if distance <= p.radius_meters), None)
            listed: list[GeoNote] = []
            if here is not None:
                key = norm_key(here.name)
                for note in notes:
                    if note.place_id == here.id:
                        listed.append(note)
                    elif note.place_id is None and note.place_name and (same_place(norm_key(note.place_name), key) or _matches(here, norm_key(note.place_name))):
                        note.place_id = here.id  # the first time this place is met: the waiting note is bound to it
                        listed.append(note)
            for note in notes:
                if note not in listed and note.latitude is not None and haversine_m(lat, lon, float(note.latitude), float(note.longitude)) <= s.geo_note_radius_meters:
                    listed.append(note)
            shown = listed[: s.geo_max_listed]
            now = datetime.now(UTC)
            for note in shown:
                note.notified_at = now
            pending: tuple[str, ...] = ()
            if here is None and not listed:
                names: list[str] = []
                for note in notes:  # one name per place, whatever the case it was written in
                    if note.place_id is None and note.place_name and not any(same_place(norm_key(note.place_name), norm_key(n)) for n in names):
                        names.append(note.place_name)
                pending = tuple(names)
            await session.flush()
            return Located(here.name if here is not None else None, tuple(NoteView(n.id, n.content) for n in shown), len(listed) - len(shown), pending)

    # ------------------------------------------------------------ the buttons

    async def done(self, tenant_id: uuid.UUID, note_id: uuid.UUID) -> Done:
        """«✅ Выполнено»: the note is archived in ONE transaction under ``FOR UPDATE`` (idempotent); the card is rebuilt from the database (the notes of the same
        place, or near the same point, that are still active). Raises on a database failure: nothing was changed then."""
        s = self.settings
        async with self.db.tenant_session(tenant_id) as session:
            note = (await session.execute(select(GeoNote).where(GeoNote.id == note_id).with_for_update())).scalar_one_or_none()
            if note is None:
                return Done("gone")
            outcome = "settled"
            if note.status == "active":
                note.status, note.archived_at = "archived", datetime.now(UTC)
                await session.flush()
                outcome = "done"
            place = None
            if note.place_id is not None:
                place = (await session.execute(select(Place).where(Place.id == note.place_id))).scalar_one_or_none()
            active = (
                await session.execute(select(GeoNote).where(GeoNote.status == "active").order_by(GeoNote.created_at, GeoNote.id).limit(s.geo_max_notes))
            ).scalars().all()
            if place is not None:
                rest = [n for n in active if n.place_id == place.id]
                head = texts.GEO_NEAR.format(place=place.name)
            else:
                rest = [
                    n for n in active
                    if n.latitude is not None and note.latitude is not None
                    and haversine_m(float(note.latitude), float(note.longitude), float(n.latitude), float(n.longitude)) <= s.geo_note_radius_meters
                ]  # fmt: skip
                head = texts.GEO_NEAR_NOTES
            shown = [NoteView(n.id, n.content) for n in rest[: s.geo_max_listed]]
            if not shown:
                text = texts.GEO_ALL_DONE.format(place=place.name) if place is not None else texts.GEO_ALL_DONE.format(place="Заметки здесь")
                return Done(outcome, text, None)
            return Done(outcome, "\n".join([head, *note_lines(shown, len(rest) - len(shown))]), done_markup(shown))

    async def save_place(self, tenant_id: uuid.UUID, name: Any, latitude: Decimal, longitude: Decimal, radius: int | None = None) -> PlaceSaved:
        """Save a place the user named at the point they sent, and bind the notes that waited for it. ONE transaction. Raises on a database failure."""
        clean = _one_line(name, MAX_PLACE_CHARS)
        if clean is None or diplomat.clean_title(clean) is None or not any(ch.isalpha() for ch in clean):
            return PlaceSaved("bad_name")
        key = norm_key(clean)
        radius = radius if radius is not None and MIN_RADIUS <= radius <= MAX_RADIUS else None
        async with self.db.tenant_session(tenant_id) as session:
            places = (await session.execute(select(Place))).scalars().all()
            existing = next((p for p in places if _matches(p, key)), None)
            if existing is not None:
                return PlaceSaved("exists", existing.name)
            if len(places) >= self.settings.geo_max_places:
                return PlaceSaved("limit")
            values: dict[str, Any] = {"user_id": tenant_id, "name": clean, "latitude": latitude, "longitude": longitude}
            if radius is not None:
                values["radius_meters"] = radius
            place_id, used = (await session.execute(pg_insert(Place).values(**values).returning(Place.id, Place.radius_meters))).one()
            waiting = (await session.execute(select(GeoNote).where(GeoNote.status == "active", GeoNote.place_id.is_(None)).with_for_update())).scalars().all()
            bound = 0
            for note in waiting:
                if note.place_name and same_place(norm_key(note.place_name), key):
                    note.place_id, note.place_name = place_id, clean
                    bound += 1
            await session.flush()
            return PlaceSaved("created", clean, used, bound)

    async def save_waiting_place(self, tenant_id: uuid.UUID, hashed: str, latitude: Decimal, longitude: Decimal) -> PlaceSaved | None:
        """The button «💾 Это «магазин»»: the name comes from a waiting note (its hash is in the button), the point from the location the button was under."""
        async with self.db.tenant_session(tenant_id) as session:
            waiting = (await session.execute(select(GeoNote.place_name).where(GeoNote.status == "active", GeoNote.place_id.is_(None), GeoNote.place_name.is_not(None)))).scalars().all()
        name = next((n for n in waiting if name_hash(n) == hashed), None)
        return None if name is None else await self.save_place(tenant_id, name, latitude, longitude)

    async def save_point_note(self, tenant_id: uuid.UUID, message_id: uuid.UUID | None, content: str, latitude: Decimal, longitude: Decimal) -> str:
        """``/geonote`` on a location: a note on the point itself. ``saved | limit``. Raises on a database failure."""
        async with self.db.tenant_session(tenant_id) as session:
            active = (await session.execute(select(func.count()).select_from(GeoNote).where(GeoNote.status == "active"))).scalar_one()
            if active >= self.settings.geo_max_notes:
                return "limit"
            session.add(GeoNote(user_id=tenant_id, latitude=latitude, longitude=longitude, content=content, source_message_id=message_id))
            return "saved"

    # ------------------------------------------------------------ Telegram glue (called by the handlers)

    async def on_location(self, message: Message, tenant: Any) -> None:
        location = message.location
        latitude = coordinate(None if location is None else location.latitude, 90)
        longitude = coordinate(None if location is None else location.longitude, 180)
        if latitude is None or longitude is None:
            return
        located = await self.locate(tenant.id, latitude, longitude)
        text, markup = location_card(located, latitude, longitude)
        await message.answer(text, reply_markup=markup)

    async def on_place_command(self, message: Message, tenant: Any, args: str | None) -> None:
        point = _replied_point(message)
        if point is None:
            await message.answer(texts.GEO_NEED_REPLY)
            return
        name, radius = (args or "").strip(), None
        match = re.match(r"^(.+?)\s+(\d{2,4})\s*(?:м|m)$", name, re.IGNORECASE)
        if match is not None:
            name, radius = match.group(1).strip(), int(match.group(2))
        result = await self.save_place(tenant.id, name, point[0], point[1], radius)
        await message.answer(_place_reply(result))

    async def on_geonote_command(self, message: Message, tenant: Any, args: str | None) -> None:
        point = _replied_point(message)
        if point is None:
            await message.answer(texts.GEO_NEED_REPLY.replace("/place зал", "/geonote проверить вес на жиме"))
            return
        content = _one_line(args or "", MAX_CONTENT_CHARS)
        if content is None or not any(ch.isalpha() for ch in content):
            await message.answer(texts.GEO_BAD_NOTE)
            return
        outcome = await self.save_point_note(tenant.id, None, content, point[0], point[1])
        await message.answer(texts.GEO_POINT_SAVED.format(content=content) if outcome == "saved" else texts.GEO_NOTES_LIMIT)

    async def on_note_callback(self, tenant: Any, query: CallbackQuery) -> None:
        note_id = parse_note_data(query.data or "")
        if note_id is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        result = await self.done(tenant.id, note_id)
        await self._answer(query, texts.GEO_TOAST[result.outcome])
        await self._edit(query, result.text, result.markup, tenant.id)

    async def on_place_callback(self, tenant: Any, query: CallbackQuery) -> None:
        parsed = parse_place_data(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        result = await self.save_waiting_place(tenant.id, *parsed)
        if result is None:
            await self._answer(query, texts.GEO_TOAST["gone"])
            return
        await self._answer(query, texts.GEO_TOAST["done"])
        await self._edit(query, _place_reply(result), None, tenant.id)

    async def _edit(self, query: CallbackQuery, text: str | None, markup: InlineKeyboardMarkup | None, tenant_id: uuid.UUID) -> None:
        message = query.message
        if message is None or text is None:
            return
        try:
            await self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=text, reply_markup=markup)
        except TelegramAPIError:  # includes "message is not modified" (a double click); the decision is already made
            log.info("could not edit the geo card of tenant %s", tenant_id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)


def _replied_point(message: Message) -> tuple[Decimal, Decimal] | None:
    replied = message.reply_to_message
    if replied is None or replied.location is None:
        return None
    latitude, longitude = coordinate(replied.location.latitude, 90), coordinate(replied.location.longitude, 180)
    return None if latitude is None or longitude is None else (latitude, longitude)


def _place_reply(result: PlaceSaved) -> str:
    if result.outcome == "created":
        return texts.GEO_PLACE_SAVED.format(name=result.name, radius=result.radius) + (texts.GEO_PLACE_BOUND.format(n=result.bound) if result.bound else "")
    if result.outcome == "exists":
        return texts.GEO_PLACE_EXISTS.format(name=result.name)
    return texts.GEO_PLACE_LIMIT if result.outcome == "limit" else texts.GEO_BAD_NAME


# ---------------------------------------------------------------- the tool


class GeoNoteTool:
    """Offered only when the user's own words ask for a note that belongs to a place (``wants_geo_note``). Never in the interview or in proactive messages, never on
    a message with an attachment (those replies have no tools at all)."""

    name = "geo_note"
    functions = frozenset({TOOL_NAME})
    offer_rank = 50

    def __init__(self, engine: Geo, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.geo_enabled or turn is None or not wants_geo_note(turn.text):
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            saved = await self.engine.save_notes(tenant.id, turn, calls)
            if not saved:
                return None
            return ToolResult(notes=(render_note(saved),), phrase=True, cards=(Card("\n".join(saved_text(item) for item in saved)),))

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=50)
