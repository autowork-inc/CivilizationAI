"""User-facing system texts (Russian, as the product speaks to its users) and small Telegram helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ai_civ.db.enums import SharedMood

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup

TELEGRAM_MAX_CHARS = 4096  # Bot API limit of one text message

# Voice of the bot: short, plain, no officialese. The main topic can be renamed by the user ("Чатик") and the Bot API cannot
# read its name, so no text ever names it: "основной чат" only.
FAILURE = "Не достучался до нейросети. Попробуйте ещё раз чуть позже."

UNREGISTERED = (
    "Привет! Я ваш AI-аватар. Нажмите кнопку ниже, создайте группу, включите в ней «Темы» и сделайте меня администратором "
    "с правом «Управление темами». Дальше я сам."
)
CREATE_PERSONA_BUTTON = "Создать AI Persona"
PRIVATE_CHAT = "Я живу в вашей форум-группе. Пишите туда."

# Registration (the bot was added to a group) and the freeze of a group that is not private any more.
SETUP_HEADER = "Почти готово. Осталось:"
PROBLEM_NOT_FORUM = "включить в группе «Темы»"
PROBLEM_NOT_ADMIN = "сделать меня администратором с правом «Управление темами»"
PROBLEM_NOT_PRIVATE = "сделать группу приватной: убрать у неё публичное имя"
PROBLEM_NOT_ALONE = "оставить в группе только вас и меня"
NOT_CREATOR = "Подключить группу может только её создатель."
CONNECTED = "Группа подключена. Пишите сюда."
RETURNED = "Я снова в деле. Продолжаем."
HAS_OTHER_GROUP = "У вас уже подключена другая группа, а я работаю с одной группой на человека. Эту подключить не могу."
OTHER_GROUP_GONE = (
    "Раньше вы подключили другую группу, а потом убрали меня оттуда. Верните меня в неё: переезд в новую группу пока не умею."
)
CHAT_TAKEN = "Эта группа уже подключена к другому человеку."
FROZEN = (
    "В группе появился кто-то кроме вас, поэтому я замолчал: здесь всё, что я о вас помню, и чужим это видеть нельзя. "
    "Уберите лишнего — и я вернусь."
)
UNFROZEN = "Снова только мы вдвоём. Продолжаем."


# Contacts: invite links and the handshake (Module 5, step 2). Names come from people: plain text only, never a parse mode.
DEFAULT_CONTACT_NAME = "Знакомый"
CONNECT_LINK = (
    "Вот ваша ссылка для связи. Перешлите её тому, кого хотите подключить:\n{link}\n\n"
    "Она одноразовая и работает {hours} ч. Человек увидит вас как «{name}». Пока вы сами не откроете доступ, он ничего не увидит."
)
CONNECT_NOT_READY = "Сначала закончите знакомство со мной — потом смогу связывать вас с другими."
CONNECT_TOO_MANY = "У вас уже несколько неиспользованных ссылок. Дождитесь, пока их примут или они истекут."
CONNECT_NEED_ACCOUNT = UNREGISTERED + " Когда всё будет готово, откройте ссылку ещё раз."
CONNECT_NOT_READY_DM = "Сначала закончите знакомство со мной в вашей группе. Потом откройте ссылку ещё раз."
CONNECT_ASK = (
    "{inviter} хочет установить с вами связь. Принять?\n\n"
    "Вы будете представлены как «{me}». Это ничего не открывает: что показывать, решаете вы сами."
)
CONNECT_ACCEPT = "Принять"
CONNECT_DECLINE = "Отклонить"
CONNECT_DONE = "Готово: {inviter} теперь в ваших связях. Пока вы сами не разрешите, никто ничего не видит."
CONNECT_DECLINED = "Хорошо, не принимаем."
CONNECT_ALREADY = "Вы с {inviter} уже на связи."
CONNECT_INVALID = "Эта ссылка уже не работает: её использовали, отозвали или она истекла."
CONNECT_OWN = "Это ваша собственная ссылка. Перешлите её тому, кого хотите подключить."
NOTIFY_CONTACT_ACCEPTED = "{name} теперь в ваших связях. Пока вы ничего не разрешили, о вас не видно ничего: открыть можно в приложении."


# The Diplomat (Module 5, step 4). NOTHING here says WHY an ask failed: "no grant", "blocked", "removed you", "rate limit" and
# "not a live account" must all look the same to the asker. Only "no such person in YOUR address book" is told apart: that is the
# asker's own data.
DIPLOMAT_NO_CONTACT = "Не нашёл такого человека среди ваших связей."
DIPLOMAT_UNAVAILABLE = "Эта информация недоступна."
DIPLOMAT_TRY_LATER = "Не получилось это проверить. Попробуйте ещё раз чуть позже."
# One word per value of the closed vocabulary (civ.shared_mood). Nouns, so they fit any gender: "у Саши сейчас: усталость".
MOOD_WORDS: dict[SharedMood, str] = {
    SharedMood.energetic: "бодрость",
    SharedMood.good: "хорошее настроение",
    SharedMood.calm: "спокойствие",
    SharedMood.tired: "усталость",
    SharedMood.stressed: "стресс",
    SharedMood.overwhelmed: "перегруз",
    SharedMood.sad: "грусть",
    SharedMood.irritated: "раздражение",
}
REVIEW_MOOD_LINE = "Тем, кому вы открыли настроение, это покажется так: «{word}»."

# Invitations to a meeting between two people (Module 5, step 5). Titles and names come from people: plain text only, never a parse mode.
# The confirmation the PROPOSER reads is produced by CODE after the commit, so the model cannot claim "sent" when nothing was.
# As with the Diplomat, a refusal never says why (blocked, removed, not live, no such account all look the same).
PROPOSE_SENT = "Отправил приглашение: {name} — «{title}», {when}.\nКак ответит, сообщу."
PROPOSE_ALREADY = "Это приглашение уже ждёт ответа: {name} — «{title}», {when}."
PROPOSE_TOO_MANY = "Слишком много приглашений без ответа. Дождитесь ответов или попробуйте позже."
PROPOSE_UNAVAILABLE = "Не получилось отправить приглашение этому человеку."
PROPOSE_BAD_INPUT = "Не понял, что и когда предлагаем. Напишите ещё раз: что, в какой день и во сколько."
PROPOSAL_CARD = "{name} зовёт: «{title}»\n{when}\n\nПринять приглашение?"
PROPOSAL_ACCEPT = "✅ Принять"
PROPOSAL_DECLINE = "🚫 Отклонить"
PROPOSAL_ACCEPTED = "✅ Принято: «{title}», {when}.\nЗанёс в календарь."
PROPOSAL_DECLINED = "🚫 Отклонено: «{title}», {when}."
PROPOSAL_EXPIRED = "Срок ответа вышел: «{title}», {when}."
PROPOSAL_GONE = "Это приглашение уже недоступно."
PROPOSAL_TOAST = {
    "accepted": "Принято",
    "declined": "Отклонено",
    "already_accepted": "Уже принято",
    "already_declined": "Уже отклонено",
    "expired": "Срок ответа вышел",
    "unavailable": PROPOSAL_GONE,
}
PROPOSAL_UNKNOWN_BUTTON = "Неизвестная кнопка"
NOTIFY_PROPOSAL_ACCEPTED = "{name} принял(а): «{title}», {when}.\nЗанёс в календарь."
NOTIFY_PROPOSAL_DECLINED = "{name} не сможет в это время: «{title}», {when}."

# Quick events: the user's OWN plan heard in conversation ("иду на 2 часа смотреть стенку"). The card is written by code; only the button
# puts the event into the calendar, so nothing here may say "saved" before that.
QUICK_CARD = "Занести в календарь: «{title}», {when}?"
QUICK_YES = "✅ Занести"
QUICK_NO = "✖️ Нет"
QUICK_CONFIRMED = "✅ Занёс в календарь: «{title}», {when}."
QUICK_DISMISSED = "Не заношу: «{title}», {when}."
QUICK_EXPIRED = "Предложение устарело: «{title}», {when}."
QUICK_GONE = "Это предложение уже недоступно."
QUICK_TOAST = {
    "confirmed": "Занёс",
    "dismissed": "Не заношу",
    "already_confirmed": "Уже в календаре",
    "already_dismissed": "Уже отклонено",
    "expired": "Предложение устарело",
    "gone": QUICK_GONE,
}

# Instant memory (save_fact): the card is written by CODE after the fact is in the database; the button undoes exactly that version.
FACT_CARD = "📝 Запомнил: «{content}»"
FACT_CARD_UPDATED = "📝 Обновил: «{content}»"
FACT_UNDO = "✖️ Отменить"
FACT_ACK = "✅ ОК"  # removes the card at once (otherwise it removes itself after SAVE_FACT_CARD_TTL_SECONDS)
FACT_UNDONE = "↩️ Отменил: «{content}»"
FACT_RESTORED = "↩️ Отменил: «{content}». Вернул прежнее: «{previous}»"
FACT_STALE = "Это уже неактуально: запись изменилась или её нет."
FACT_PROTECTED = "Эту запись я сам не меняю: она у вас закреплена. Поправить её можно в «Настройки и Память»."
FACT_LIMIT = "На сегодня новых записей в память хватит. Продолжим завтра, а важное можно внести в «Настройки и Память»."
DEBATE_HEADER = "🎭 Дебаты: {topic}"
DEBATE_ROLES = {"skeptic": "🔍 Критик", "visionary": "💡 Визионер", "pragmatist": "⚖️ Прагматик"}
DEBATE_QUESTION = "❓ {question}"
DEBATE_TOPIC_DEFAULT = "ваша идея"
DEBATE_QUESTION_DEFAULT = "Что из этого вам ближе всего и какой первый шаг вы готовы сделать?"
SUMMARY_FAIL = {
    "bad_url": "Эту ссылку я открыть не могу: поддерживаются только обычные адреса http:// и https://.",
    "blocked": "Эту ссылку я не открываю: она ведёт на внутренний адрес или закрытый порт, а не на обычный сайт.",
    "dns": "Не нашёл такой сайт. Проверьте адрес ссылки.",
    "timeout": "Страница слишком долго отвечает. Попробуйте позже.",
    "too_large": "Страница слишком большая, чтобы её прочитать (больше 2 МБ).",
    "unsupported": "Это не страница с текстом (файл, картинка или видео), прочитать её не получится.",
    "http_error": "Сайт не отдаёт эту страницу. Попробуйте другую ссылку.",
    "redirects": "Ссылка слишком много раз перенаправляет, открывать её не буду.",
    "no_text": "Не получилось вытащить из страницы чистый текст: возможно, она закрыта или собирается скриптами.",
    "no_transcript": "У этого видео нет субтитров, поэтому пересказать его я не могу.",
    "video_unavailable": "Это видео недоступно: оно закрыто, удалено или с возрастным ограничением.",
    "failed": "Не получилось достать текст по этой ссылке. Попробуйте позже.",
}
MODE_DANSK_ON = "Okay! Nu taler vi kun dansk her. Skriv til mig på dansk, så retter jeg dig venligt. Skriv /normal, hvis du vil skifte tilbage."
MODE_NORMAL_ON = "Вернул обычный режим: в этом топике отвечаю на вашем языке."
MODE_NO_TOPIC = "Языковой режим включается внутри тематического топика. Откройте нужный топик и отправьте команду там."
CARD_HEADER = "🃏 {deck}"
CARD_RIGHT = "✅ Верно!"
CARD_WRONG = "❌ Ошибка!"
CARD_SETTLED = "Уже отвечено"
CARD_GONE = "Этой карточки уже нет."
CARD_BACK = "🔁 Вернусь к ней {when}."
CARD_BACK_TOMORROW = "завтра"
CARD_BACK_DAYS = "через {days} дн."
CARDS_MADE = "🃏 Колода «{deck}»: добавил карточек — {count}."
CARDS_DUPLICATES = " Уже были: {count}."
CARDS_FULL = "Набор карточек полон: чтобы добавить новые, нужно удалить часть старых."
CAPSULE_SEALED = "⏳ Запечатал письмо в будущее. Откроется {when}: «{message}»"
CAPSULE_CANCEL_BUTTON = "✖️ Отменить капсулу"
CAPSULE_CANCELED = "✖️ Капсула отменена: письмо не придёт."
CAPSULE_DELIVERED = "⏳ Письмо из прошлого (запечатано {sealed}):\n\n{message}\n\nТы достиг того, о чём думал тогда?"
CAPSULE_TOO_SOON = "Капсула времени открывается не раньше чем через {n} дней — для коротких напоминаний есть обычные. Назови дату подальше."
CAPSULE_TOO_FAR = "Капсулу можно запечатать не дальше чем на {n} лет вперёд. Назови дату поближе."
CAPSULE_LIMIT = "Запечатанных капсул уже много: подожди, пока откроются, или отмени лишние."
CAPSULE_TOAST = {"canceled": "Отменил", "settled": "Уже отменена", "delivered": "Эта капсула уже открыта", "gone": "Такой капсулы нет"}
FLASHBACK_LONG = "Год назад"
FLASHBACK_SHORT = "Полгода назад"
FLASHBACK_LINE = "🕰 {ago} в этот день ты {action}."
GEO_LATER = "Геопозицию учту, когда закончим знакомство. Ответь пока на вопрос выше."
GEO_NEAR = "📍 Ты рядом с {place}:"
GEO_HERE = "📍 {place}"
GEO_NEAR_NOTES = "📍 Рядом есть заметки:"
GEO_UNKNOWN = "📍 Это место пока не сохранено. Ответь на свою точку командой /place Название — например, /place зал."
GEO_MORE = "…и ещё {n}"
GEO_ALL_DONE = "📍 {place} — всё выполнено ✅"
GEO_SAVE_BUTTON = "💾 Это «{name}»"
GEO_DONE_BUTTON = "✅ Выполнено: {n}"
GEO_TOAST = {"done": "Готово", "settled": "Уже закрыто", "gone": "Этой заметки уже нет"}
GEO_PLACE_SAVED = "📍 Сохранил место «{name}» (радиус {radius} м)."
GEO_PLACE_BOUND = " Привязал заметок: {n}."
GEO_PLACE_EXISTS = "Место «{name}» уже сохранено."
GEO_PLACE_LIMIT = "Мест уже слишком много: чтобы добавить новое, нужно убрать часть старых."
GEO_NEED_REPLY = "Ответь этой командой на сообщение с геопозицией — тогда я пойму, где это место. Например: /place зал"
GEO_BAD_NAME = "Название места — одна короткая строка. Например: /place зал"
GEO_BAD_NOTE = "Напиши текст заметки после команды: /geonote проверить вес на жиме"
GEO_POINT_SAVED = "📍 Запомнил заметку на этой точке: {content}"
GEO_NOTES_LIMIT = "Активных заметок уже слишком много: сначала закрой часть."
GEO_NOTE_SAVED = "📍 Запомнил для «{place}»: {content}. Покажу, когда пришлёшь геопозицию рядом."
GEO_NOTE_PLACE_UNKNOWN = " Места «{place}» ещё нет: пришли точку и ответь на неё командой /place {place} — тогда привяжу."
INVENTORY_BUTTON = "Купил / Пополнил"
INVENTORY_REMINDER = "⚠️ {name}: осталось {lasts} (~{qty} {unit}). Пора заказать новую пачку!"
INVENTORY_LESS_THAN_DAY = "меньше чем на день"
INVENTORY_RESTOCKED = "✅ {name}: пополнил на +{amount} {unit} → осталось {qty} {unit}"
INVENTORY_NO_AMOUNT = "Не знаю, сколько вы обычно берёте. Напишите, например: «купил креатин 500 г» — и я учту."
INVENTORY_STALE = "Это напоминание уже неактуально."
INVENTORY_TOAST = {
    "restocked": "Пополнил",
    "no_amount": "Напишите, сколько купили",
    "settled": "Уже учтено",
    "stale": "Напоминание устарело",
    "gone": "Этой позиции уже нет",
}
RECIPE_BUTTON = "🍳 Приготовлено!"
RECIPE_PREP = "⏱ {n} мин"
RECIPE_INGREDIENTS = "Ингредиенты:"
RECIPE_STEPS = "Приготовление:"
RECIPE_FOCUS = "🎯 Фокус: {items}"
RECIPE_COOKED = "✅ Приготовлено"
RECIPE_XP = "⭐ +{amount} XP — тело"
RECIPE_REPEAT = "Это блюдо я совсем недавно уже предлагал. Напишите ещё раз — придумаю что-то другое."
RECIPE_LIMIT = "На сегодня рецептов достаточно. Вернёмся к этому позже."
RECIPE_TOAST = {"cooked": "Приятного аппетита!", "settled": "Уже отмечено", "gone": "Этого рецепта уже нет"}
WORKOUT_HEADER = "🏋️ Тренировка записана"
WORKOUT_HINT = "💡 {name}: {sessions} занятия подряд все целевые повторения выполнены — в следующий раз можно добавить {inc} кг: {now} → {next} кг."
WORKOUT_XP = "⭐ +{amount} XP — тело"
TASK_HEADER = "{mark} {goal} — {done}/{total}"
TASK_DONE = "✅ {n}"
TASK_DROP = "🚫 {n}"
TASK_SLOT = "📅 {n}"
TASK_GONE = "Этой задачи уже нет."
TASK_TOAST = {
    "done": "Отметил выполненным",
    "dropped": "Отменил шаг",
    "settled": "Этот шаг уже закрыт",
    "slot": "Предлагаю слот — ответьте под карточкой ниже",
    "waiting": "Слот уже предложен, ответьте на карточку",
    "planned": "Слот уже в календаре",
    "no_slot": "Для этого шага нет времени в будущем",
    "limit": "Слишком много неотвеченных предложений в календарь. Сначала ответьте на них",
    "gone": TASK_GONE,
}
FACT_TOAST = {
    "undone": "Отменил",
    "restored": "Вернул прежнее",
    "stale": "Уже неактуально",
    "gone": FACT_STALE,
}

# Photos and documents. The markers are what the STORED message says about an attachment (the bytes are never stored).
ATTACHED_PHOTO = "[фото]"
ATTACHED_FILE = "[файл: {name}]"
ATTACHED_FILE_NAMELESS = "[файл]"
FILE_OFF = "Файлы и фото я пока не разбираю. Напишите то же самое текстом."
FILE_UNSUPPORTED = "Такой формат я не читаю. Понимаю фото, PDF, DOCX и TXT."
FILE_TOO_BIG = "Файл слишком большой: беру до {limit} МБ. Пришлите поменьше или перескажите словами."
FILE_TEXT_TOO_BIG = "Файл слишком большой для поточного анализа. Ограничение — 500 КБ."
FILE_UNREADABLE = "Не смог открыть этот файл: он повреждён или это не тот формат. Попробуйте другой."
FILE_EMPTY = "В файле не нашёл текста. Пришлите другой или напишите словами."
FILE_LATER = "Файлы приму, когда закончим знакомство. Ответьте пока на вопрос выше."

WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def when_text(start: datetime, end: datetime, timezone: str, now: datetime | None = None) -> str:
    """"завтра, 19:00–20:00" / "сб, 12 окт, 19:00–20:00", in the reader's own time zone. A span that crosses midnight says so."""
    zone = ZoneInfo(timezone)
    first, last = start.astimezone(zone), end.astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    if first.date() == today:
        day = "сегодня"
    elif first.date() == today + timedelta(days=1):
        day = "завтра"
    else:
        day = f"{WEEKDAYS_SHORT[first.weekday()]}, {first.day} {MONTHS_SHORT[first.month - 1]}"
    if last.date() == first.date():
        span = f"{first:%H:%M}–{last:%H:%M}"
    else:
        span = f"{first:%H:%M} – {last:%H:%M} (на следующий день)"
    return f"{day}, {span}"


