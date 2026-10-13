"""Stand-ins for the Telegram Bot API and for the Gemini chat model. Nothing here touches the network.

``FakeTelegramSession`` replaces aiogram's HTTP session, so the REAL ``Bot``, the real aiogram types, middleware and
Dispatcher run in the tests; only the wire is faked. Every API call is recorded as the aiogram method object it was.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    CreateForumTopic,
    DeleteForumTopic,
    DeleteMessage,
    EditForumTopic,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChat,
    GetChatMember,
    GetChatMemberCount,
    GetFile,
    GetMe,
    LeaveChat,
    PromoteChatMember,
    SendChatAction,
    SendDocument,
    SendMessage,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import (
    AcceptedGiftTypes,
    CallbackQuery,
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberUpdated,
    File,
    ForumTopic,
    ForumTopicEdited,
    Location,
    Message,
    Update,
    User,
    Voice,
    Document,
    PhotoSize,
)

from ai_civ.memory.llm import ChatTurn, ToolReply, ToolSpec

TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # shape only; aiogram validates the format, never the secret
BOT_ID = 123456789  # the id inside TOKEN: what ``Bot.id`` returns


@dataclass
class FakeGroup:
    """What the fake Telegram knows about one group. ``members`` are the HUMANS in it (the creator included); the bot is extra."""

    creator_id: int
    members: set[int] = field(default_factory=set)
    type: str = "supergroup"
    is_forum: bool = True
    username: str | None = None
    bot_status: str = "administrator"  # administrator | member | left
    bot_can_manage_topics: bool = True
    admins: set[int] = field(default_factory=set)  # humans the bot (or the creator) made administrators
    promote_error: str | None = None  # the text of the Bad Request promoteChatMember answers with (None = it works)

    def __post_init__(self) -> None:
        self.members.add(self.creator_id)

    @property
    def member_count(self) -> int:
        return len(self.members) + (0 if self.bot_status == "left" else 1)


class FakeTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.closed = False
        self._rules: list[tuple[str, type[Exception], Callable[[Any], bool], int | None, str | None, dict[str, Any]]] = []
        self._message_ids = itertools.count(1000)
        self._thread_ids = itertools.count(500)
        self.sent_ids: list[int] = []  # message ids returned for each SendMessage, in the order of ``sent()``
        self.groups: dict[int, FakeGroup] = {}  # unknown chats: "chat not found", like a group the bot is not in
        self.files: dict[str, bytes] = {}  # file_id -> the bytes a download returns (voice notes)
        self.downloads: list[str] = []  # file ids actually downloaded, in order
        self.download_error: Exception | None = None  # raised by the download itself (getFile failures: use ``fail_on("getFile", ...)``)

    def add_group(self, chat_id: int, creator_id: int, **kw: Any) -> FakeGroup:
        self.groups[chat_id] = group = FakeGroup(creator_id=creator_id, **kw)
        return group

    def _group(self, method: TelegramMethod[Any], chat_id: int) -> FakeGroup:
        if chat_id not in self.groups:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        return self.groups[chat_id]

    def _member(self, method: TelegramMethod[Any], chat_id: int, user_id: int) -> Any:
        group = self._group(method, chat_id)
        user = User(id=user_id, is_bot=user_id == BOT_ID, first_name="x")
        if user_id == BOT_ID:
            if group.bot_status == "administrator":
                return ChatMemberAdministrator(
                    user=user, can_be_edited=False, is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
                    can_manage_video_chats=True, can_restrict_members=True, can_promote_members=False, can_change_info=True,
                    can_invite_users=True, can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
                    can_send_welcome_messages=False, can_manage_topics=group.bot_can_manage_topics,
                )  # fmt: skip
            return ChatMemberMember(user=user) if group.bot_status == "member" else ChatMemberLeft(user=user)
        if user_id == group.creator_id:
            return ChatMemberOwner(user=user, is_anonymous=False)
        if user_id in group.admins and user_id in group.members:
            return ChatMemberAdministrator(
                user=user, can_be_edited=True, is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
                can_manage_video_chats=True, can_restrict_members=True, can_promote_members=True, can_change_info=True,
                can_invite_users=True, can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
                can_send_welcome_messages=False, can_manage_topics=True,
            )  # fmt: skip
        return ChatMemberMember(user=user) if user_id in group.members else ChatMemberLeft(user=user)

    def fail_on(
        self,
        api_method: str,
        error: type[Exception],
        *,
        when: Callable[[Any], bool] = lambda method: True,
        times: int | None = None,
        message: str | None = None,
        **error_kwargs: Any,
    ) -> None:
        """Make calls of ``api_method`` (e.g. "sendMessage") raise ``error(method=..., message=...)``; ``times`` limits how many.
        ``message`` replaces the text of the error; ``error_kwargs`` are extra constructor arguments (e.g. ``retry_after=120``)."""
        self._rules.append((api_method, error, when, times, message, error_kwargs))

    async def close(self) -> None:
        self.closed = True

    async def stream_content(self, url: str, *args: Any, chunk_size: int = 65536, **kwargs: Any):
        """aiogram's file download: the URL ends with ``voice/<file_id>.oga`` (see ``GetFile`` below)."""
        file_id = url.rsplit("/", 1)[-1].removesuffix(".oga")
        self.downloads.append(file_id)
        if self.download_error is not None:
            raise self.download_error
        data = self.files[file_id]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        name = method.__api_method__
        for index, (rule_name, error, when, times, message, error_kwargs) in enumerate(self._rules):
            if rule_name == name and when(method) and times != 0:
                if times is not None:
                    self._rules[index] = (rule_name, error, when, times - 1, message, error_kwargs)
                raise error(method=method, message=message or f"simulated {name} failure", **error_kwargs)
        if isinstance(method, SendMessage):
            self.sent_ids.append(next(self._message_ids))
            return Message(
                message_id=self.sent_ids[-1],
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="supergroup"),
                text=method.text,
                message_thread_id=method.message_thread_id,
            )
        if isinstance(method, PromoteChatMember):
            group = self._group(method, int(method.chat_id))
            if group.promote_error is not None:
                raise TelegramBadRequest(method=method, message=group.promote_error)
            group.admins.add(method.user_id)
            return True
        if isinstance(method, LeaveChat):
            self._group(method, int(method.chat_id))
            return True
        if isinstance(method, SendDocument):
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                caption=method.caption,
            )
        if isinstance(method, SendVoice):
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="supergroup"),
                caption=method.caption,
                message_thread_id=method.message_thread_id,
            )
        if isinstance(method, CreateForumTopic):
            return ForumTopic(message_thread_id=next(self._thread_ids), name=method.name, icon_color=0x6FB9F0)
        if isinstance(method, SendChatAction | AnswerCallbackQuery | EditForumTopic | DeleteForumTopic | DeleteMessage | EditMessageReplyMarkup):
            return True
        if isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id or 0,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="supergroup"),
                text=method.text,
            )
        if isinstance(method, GetFile):
            if method.file_id not in self.files:
                raise TelegramBadRequest(method=method, message="Bad Request: wrong file_id")
            return File(
                file_id=method.file_id, file_unique_id=f"u-{method.file_id}",
                file_path=f"voice/{method.file_id}.oga", file_size=len(self.files[method.file_id]),
            )  # fmt: skip
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="civ", username="civ_bot")
        if isinstance(method, GetChat):
            group = self._group(method, int(method.chat_id))
            return ChatFullInfo(
                id=int(method.chat_id), type=group.type, is_forum=group.is_forum or None, username=group.username,
                accent_color_id=0, max_reaction_count=0,
                accepted_gift_types=AcceptedGiftTypes(
                    unlimited_gifts=False, limited_gifts=False, unique_gifts=False, premium_subscription=False,
                    gifts_from_channels=False,
                ),
            )  # fmt: skip
        if isinstance(method, GetChatMember):
            return self._member(method, int(method.chat_id), method.user_id)
        if isinstance(method, GetChatMemberCount):
            return self._group(method, int(method.chat_id)).member_count
        raise NotImplementedError(f"FakeTelegramSession does not implement {name}")

    # ---- inspection helpers
    def sent(self) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage)]

    def voices(self) -> list[SendVoice]:
        return [c for c in self.calls if isinstance(c, SendVoice)]

    def promotions(self) -> list[PromoteChatMember]:
        return [c for c in self.calls if isinstance(c, PromoteChatMember)]

    def documents(self) -> list[SendDocument]:
        return [c for c in self.calls if isinstance(c, SendDocument)]

    def sent_to(self, thread_id: int | None) -> list[SendMessage]:
        return [c for c in self.sent() if c.message_thread_id == thread_id]

    def topics(self) -> list[CreateForumTopic]:
        return [c for c in self.calls if isinstance(c, CreateForumTopic)]

    def topic_deletions(self) -> list[DeleteForumTopic]:
        return [c for c in self.calls if isinstance(c, DeleteForumTopic)]

    def topic_renames(self) -> list[EditForumTopic]:
        return [c for c in self.calls if isinstance(c, EditForumTopic)]

    def edits(self) -> list[EditMessageText]:
        return [c for c in self.calls if isinstance(c, EditMessageText)]

    def deletes(self) -> list[DeleteMessage]:
        return [c for c in self.calls if isinstance(c, DeleteMessage)]

    def callback_answers(self) -> list[AnswerCallbackQuery]:
        return [c for c in self.calls if isinstance(c, AnswerCallbackQuery)]


