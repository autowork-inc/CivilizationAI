"""Python mirrors of the PostgreSQL enum types in schema civ (names and values match the migration)."""

from __future__ import annotations

import enum
from typing import TypeVar

from sqlalchemy import Enum as SAEnum

from ai_civ.db.base import SCHEMA

E = TypeVar("E", bound=enum.Enum)


def pg_enum(enum_cls: type[E], name: str) -> SAEnum:
    """SQLAlchemy column type bound to an EXISTING PostgreSQL enum (the migration owns CREATE TYPE)."""
    return SAEnum(
        enum_cls,
        name=name,
        schema=SCHEMA,
        create_type=False,
        values_callable=lambda members: [m.value for m in members],
    )


class OnboardingState(enum.StrEnum):
    """users.onboarding_state is plain text + CHECK (not a PostgreSQL enum); the values match the CHECK."""

    pending = "pending"  # registered, the interview has not started
    in_progress = "in_progress"
    done = "done"


class GroupState(enum.StrEnum):
    """users.group_state is plain text + CHECK (not a PostgreSQL enum); the values match the CHECK (migration 0004)."""

    active = "active"
    frozen = "frozen"  # somebody besides the owner and the bot is in the group: the bot is silent there
    dormant = "dormant"  # the bot was removed from the group


class RegistrationOutcome(enum.StrEnum):
    """What ``civ.register_tenant`` answers (migration 0004)."""

    created = "created"
    already_registered = "already_registered"
    reactivated = "reactivated"
    attached_chat = "attached_chat"
    has_other_group = "has_other_group"
    other_group_dormant = "other_group_dormant"
    chat_taken = "chat_taken"


class FactCategory(enum.StrEnum):
    identity = "identity"
    body = "body"
    principle = "principle"
    preference = "preference"
    goal = "goal"
    relationship = "relationship"
    health = "health"
    other = "other"


class FactSource(enum.StrEnum):
    onboarding = "onboarding"
    user_manual = "user_manual"
    approved_review = "approved_review"
    chat_inline = "chat_inline"  # written by the bot from the user's own words in a chat (save_fact, migration 0010); priority <= 60 (CHECK)


class EmotionSource(enum.StrEnum):
    user_declared = "user_declared"
    inferred = "inferred"
    user_confirmed = "user_confirmed"


class EmotionStatus(enum.StrEnum):
    active = "active"
    expired = "expired"
    dismissed = "dismissed"


class SharedMood(enum.StrEnum):
    """The closed vocabulary of what an avatar may say about its owner's mood (migration 0007). Russian wording: ``bot.diplomat``."""

    energetic = "energetic"
    good = "good"
    calm = "calm"
    tired = "tired"
    stressed = "stressed"
    overwhelmed = "overwhelmed"
    sad = "sad"
    irritated = "irritated"


class OverrideMode(enum.StrEnum):
    suspend = "suspend"
    soften = "soften"


class PendingStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class PendingSubject(enum.StrEnum):
    core_fact = "core_fact"
    emotional_state = "emotional_state"


class ChangeType(enum.StrEnum):
    create = "create"
    update = "update"
    delete = "delete"


class OutboxStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"
    dead = "dead"


class OutboxOperation(enum.StrEnum):
    compress_day = "compress_day"
    embed_episode = "embed_episode"
    extract_facts = "extract_facts"


class ContactStatus(enum.StrEnum):
    pending = "pending"
    accepted = "accepted"
    blocked = "blocked"


class AclScope(enum.StrEnum):
    emotional_state_label = "emotional_state_label"
    calendar_free_busy = "calendar_free_busy"
    calendar_details = "calendar_details"
    profile_public = "profile_public"
    persona_status = "persona_status"


class EventStatus(enum.StrEnum):
    planned = "planned"
    done = "done"
    missed = "missed"
    cancelled = "cancelled"
    rescheduled = "rescheduled"


class EventVisibility(enum.StrEnum):
    private = "private"
    busy_only = "busy_only"
    shared = "shared"


class ProposalStatus(enum.StrEnum):
    """event_proposals.status is plain text + CHECK (not a PostgreSQL enum); the values match the CHECK (migration 0008)."""

    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"


class DraftStatus(enum.StrEnum):
    """event_drafts.status is plain text + CHECK (not a PostgreSQL enum); the values match the CHECK (migration 0008)."""

    pending = "pending"
    confirmed = "confirmed"
    dismissed = "dismissed"
    expired = "expired"


class EventSource(enum.StrEnum):
    """events.created_via is plain text + CHECK. ``mini_app`` / ``bot_confirmed`` = the user's own plans, ``peer_proposal`` = a meeting
    that another person proposed and this user accepted (migration 0008)."""

    mini_app = "mini_app"
    bot_confirmed = "bot_confirmed"
    peer_proposal = "peer_proposal"


class EpisodeKind(enum.StrEnum):
    dialogue_chunk = "dialogue_chunk"
    daily_summary = "daily_summary"
    event_note = "event_note"


class Sensitivity(enum.StrEnum):
    normal = "normal"
    private = "private"
    secret = "secret"


class MessageRole(enum.StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"


class TaskStatus(enum.StrEnum):
    """tasks.status is plain text + CHECK (not a PostgreSQL enum), like event_drafts.status; the values match the CHECK (migration 0013)."""

    open = "open"
    done = "done"
    dropped = "dropped"


class TaskSource(enum.StrEnum):
    chat_decompose = "chat_decompose"
    mini_app = "mini_app"


class CardState(enum.StrEnum):
    """card_reviews.state is plain text + CHECK (migration 0014)."""

    new = "new"
    learning = "learning"
    review = "review"


class PersonaMode(enum.StrEnum):
    """personas.mode is plain text + CHECK (migration 0015)."""

    normal = "normal"
    immersion_da = "immersion_da"