def setup_checklist(problems: list[str]) -> str:
    return SETUP_HEADER + "".join(f"\n• {problem}" for problem in problems)
UNKNOWN_TOPIC = (
    "Этот топик пока ни за что не отвечает. Напишите в основной чат, чем хотите заняться, — заведу под это отдельный."
)
RATE_LIMITED = "Превышен лимит запросов. Пожалуйста, подождите"  # hard-coded on purpose: no model is involved
TOPIC_RENAMED ="✏️ Тема теперь называется «{title}»"
VOICE_OFF ="Голосовые я пока не разбираю. Напишите то же самое текстом."
VOICE_TOO_LONG = "Голосовое слишком длинное: я беру до {limit} секунд. Запишите покороче или напишите текстом."
VOICE_UNCLEAR = "Не разобрал, что в голосовом. Запишите ещё раз или напишите текстом."
VOICE_ECHO = "🎤 Услышал: «{text}»"
TOPIC_FAILED = "Не вышло завести топик. Сделайте меня админом группы с правом «Управление темами» и напишите ещё раз."
MOVED = "Ушёл с этим в «{title}» ➡️"
TOPIC_CREATED = "Завёл тему «{title}» ➡️"
OPEN_TOPIC = "Открыть «{title}»"
SETTINGS_GREETING = "Это ваш пульт: что я о вас помню, ваши принципы и расписание. Открывайте кнопкой ниже."
OPEN_APP = "⚙️ Настройки и Память"
APP_LINK = "Вот ваш пульт — кнопка ниже."
SETUP_DONE ="Готово. Завёл темы: {summary}."
SETUP_NOTHING_NEW = "Всё уже на месте, новых тем не нужно."
SETUP_NO_GOALS = (
    "Не вижу ваших целей, поэтому не знаю, какие темы заводить. Напишите в основной чат, чем хотите заняться, — "
    "заведу тему под каждое дело."
)
SETUP_NO_TOPICS = "Не получилось придумать темы под ваши цели. Отправьте /setup ещё раз."