def make_bot(session: FakeTelegramSession | None = None) -> tuple[Bot, FakeTelegramSession]:
    session = session or FakeTelegramSession()
    return Bot(token=TOKEN, session=session), session


_update_ids = itertools.count(1)


def make_update(
    *,
    text: str | None,
    user_id: int,
    chat_id: int,
    message_id: int,
    thread_id: int | None = None,
    is_topic_message: bool | None = None,
    chat_type: str = "supergroup",
    is_bot: bool = False,
    username: str | None = None,
) -> Update:
    """An incoming text message. For a message in a forum topic pass ``thread_id`` (``is_topic_message`` defaults to true);
    for General pass neither - or ``thread_id`` with ``is_topic_message=None`` to mimic Telegram's reply-chain quirk."""
    if is_topic_message is None and thread_id is not None:
        is_topic_message = True
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type, is_forum=chat_type == "supergroup"),
            from_user=User(id=user_id, is_bot=is_bot, first_name="Tester", username=username),
            text=text,
            message_thread_id=thread_id,
            is_topic_message=is_topic_message,
        ),
    )


def make_location_update(
    *,
    latitude: float,
    longitude: float,
    user_id: int,
    chat_id: int,
    message_id: int,
    thread_id: int | None = None,
) -> Update:
    """An incoming location (a point on the map or the current position)."""
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="supergroup", is_forum=True),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            location=Location(latitude=latitude, longitude=longitude),
            message_thread_id=thread_id,
            is_topic_message=True if thread_id is not None else None,
        ),
    )


