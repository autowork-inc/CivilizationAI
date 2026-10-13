"""Importing this package registers every model on Base.metadata (needed by Alembic)."""

from ai_civ.db.models.app import UserSession
from ai_civ.db.models.capsules import TimeCapsule
from ai_civ.db.models.cards import Card, CardReview
from ai_civ.db.models.character import CharacterProfile, TavernCheer, XpLedger
from ai_civ.db.models.core import CoreFact, EmotionalOverride, EmotionalState, Persona, User
from ai_civ.db.models.geo import GeoNote, Place
from ai_civ.db.models.inventory import InventoryItem, InventoryMovement
from ai_civ.db.models.mood import DailyMood
from ai_civ.db.models.memory import EpisodicMemory, MemoryOutbox, Message, MessageAttachment
from ai_civ.db.models.recipes import RecipeHistory
from ai_civ.db.models.review import PendingFact, ReviewBatch
from ai_civ.db.models.schedule import Event, EventDraft, EventProposal, ProactiveCheckinLog, ProactiveSchedule
from ai_civ.db.models.social import AccessGrant, Contact, ContactInvite, DiplomatAuditLog, NotificationOutbox
from ai_civ.db.models.tasks import Task, TaskEvent
from ai_civ.db.models.workouts import Exercise, WorkoutSession, WorkoutSet

__all__ = [
    "AccessGrant",
    "Card",
    "CardReview",
    "CharacterProfile",
    "Contact",
    "ContactInvite",
    "CoreFact",
    "DailyMood",
    "DiplomatAuditLog",
    "EmotionalOverride",
    "EmotionalState",
    "EpisodicMemory",
    "Event",
    "Exercise",
    "EventDraft",
    "EventProposal",
    "GeoNote",
    "InventoryItem",
    "InventoryMovement",
    "MemoryOutbox",
    "Message",
    "MessageAttachment",
    "NotificationOutbox",
    "PendingFact",
    "Persona",
    "Place",
    "ProactiveCheckinLog",
    "ProactiveSchedule",
    "RecipeHistory",
    "ReviewBatch",
    "Task",
    "TaskEvent",
    "TimeCapsule",
    "TavernCheer",
    "User",
    "UserSession",
    "WorkoutSession",
    "WorkoutSet",
    "XpLedger",
]