def shorten(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def topic_url(chat_id: int, thread_id: int) -> str | None:
    """Deep link to a forum topic (``t.me/c/<chat>/<topic>``: a topic id is the id of its first service message).

    Only supergroups have such links: their ids look like -100<internal id>. Anything else -> None, and the caller simply
    sends no button."""
    raw = str(chat_id)
    if not raw.startswith("-100") or len(raw) <= 4:
        return None
    return f"https://t.me/c/{raw[4:]}/{thread_id}"


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Cut ``text`` into pieces of at most ``limit`` characters, preferring paragraph, then line, then word boundaries.

    Nothing is dropped: ``"".join`` of the pieces (plus the separators that were cut at) reproduces the text, minus
    surrounding whitespace on each piece.
    """
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = rest.rfind(separator, 0, limit + 1)
            if cut > limit // 2:  # a boundary in the first half would make pieces needlessly small
                break
            cut = -1
        if cut == -1:
            cut = limit  # no usable boundary (e.g. one endless word): hard cut
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


class KeyedLocks:
    """In-process mutual exclusion per key (tenant, topic): two quick messages in one topic are answered in order,
    each seeing the previous answer in its context. Entries disappear when nobody holds or waits for them."""

    def __init__(self) -> None:
        self._locks: dict[Hashable, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        lock, users = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    def __len__(self) -> int:
        return len(self._locks)


# Feedback channel (PR 5.1): the topic "💡 Идеи и отзывы" is forwarded to the author of the project, the model does not answer there.
FEEDBACK_GREETING = (
    "Здесь можно оставить идею, пожелание или жалобу на меня. Всё, что вы напишете в этой теме, я передам автору проекта — "
    "сам тут отвечать не буду."
)
FEEDBACK_LINK = "Идеи и отзывы собираются в отдельной теме — кнопка ниже."
FEEDBACK_ACCEPTED = "Принято, передал автору проекта!"
FEEDBACK_EMPTY = "Напишите текстом, что хотите передать автору проекта."
FEEDBACK_CARD = "📩 Новое предложение от {username} (ID: {tg_id}) | Персонаж: {character}\nТекст: {text}"
FEEDBACK_NO_USERNAME = "(без username)"
FEEDBACK_NO_NAME = "Без имени"

# Deleting a topic (PR 5.1): the model only ASKS; the user's button deletes.
TOPIC_DELETE_ASK = "Удалить тему «{title}» вместе со всей перепиской в ней? Отменить это нельзя."
TOPIC_DELETE_YES = "🗑 Удалить"
TOPIC_DELETE_NO = "Оставить"
TOPIC_DELETED = "Тема «{title}» удалена."
TOPIC_DELETE_KEPT = "Оставил тему «{title}»."
TOPIC_DELETE_NO_RIGHTS = "Не получилось удалить тему «{title}»: у меня нет права «Управление темами»."
TOPIC_DELETE_FAILED = "Не получилось удалить тему «{title}». Попробуйте ещё раз чуть позже."
TOPIC_DELETE_GONE = "Темы «{title}» уже нет: считаю удалённой."
TOPIC_DELETE_TOASTS = {
    "deleted": "Удалил",
    "kept": "Оставил",
    "no_rights": "Нет права «Управление темами»",
    "failed": "Не вышло, попробуйте позже",
    "gone": "Темы уже нет",
    "unavailable": "Эту тему уже нельзя удалить",
}

# Reminders, alarms and pings (PR 5.4): written by code, never by the model.
REMINDER_SCHEDULED = "⏰ Напомню {when}: «{text}»"
REMINDER_ALARM_SCHEDULED = "⏰ Поставил будильник на {when}: «{text}». Если не отвечу на кнопку, буду напоминать каждые пару минут."
REMINDER_PING_SCHEDULED = "🔔 Пингану {when}: «{text}» — и буду повторять, пока вы не отметитесь."
REMINDER_CANCEL_BUTTON = "✖️ Отменить"
REMINDER_CANCELED = "Отменил: «{text}»."
REMINDER_TOO_MANY = "Столько напоминаний сразу я не потяну: подождите, пока часть сработает."
REMINDER_BAD_TIME = "Не смог разобрать, на когда это поставить. Скажите точнее: «через 20 минут» или «в 7:30»."
REMINDER_TOO_FAR = "Дальше, чем на сутки вперёд, напоминания не ставлю. Для далёких дел есть календарь и «капсула времени»."
REMINDER_ACK_BUTTON = "✅ Я проснулся / На месте"
REMINDER_ACKED = "Отлично, отметил: вы на месте."
REMINDER_GAVE_UP = "Я напоминал несколько раз и больше не буду. Напишите, когда будете на месте."
REMINDER_TOAST = {"acked": "Отметил", "settled": "Уже отмечено", "canceled": "Отменил", "gone": "Этого напоминания уже нет"}
REMINDER_REPEAT_NOTE = "\n\nНапоминаю ещё раз ({n}/{total}). Нажмите кнопку, когда будете на месте."
REMINDER_UNAVAILABLE = "Не получилось поставить напоминание: очередь сейчас недоступна. Попробуйте чуть позже."
# The one short, insistent follow-up after a morning check-in that nobody answered (the button is the reminders' "I am up" button).
MORNING_FOLLOWUP = "Эй, вы проснулись? Нажмите кнопку или напишите мне: я не отстану."


# Rights the bot asks for when it is added to a group through the "create persona" button (Telegram shows them pre-ticked in the admin dialog).
ADMIN_RIGHTS = "change_info+delete_messages+restrict_members+invite_users+pin_messages+manage_topics"


def create_persona_url(bot_username: str) -> str:
    """``t.me/<bot>?startgroup=setup&admin=...``: Telegram offers to add the bot to one of the user's groups with these administrator rights."""
    return f"https://t.me/{bot_username}?startgroup=setup&admin={ADMIN_RIGHTS}"


def create_persona_markup(bot_username: str) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # lazy: the mobile service layer imports this module without aiogram

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=CREATE_PERSONA_BUTTON, url=create_persona_url(bot_username))]])