def make_reply_to_location_update(
    *,
    text: str,
    latitude: float,
    longitude: float,
    user_id: int,
    chat_id: int,
    message_id: int,
    thread_id: int | None = None,
) -> Update:
    """A text (a command) that is a REPLY to a location message."""
    chat = Chat(id=chat_id, type="supergroup", is_forum=True)
    sender = User(id=user_id, is_bot=False, first_name="Tester")
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=chat,
            from_user=sender,
            text=text,
            message_thread_id=thread_id,
            is_topic_message=True if thread_id is not None else None,
            reply_to_message=Message(
                message_id=message_id - 1, date=datetime.now(UTC), chat=chat, from_user=sender, location=Location(latitude=latitude, longitude=longitude)
            ),
        ),
    )


def make_voice_update(
    *,
    file_id: str,
    user_id: int,
    chat_id: int,
    message_id: int,
    duration: int = 5,
    file_size: int | None = 1000,
    mime_type: str | None = "audio/ogg",  # what Telegram says (the bot passes it to the transcription call)
    thread_id: int | None = None,
    chat_type: str = "supergroup",
) -> Update:
    """An incoming voice note (the bytes live in ``FakeTelegramSession.files[file_id]``)."""
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type, is_forum=chat_type == "supergroup"),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            voice=Voice(
                file_id=file_id, file_unique_id=f"u-{file_id}", duration=duration, mime_type=mime_type, file_size=file_size
            ),
            message_thread_id=thread_id,
            is_topic_message=True if thread_id is not None else None,
        ),
    )


def make_file_update(
    *,
    file_id: str,
    user_id: int,
    chat_id: int,
    message_id: int,
    photo: bool = False,
    file_name: str | None = "file.pdf",
    mime_type: str | None = "application/pdf",
    file_size: int | None = 1000,
    caption: str | None = None,
    thread_id: int | None = None,
) -> Update:
    """An incoming photo (``photo=True``: one size) or document (the bytes live in ``FakeTelegramSession.files[file_id]``)."""
    media: dict[str, Any] = (
        {"photo": [PhotoSize(file_id=file_id, file_unique_id=f"u-{file_id}", width=800, height=600, file_size=file_size)]}
        if photo
        else {"document": Document(file_id=file_id, file_unique_id=f"u-{file_id}", file_name=file_name, mime_type=mime_type, file_size=file_size)}
    )
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="supergroup", is_forum=True),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            caption=caption,
            message_thread_id=thread_id,
            is_topic_message=True if thread_id is not None else None,
            **media,
        ),
    )


def make_topic_edited_update(
    *, name: str | None, user_id: int, chat_id: int, thread_id: int | None, message_id: int = 7000, is_bot: bool = False
) -> Update:
    """The service message Telegram posts into a topic when somebody renames it (``forum_topic_edited``); ``name=None`` = icon only."""
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="supergroup", is_forum=True),
            from_user=User(id=user_id, is_bot=is_bot, first_name="Tester"),
            message_thread_id=thread_id,
            is_topic_message=True if thread_id is not None else None,
            forum_topic_edited=ForumTopicEdited(name=name, icon_custom_emoji_id="1" if name is None else None),
        ),
    )


def make_callback_update(
    *, data: str, user_id: int, chat_id: int, message_id: int, thread_id: int | None = None, chat_type: str = "supergroup"
) -> Update:
    """A press of an inline button that belongs to the bot's message ``message_id`` in ``chat_id``."""
    chat = Chat(id=chat_id, type=chat_type, is_forum=True if chat_type == "supergroup" else None)
    return Update(
        update_id=next(_update_ids),
        callback_query=CallbackQuery(
            id=str(next(_update_ids)),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            chat_instance="ci",
            message=Message(
                message_id=message_id,
                date=datetime.now(UTC),
                chat=chat,
                from_user=User(id=42, is_bot=True, first_name="civ"),
                text="card",
                message_thread_id=thread_id,
                is_topic_message=True if thread_id is not None else None,
            ),
            data=data,
        ),
    )


def _chat_member(user_id: int, status: str, *, is_bot: bool = False, can_manage_topics: bool = True) -> Any:
    """A ChatMember object of ``status`` (administrator | member | left) for the ``old/new_chat_member`` of an update."""
    user = User(id=user_id, is_bot=is_bot, first_name="x")
    if status == "administrator":
        return ChatMemberAdministrator(
            user=user, can_be_edited=False, is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
            can_manage_video_chats=True, can_restrict_members=True, can_promote_members=False, can_change_info=True,
            can_invite_users=True, can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
            can_send_welcome_messages=False, can_manage_topics=can_manage_topics,
        )  # fmt: skip
    return ChatMemberMember(user=user) if status == "member" else ChatMemberLeft(user=user)


def make_bot_membership_update(
    *, chat_id: int, actor_id: int, new_status: str = "administrator", old_status: str = "left", chat_type: str = "supergroup"
) -> Update:
    """``my_chat_member``: ``actor_id`` changed the bot's status in the chat (added it, promoted it, removed it)."""
    return Update(
        update_id=next(_update_ids),
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type=chat_type, is_forum=chat_type == "supergroup"),
            from_user=User(id=actor_id, is_bot=False, first_name="Actor"),
            date=datetime.now(UTC),
            old_chat_member=_chat_member(BOT_ID, old_status, is_bot=True),
            new_chat_member=_chat_member(BOT_ID, new_status, is_bot=True),
        ),
    )


def make_member_update(*, chat_id: int, user_id: int, new_status: str, old_status: str, actor_id: int | None = None) -> Update:
    """``chat_member``: somebody else joined or left the chat."""
    return Update(
        update_id=next(_update_ids),
        chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type="supergroup", is_forum=True),
            from_user=User(id=actor_id or user_id, is_bot=False, first_name="Actor"),
            date=datetime.now(UTC),
            old_chat_member=_chat_member(user_id, old_status),
            new_chat_member=_chat_member(user_id, new_status),
        ),
    )