# The group is created for the person by the userbot (bot/provision.py). Texts are written by code; the model never says them.
PROVISION_START = "Секунду, создаю вашу группу. Это займёт меньше минуты."
PROVISION_WAIT = "Группа уже создаётся. Подождите минутку, я напишу, когда она будет готова."
PROVISION_ADDED = "Готово: группа «{title}» создана, вы в ней, я тоже. Откройте её в списке чатов, я там сам начну знакомство."
PROVISION_LINK = (
    "Группа «{title}» создана, но добавить вас в неё сам я не смог: так устроена приватность вашего аккаунта. "
    "Войдите по ссылке, она одноразовая и действует {days} дн.:\n{link}"
)
PROVISION_REBUILD = "Прежняя группа не работает, поэтому создаю новую, чистую. Это займёт меньше минуты, ваши данные и память остаются со мной."
# The owner has joined the group made for them (by the userbot): the bot makes them an administrator and greets them in the main chat.
GROUP_READY = (
    "Привет! Твоё личное пространство AI Persona готово. Ты здесь админ. В ветке ⚙️ Настройки управляй Mini App, "
    "в 💡 Идеи и отзывы пиши фидбек, а здесь мы будем вести основной диалог."
)
GROUP_NO_ADMIN = (
    "⚠️ Я не смог выдать тебе права админа из-за ограничений Telegram. "
    "Пожалуйста, назначь себя админом вручную, а я пока создал нужные ветки."
)
GROUP_MOVED = "Перенёс вас в эту группу: вся память и история со мной. Темы прежней группы я создам заново, когда они понадобятся."
PROVISION_LINK_BUTTON = "Войти в группу"
PROVISION_FAILED = (
    "Автоматически создать группу не получилось. Сделайте это вручную: нажмите кнопку ниже, создайте группу, включите в ней «Темы» "
    "и сделайте меня администратором с правом «Управление темами». Дальше я сам."
)


def link_markup(url: str, caption: str = PROVISION_LINK_BUTTON) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=caption, url=url)]])