def make_service_update(
    *, chat_id: int, joined: list[int] | None = None, left: int | None = None, actor_id: int | None = None
) -> Update:
    """The "X joined" / "X left" service message (no text)."""
    who = actor_id or (joined[0] if joined else left) or 1
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_update_ids),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="supergroup", is_forum=True),
            from_user=User(id=who, is_bot=False, first_name="Actor"),
            new_chat_members=[User(id=u, is_bot=u == BOT_ID, first_name="New") for u in joined] if joined else None,
            left_chat_member=User(id=left, is_bot=False, first_name="Gone") if left else None,
        ),
    )


class FakeChatLLM:
    """TextLLM: ``responder(system, turns)`` returns the reply text or an Exception (raised, as a transport failure would be).

    ``on_call`` is awaited while the "model" is being called - lets a test inspect the database mid-call.
    """

    def __init__(
        self,
        responder: Callable[[str, list[ChatTurn]], Any] | None = None,
        on_call: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.responder = responder or (lambda system, turns: f"ok: {turns[-1].text}")
        self.on_call = on_call
        self.calls: list[dict[str, Any]] = []
        # generate_with_tools: returns a ToolReply, a plain string (a final answer) or an Exception; by default it behaves like
        # generate_text (the model chose not to call anything)
        self.tool_responder: Callable[[str, list[ChatTurn], list[ToolSpec]], Any] | None = None
        self.tool_calls: list[dict[str, Any]] = []
        # transcribe: ``transcriber(data, mime_type)`` returns the words or an Exception (raised, as a transport failure would be)
        self.transcriber: Callable[[bytes, str], Any] = lambda data, mime_type: "привет, это голосовое"
        self.transcribe_calls: list[dict[str, Any]] = []
        # stream_text (see below)
        self.stream_calls: list[dict[str, Any]] = []
        self.stream_chunk_words = 2
        self.stream_pause = 0.0  # seconds before each piece
        self.stream_break_after: int | None = None
        self.stream_break_error: BaseException = RuntimeError("stream broke")

    async def transcribe(self, *, model: str, system: str, data: bytes, mime_type: str) -> str:
        self.transcribe_calls.append({"model": model, "system": system, "data": data, "mime_type": mime_type})
        result = self.transcriber(data, mime_type)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result

    async def generate_with_tools(
        self, *, model: str, system: str, turns: list[ChatTurn], tools: list[ToolSpec], max_output_tokens: int | None = None
    ) -> ToolReply:
        self.tool_calls.append(
            {"model": model, "system": system, "turns": list(turns), "tools": list(tools), "max_output_tokens": max_output_tokens}
        )
        if self.on_call is not None:
            await self.on_call()
        if self.tool_responder is None:
            result: Any = self.responder(system, turns)
        else:
            result = self.tool_responder(system, turns, tools)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result if isinstance(result, ToolReply) else ToolReply(text=result)

    async def generate_text(
        self, *, model: str, system: str, turns: list[ChatTurn], max_output_tokens: int | None = None
    ) -> str:
        self.calls.append({"model": model, "system": system, "turns": list(turns), "max_output_tokens": max_output_tokens})
        if self.on_call is not None:
            await self.on_call()
        result = self.responder(system, turns)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result

    async def stream_text(
        self, *, model: str, system: str, turns: list[ChatTurn], max_output_tokens: int | None = None
    ) -> AsyncIterator[str]:
        """StreamLLM: the reply of ``responder`` in word-sized pieces (``stream_chunk_words``). ``stream_break_after`` = raise ``stream_break_error`` after that many pieces."""
        self.stream_calls.append({"model": model, "system": system, "turns": list(turns), "max_output_tokens": max_output_tokens})
        if self.on_call is not None:
            await self.on_call()
        result = self.responder(system, turns)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, BaseException):
            raise result
        words = result.split(" ")
        size = self.stream_chunk_words
        pieces = [" ".join(words[i : i + size]) + (" " if i + size < len(words) else "") for i in range(0, len(words), size)]
        for number, piece in enumerate(pieces):
            if self.stream_break_after is not None and number >= self.stream_break_after:
                raise self.stream_break_error
            await asyncio.sleep(self.stream_pause)
            yield piece
