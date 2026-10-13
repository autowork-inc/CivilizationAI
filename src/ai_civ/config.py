"""Application settings (environment / .env). Fail-fast: a missing or malformed URL raises immediately."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Runtime URL: role ai_civ_app (Row Level Security is enforced for it).
    database_url: str | None = None
    # Owner / migration role. Used ONLY by Alembic. Never give it to the bot or to Celery workers.
    migration_database_url: str | None = None

    # direct | session -> regular pooling; transaction (Supabase port 6543) -> no prepared statements.
    db_pooler_mode: Literal["direct", "session", "transaction"] = "session"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # ---- Celery / Redis (broker only; task results are not stored - the outbox table is the durable state)
    celery_broker_url: str = "redis://127.0.0.1:6379/0"
    # soft < hard < outbox lease: a task that overruns is stopped before its lease can expire under it
    celery_task_soft_time_limit: int = 840
    celery_task_time_limit: int = 870

    # ---- Gemini (the only LLM vendor). The key is a secret: environment / .env only, never in code or migrations.
    gemini_api_key: SecretStr | None = None
    gemini_judge_model: str = "gemini-3.5-flash-lite"
    gemini_gc_model: str = "gemini-3.8-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    # gemini-embedding-001 natively returns 3072 dims, but pgvector HNSW indexes `vector` up to 2000 dims, so the model
    # is asked for 1536 (Matryoshka truncation, re-normalised by us) and civ.episodic_memories stays vector(1536).
    embedding_dim: int = 1536
    llm_timeout_seconds: float = 90.0
    embed_timeout_seconds: float = 30.0
    # Module 2 (orchestrator): the chat model answers users; the router model (cheap) decides whether a message in
    # "General" starts a new activity / topic. Both are model names from .env, never hard-wired in code paths.
    gemini_chat_model: str = "gemini-3.8-flash"
    gemini_router_model: str = "gemini-3.5-flash-lite"
    # Onboarding interview: turns ONE free-text answer into strict JSON (the code validates it); cheap classification-grade model.
    gemini_onboarding_model: str = "gemini-3.5-flash-lite"
    # Voice notes: Gemini listens to the recording (audio bytes go straight to the cloud model) and returns the words; from then on the
    # message is ordinary text. Cheap audio-capable model, chosen by the user (2026-10-04).
    gemini_transcribe_model: str = "gemini-3.5-flash-lite"
    # Daily mood chart (bot/mood.py): a cheap model reads one local day of the user's own messages and returns a score -2..+2 (the code validates it).
    gemini_mood_model: str = "gemini-3.5-flash-lite"
    mood_enabled: bool = True
    mood_min_messages: int = 3  # a day with fewer of the user's own messages is not judged (a gap, never an invented "fine")
    mood_max_messages: int = 60  # the newest of the day that the model reads
    mood_max_chars: int = 9000  # ... and at most this much text (the oldest lines go first)

    # ---- Telegram (aiogram, Long Polling). The token is a secret: environment / .env only.
    telegram_bot_token: SecretStr | None = None
    telegram_request_timeout: float = 30.0  # per Bot API request (aiogram adds polling_timeout for getUpdates itself)
    telegram_polling_timeout: int = 10  # long-poll wait, seconds
    # A short blip of the proxy (WARP) must not cost a whole handler: a Bot API call that failed at the NETWORK level is repeated this many times
    # (bounded, growing pause). A request that may already have reached Telegram (timeout, dropped connection) is repeated only if it is a read.
    telegram_network_retries: int = 2
    telegram_network_retry_pause: float = 1.0

    # ---- Outbound proxy (docker-compose `proxy` service: socat -> WARP). Only the two vendors that may be blocked from the server go through it:
    # Gemini (google-genai / httpx) and the Telegram Bot API (aiogram / aiohttp). Empty = direct. Everything else (database, Redis, the link
    # summariser) stays direct on purpose: the summariser's SSRF guard depends on connecting to the addresses it validated itself.
    gemini_proxy: str | None = None
    telegram_proxy: str | None = None

    # ---- Userbot (Telethon): creates the person's private forum group on /start, so they do not have to. A SECOND Telegram identity (a normal user
    # account whose StringSession is a secret as strong as a password: whoever holds it is that account). Active only when all three credentials are set.
    # my.telegram.org -> API development tools gives api_id / api_hash; ``python -m ai_civ.cli.userbot_login`` prints the session string.
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_userbot_session: SecretStr | None = None
    userbot_enabled: bool = True  # false = the old way even with credentials (the person creates the group themself)
    userbot_title_prefix: str = "AI Persona | "  # the group is called "<prefix><the person's name>"
    userbot_max_groups_per_day: int = 20  # across all people: Telegram flood-limits an account that creates groups in bulk
    userbot_link_ttl_days: int = 7  # the one-time invite link for a person whose privacy settings forbid adding them
    userbot_lock_seconds: int = 300  # one Telethon connection at a time (the same StringSession in two places at once is revoked by Telegram)
    provision_key_prefix: str = "civ:prov"

    # ---- Automatic registration (Module 5): the bot is added to a forum group -> the person is registered. Registration is
    # re-evaluated on every event, so the person may fix things in any order; these only limit how often one group is
    # re-checked from plain messages (each check is a few Bot API calls) and how often it is told what is still missing.
    registration_retry_seconds: float = 30.0
    registration_hint_seconds: float = 600.0

    # ---- Contacts (Module 5, step 2): invite links and the notification outbox.
    invite_ttl_hours: int = 72  # a link works this long (and once); the database caps it at 30 days
    invite_purge_days: int = 30  # ended invites are deleted this long after they ended
    notification_lease_seconds: int = 300  # a claimed notification is invisible to other workers this long
    notification_backoff_base_seconds: int = 30  # after a failed delivery: base, 2*base, 4*base ... (a Telegram retry_after wins)
    notification_backoff_max_seconds: int = 900
    notification_batch_per_run: int = 20  # notifications one worker run may deliver for one tenant
    notification_dispatch_limit: int = 500  # tenants with deliverable notifications that one Beat tick hands to the queue
    notification_max_age_days: int = 30  # a notification that could not be delivered for this long is given up (dead)
    notification_done_ttl_days: int = 7

    # ---- Mini App (Module 4). Opened by a Direct Link (https://t.me/<bot>/<short_name>): inline web_app buttons do not work
    # in groups, and a Direct Link does. The page itself is served by ai_civ.web; its public HTTPS URL is set in @BotFather.
    telegram_bot_username: str = "PerfectPerson_bot"
    mini_app_short_name: str = "Overview"
    web_host: str = "127.0.0.1"  # the tunnel (Cloudflare / ngrok) connects to this address; never expose 0.0.0.0 directly
    web_port: int = 8080
    web_dist_dir: str | None = None  # the Svelte build (`npm run build` in web/); default: <project>/web/dist
    # initData is signed once, when the Mini App opens, and never refreshed: this is how long a session may last
    # (Telegram only recommends checking auth_date; a short window would log users out of an open app).
    init_data_max_age_seconds: int = 86_400

    # ---- Prompt assembly (Tier 1 + Tier 3)
    context_hours: int = 72  # Context Buffer window (CLAUDE.md: Redis TTL 72 h; here read from PostgreSQL)
    context_max_messages: int = 60  # newest messages of the topic inside the window
    context_max_chars: int = 24_000  # budget for the buffer; the OLDEST turns are dropped first, the newest is kept
    prompt_max_core_facts: int = 150  # highest-priority facts first; the rest is logged as truncated

    # ---- Tier 2 in live replies (episodic memory + LLM-Judge). Optional enrichment: a failure degrades, it never fails the reply.
    recall_enabled: bool = True
    recall_min_query_chars: int = 12  # shorter messages ("ok", "да") are not worth an embedding + a Judge call
    recall_query_max_chars: int = 2000  # the query is the user's last message, cut to what an embedding can take
    recall_max_chars: int = 4000  # budget for the "Episodic memory" prompt section; whole fragments are dropped, never cut
    recall_timeout_seconds: float = 20.0  # one deadline for embed + search + Judge (the chat model has its own)
    recall_max_distance: float | None = None  # cosine distance above which a candidate never reaches the Judge (uncalibrated: off)

    # ---- The Diplomat in live replies (Function Calling: the model may ASK a contact's avatar; the database decides what it learns)
    diplomat_enabled: bool = True
    diplomat_max_calls: int = 3  # asks honoured per message; the rest are ignored (a prompt cannot make the bot fan out)
    diplomat_max_contacts: int = 40  # address-book entries shown to the model as c1..cN
    diplomat_timeout_seconds: float = 15.0  # one ask (one short database transaction)
    # Hard cap on the reply the model writes ABOUT A CONTACT (the turn with the "Contact data" section). 0 = no cap. Thinking tokens
    # count against it, so do not set it below what the chat model needs to think (an empty reply is an error, see llm._why_empty).
    diplomat_max_output_tokens: int = 400

    # ---- Events from conversation (Module 5, step 5). Quick events: "I am going for 2 hours" -> a draft + a card with buttons; only the
    # user's button writes the event. Housekeeping of drafts and invitations is one Beat task (civ.purge_events).
    quick_events_enabled: bool = True  # offered to EVERY user, with or without contacts
    quick_events_max_calls: int = 3  # drafts made per message; the rest are ignored
    quick_events_max_pending: int = 10  # open drafts of one user; more are not made (a chatty model cannot flood the chat)
    quick_events_ttl_hours: int = 24  # an unanswered card goes stale after this
    event_draft_purge_days: int = 7  # decided / expired drafts are deleted this long after
    event_proposal_purge_days: int = 30  # decided / expired invitations are deleted this long after

    # ---- Rate limit (v1.1): protects the Gemini budget. Redis, fixed window per Telegram user; over the limit the update is dropped and the user
    # gets a fixed text once per window. Fail-open: if Redis is unusable the messages pass (a warning is logged).
    rate_limit_enabled: bool = True
    rate_limit_messages: int = 20  # messages per window per user (a voice note counts as one)
    rate_limit_window_seconds: int = 60
    rate_limit_redis_url: str | None = None  # None = the Celery broker (CELERY_BROKER_URL); counters live only a minute or two
    rate_limit_redis_timeout_seconds: float = 1.0  # connect and command timeout: a dead Redis costs a message at most this
    rate_limit_key_prefix: str = "civ:rl"

    # ---- Autonomous topic renaming (v1.1): the model may call rename_current_topic when the subject of a topic has clearly changed.
    # Telegram does not publish a rate limit for editForumTopic, so the limit is ours: a topic is renamed by the bot at most once per
    # cooldown, and a name the user typed by hand is respected for longer. The cooldown lives in the database (personas.title_changed_at).
    topic_rename_enabled: bool = True
    topic_rename_cooldown_hours: int = 24  # after a rename (or the creation) by the bot
    topic_rename_user_cooldown_days: int = 7  # after the user renamed the topic by hand

    # ---- Instant memory (v2.0): the model may call save_fact when the user states something lasting about themselves. It writes to Core with no
    # review, so it is narrow: the code demands a verbatim quote of the user's own message, the database caps the priority at 60, and these
    # knobs bound how much one chat can write.
    save_fact_enabled: bool = True
    save_fact_max_calls: int = 3  # facts saved per message; the rest are ignored
    save_fact_max_per_day: int = 40  # versions a user's chat may write in 24 h (a chatty model cannot flood the profile)
    save_fact_min_chars: int = 8  # a shorter message ("ok", "да") is not offered the function at all
    save_fact_timeout_seconds: float = 15.0  # one save (one short database transaction)
    save_fact_card_ttl_seconds: float = 15.0  # the "📝 Запомнил" card deletes itself after this long unless the user pressed "Отменить" ("✅ ОК" deletes it at once); 0 = it stays

    # ---- Organic onboarding (v2.0): ONE question (the city, for the time zone) instead of the 7-question interview, and topics that appear as the
    # conversation develops (the model's ``create_topic``, limited by a cooldown and a cap). ``onboarding_light=false`` brings the old interview back.
    onboarding_light: bool = True
    create_topic_enabled: bool = True
    create_topic_cooldown_hours: int = 24  # at most one new topic a day: between two topics the bot makes (the stamp is ``personas.title_changed_at`` of the newest one)
    create_topic_max_active: int = 5  # topics of the bot's own (Settings and feedback excluded); at the cap no new topic is made, by the router or by ``create_topic``
    router_min_confidence: float = 0.85  # the "General" router opens a NEW topic only when its own confidence reaches this (long-term switch stated explicitly)
    router_min_confidence_existing: float = 0.6  # ... and moves a message into an EXISTING topic from this confidence

    # ---- Tasks and decomposition (v2.0, PR 3.1): ``decompose_task`` is offered only when the user's own message asks for a breakdown (a regex in code).
    tasks_enabled: bool = True
    tasks_max_steps: int = 10  # steps kept from one breakdown (more are cut)
    tasks_max_open: int = 20  # open root tasks per user; at the cap no new breakdown is made

    # ---- Workout tracker (v2.0, PR 4.1): ``log_workout`` is offered only when CODE finds a workout cue AND a number in the user's own message; every
    # number the model passes must appear verbatim in that message.
    workout_enabled: bool = True
    workout_max_exercises: int = 12  # exercises kept from one message (more are cut)
    workout_max_sets: int = 60  # sets kept from one message; a workout with more is refused as a whole
    workout_max_per_day: int = 10  # workouts a user may log in 24 h
    workout_max_dictionary: int = 200  # exercises in one user's dictionary; at the cap no new custom exercise is made
    workout_timeout_seconds: float = 15.0  # one save (one short database transaction)

    # ---- Recipe generator (v2.0, PR 4.2): ``suggest_recipe`` is offered on an explicit request ("что приготовить", "рецепт") or a photo; it reads the user's
    # food / health / preference facts and the latest recipes, and the code refuses a repeat of a recent one.
    recipes_enabled: bool = True
    recipes_max_per_day: int = 10  # suggestions per user per 24 h
    recipes_recent_count: int = 5  # the latest recipes the model is told not to repeat ...
    recipes_recent_days: int = 14  # ... within this many days
    recipes_max_facts: int = 20  # food / health / preference facts handed to the model
    recipes_timeout_seconds: float = 15.0  # one database step

    # ---- Smart inventory (v2.0, PR 4.3): ``update_inventory`` is offered on a purchase / stock message; a daily Beat job burns the daily rate and reminds.
    inventory_enabled: bool = True
    inventory_max_calls: int = 5  # items changed by one message
    inventory_max_items: int = 50  # items per user; at the cap no new item is made
    inventory_timeout_seconds: float = 15.0  # one database step
    inventory_local_hour: int = 8  # the daily job runs for a user from this local hour on ("in the morning")
    inventory_remind_cooldown_days: float = 3.0  # one reminder per item per this many days
    inventory_max_catchup_days: int = 7  # missed days (the bot or the worker was down) that are still burned
    inventory_dispatch_limit: int = 500  # tenants visited per Beat tick

    # ---- Geo notes and places (v2.0, PR 4.4): ``save_geo_note`` is offered on "напомни в магазине ..." / "заметка для зала ..."; a sent location is matched against the
    # user's places (haversine in code) and the notes of that place are shown with a button to close each.
    geo_enabled: bool = True
    geo_max_places: int = 100  # places per user; at the cap no new place is saved
    geo_max_notes: int = 200  # ACTIVE notes per user; at the cap no new note is saved
    geo_max_listed: int = 10  # notes shown in one card (the rest is counted)
    geo_max_calls: int = 3  # notes saved from one message
    geo_note_radius_meters: int = 150  # a note made on a point is shown within this distance of it
    geo_timeout_seconds: float = 15.0  # one database step

    # ---- Time capsules (v2.0, PR 4.5): ``seal_time_capsule`` is offered on "капсула времени" / "напомни через полгода" / "письмо в будущее"; an hourly Beat job delivers the
    # letters that are due (never in quiet hours). It is not a timer: the date is at least ``capsules_min_days`` days away (counted in local DATES) and at most 5 years.
    capsules_enabled: bool = True
    capsules_min_days: int = 7
    capsules_max_years: int = 5
    capsules_default_hour: int = 9  # a date without a time is delivered at this local hour
    capsules_max_pending: int = 20  # sealed and not yet opened, per user
    capsules_max_per_day: int = 5  # sealed per user per 24 h
    capsules_per_tick: int = 3  # delivered per user per Beat tick
    capsules_lease_minutes: float = 10.0  # a capsule being delivered is not picked up again for this long
    capsules_dispatch_limit: int = 500  # tenants visited per Beat tick
    capsules_timeout_seconds: float = 15.0  # one database step
    # ---- "A year ago" (v2.0, PR 4.5): the first check-in of the local day may end with one line about what the user did exactly this many days ago (code, from the database).
    flashback_enabled: bool = True
    flashback_long_days: int = 365
    flashback_short_days: int = 180

    # ---- Spaced repetition (v2.0, PR 3.2): ``create_cards`` is offered only on an explicit request; Beat sends due cards (SM-2) outside quiet hours.
    sr_enabled: bool = True
    sr_max_cards_per_call: int = 10  # cards kept from one message (more are cut)
    sr_max_cards: int = 1000  # cards per user; at the cap no new card is made
    sr_daily_limit: int = 10  # cards SENT per local day
    sr_per_tick: int = 2  # cards sent per Beat tick to one user
    sr_max_unanswered: int = 3  # sent and not answered; at that many the bot waits for an answer
    sr_resend_hours: float = 24.0  # an unanswered card is offered again after this long
    sr_dispatch_limit: int = 500  # tenants visited per Beat tick

    # ---- Link summaries (v2.0, PR 3.4): a link + a request for a summary. The page is fetched SSRF-safely (bot/fetch.py) and given to the model as
    # untrusted data in a reply that has no functions at all.
    summary_enabled: bool = True
    fetch_timeout_seconds: float = 10.0  # one deadline for the whole chain of requests (redirects included)
    fetch_max_bytes: int = 2_000_000  # a bigger page is refused (counted after decompression)
    fetch_max_redirects: int = 3
    fetch_min_text_chars: int = 100  # a page with less clean text is "no text"
    summary_max_chars: int = 30_000  # of the extracted text given to the model (longer is cut, and the model is told)

    # ---- Debate (v2.0, PR 3.5): ``debate`` is offered only on an explicit request and is exclusive (no other tool in that reply).
    debate_enabled: bool = True

    # ---- Photos and documents (PR 1.3). Limits are checked on Telegram's metadata BEFORE the download and again on the bytes. Bytes live in
    # memory for one reply only; images and PDFs go to Gemini inline, TXT and DOCX are read by us.
    attachments_enabled: bool = True
    attachment_max_bytes: int = 10_000_000  # Bot API allows 20 MB downloads; Gemini's inline request limit is the tighter one
    attachment_text_max_chars: int = 60_000  # text of a TXT / DOCX given to the model (longer is cut, and the model is told)
    attachment_text_file_max_bytes: int = 500_000  # TXT / DOCX only: a bigger one is refused ("не для поточного анализа"); the text goes into the prompt, a book must not
    attachment_text_file_max_lines: int = 2000  # ... and so is a text with more lines than this (a log of short lines is small in bytes but not in lines)
    attachment_unzipped_max_bytes: int = 30_000_000  # a DOCX that unpacks to more is refused before parsing (zip bomb)

    # ---- Voice notes. The recording is downloaded into memory, transcribed by Gemini (``gemini_transcribe_model``), and only the TEXT is kept
    # (audio is never stored).
    voice_enabled: bool = True
    voice_echo: bool = False  # "🎤 Heard: ..." before the reply (off by default: it is chat noise; the transcript is stored either way)
    voice_hear_tone: bool = True  # the recording ITSELF also travels to the chat model, so it hears the tone, not only the words (see bot/voice.py::TONE_RULES)
    voice_max_seconds: int = 900  # 15 minutes; checked on Telegram's metadata BEFORE downloading
    voice_max_bytes: int = 25 * 1024 * 1024  # checked on the metadata and again on what was actually downloaded (the cloud Bot API itself hands out files up to 20 MB)
    voice_transcript_max_chars: int = 25_000  # a longer transcript is refused, never cut: cutting would change what the user said
    voice_llm_timeout_seconds: float = 180.0  # transcribing / answering a LONG recording takes much longer than a text reply: this replaces ``llm_timeout_seconds`` for it

    # ---- Mobile app service layer (src/ai_civ/app). A repeated request (same client_message_id) is answered from the stored reply; while the first one is
    # still being generated the second waits for it by polling the database (a session advisory lock would not survive the transaction pooler).
    app_dedup_poll_seconds: float = 0.25
    app_stale_seconds: float = 300.0  # a user message without a reply older than this is considered abandoned (the process died): the next request answers it

    # ---- Mobile app API (/api/v1/mobile, ai_civ.web.app_*). Without APP_JWT_SECRET the routers are NOT mounted (the Mini App and the bot are unaffected).
    app_jwt_secret: SecretStr | None = None  # HS256 key of the access tokens; at least 32 characters
    app_access_ttl_minutes: int = 15
    app_refresh_ttl_days: int = 30
    app_refresh_grace_seconds: int = 10  # a refresh token that was just rotated still works this long (parallel requests of one phone must not sign the person out)
    app_auth_rate_limit_attempts: int = 8  # /login and /register: attempts per window for one (IP, e-mail) pair ...
    app_auth_rate_limit_ip_attempts: int = 40  # ... and for one IP whatever the e-mail (stops spraying many addresses); needs RATE_LIMIT_ENABLED, fail-open like the bot's limiter
    app_auth_rate_limit_window_seconds: int = 600
    app_trust_forwarded_for: bool = False  # True ONLY behind a proxy that overwrites X-Forwarded-For (the first address is then the client)

    # ---- Outbox / Garbage Collector
    outbox_lease_seconds: int = 900
    outbox_backoff_base_seconds: int = 30
    outbox_backoff_max_seconds: int = 3600
    outbox_done_ttl_days: int = 7
    outbox_batch_per_run: int = 20  # tasks one worker invocation may claim for one tenant
    gc_local_hour: int = 3  # the day is compressed once the tenant's local clock reaches this hour
    gc_max_chunk_chars: int = 60_000
    judge_top_k: int = 8

    # ---- Proactivity (Module 3): per-user times live in civ.proactive_schedule; these are the global knobs.
    # A schedule row fires when the tenant's local clock is in [local_time, local_time + grace): after longer downtime the
    # nudge is skipped for that day instead of arriving hours late.
    proactive_grace_minutes: int = 30
    # Check-ins (v2.0): when the user has been silent, the bot writes first in one of the tenant-local slots of the day (3-4 a day; fixed ``checkin_slots`` or random, see below).
    # Never inside quiet hours; never before ``checkin_min_silence_hours`` of silence; after ``checkin_max_unanswered`` unanswered ones it waits for the user.
    checkin_enabled: bool = True
    checkin_slots: str = ""  # "" = random slots per tenant and day (see the knobs below); "10:30,14:00,17:30,20:00" pins fixed ones
    checkin_min_silence_hours: float = 3.0
    checkin_max_unanswered: int = 2

    # Randomised check-ins (v2.0, stage 5, PR 5.4): ``checkin_slots`` empty = every tenant gets 3-4 slots a day chosen by code (deterministic per tenant and local
    # day, so every Beat tick computes the same ones) inside the user's ACTIVE hours (between the end and the start of the quiet hours), at least
    # ``checkin_min_gap_minutes`` apart and ``checkin_edge_minutes`` away from the edges of the day. A non-empty ``checkin_slots`` pins fixed slots.
    checkin_min_slots: int = 3
    checkin_max_slots: int = 4
    checkin_min_gap_minutes: int = 150
    checkin_edge_minutes: int = 90

    # ---- Scheduled reminders, alarms and pings (v2.0, PR 5.4): the model's ``schedule_reminder`` puts a Celery task with an ETA; an alarm (or a ping with
    # ``repeat_until_ack``) repeats until the user presses the button or says "я встал / я тут" (the state lives in Redis, ``reminders_redis_url`` None = the broker).
    reminders_enabled: bool = True
    reminders_max_delay_seconds: int = 86_400  # a reminder is set at most one day ahead
    reminders_min_delay_seconds: int = 10
    reminders_max_pending: int = 10  # scheduled and not yet delivered, per user
    reminders_text_max_chars: int = 300
    reminders_repeat_min_seconds: int = 120  # an unanswered alarm repeats every 2-3 minutes ...
    reminders_repeat_max_seconds: int = 180
    reminders_max_repeats: int = 5  # ... at most this many times after the first message
    # A wake-up ALARM (``reminder_type == "alarm"``) is pushier than a ping: every ``reminders_alarm_repeat_seconds`` exactly, up to ``reminders_alarm_max_repeats`` repeats.
    reminders_alarm_repeat_seconds: int = 180
    reminders_alarm_max_repeats: int = 60  # 60 x 180 s = 3 hours of pings; the only things that end an alarm sooner are the user's answer, a canceled/frozen group and a lost state
    # Voice mode: the REPEATS of an alarm (not the first message) go out as a short voice note, which makes the phone sound its priority tone. Telegram's sendVoice takes OGG/Opus, MP3 or M4A, and the
    # project has no encoder or TTS, so the sound is a file YOU provide (``reminders_voice_file``); without the file, or when Telegram refuses it, the repeat goes out as an ordinary text message.
    reminders_voice_ping: bool = False
    reminders_voice_file: str | None = None
    # The first check-in of a local morning (05:00-11:00) that gets no answer in this many seconds is followed by ONE short insistent message with the "I am up" button
    # (``bot/checkin.py`` / ``Orchestrator.run_proactive``); the follow-up is dropped if the user wrote anything meanwhile. 0 = off.
    checkin_morning_followup_seconds: int = 600
    reminders_redis_url: str | None = None
    reminders_redis_timeout_seconds: float = 2.0
    reminders_key_prefix: str = "civ:rem"
    reminders_state_ttl_seconds: int = 172_800  # a reminder's state is forgotten two days after it was made

    # ---- Decay system (v2.0, PR 5.3): the nightly job takes XP away for neglect. -10 body when there has been no logged workout for MORE than ``xp_decay_body_idle_days``
    # days; -10 focus for a task step still open ``xp_decay_task_overdue_hours`` after its deadline (one penalty per task). The database floors every branch at 0.
    xp_decay_enabled: bool = True
    xp_decay_local_hour: int = 3  # a tenant is processed once its local clock shows this hour ("at night")
    xp_decay_body_idle_days: int = 5
    xp_decay_task_overdue_hours: int = 48
    xp_decay_amount: int = 10
    xp_decay_max_task_penalties: int = 5  # per tenant per run (a backlog of old tasks is worked off over several nights, not in one blow)

    # ---- Feedback topic (v2.0, PR 5.1): messages written in the "Ideas and feedback" topic are forwarded to the project's author instead of being answered.
    feedback_admin_telegram_id: int = 413277509
    feedback_max_chars: int = 3500
    # Crash reports (bot/error_handler.py): an unhandled error (a bot update, a Celery task, the userbot) is sent to the admin above as crash_log_<time>.txt.
    error_reports_enabled: bool = True
    error_report_cooldown_seconds: int = 300  # the same error from the same place is reported once per this many seconds (the rest is counted)
    error_report_max_bytes: int = 200_000  # a longer log keeps its beginning and its end
    # The model may call create_topic only after the user agreed to a topic offered in the previous assistant message (the offer itself is allowed only for an activity
    # the user keeps coming back to). False = the old behaviour (the model may open a topic right away).
    create_topic_require_offer: bool = True
    delete_topic_enabled: bool = True

    # ---- Weekly review of the quarantine (pending_facts): a message with "keep / delete" buttons per proposal.
    review_local_weekday: int = 6  # tenant-local weekday of the review, Monday = 0 (default: Sunday)
    review_local_hour: int = 18  # sent once the tenant's local clock has reached this hour on that weekday
    review_max_items: int = 8  # proposals per weekly message (the rest waits for the next week)
    review_expire_days: int = 28  # a proposal nobody answered for this long becomes 'expired' (final)
    review_cooldown_days: int = 30  # a proposal the user deleted is not proposed again (same key and value) for this long

    @field_validator("app_jwt_secret", mode="before")
    @classmethod
    def _check_jwt_secret(cls, value):
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        text = None if value is None else str(value).strip().strip("'\"").strip()
        if not text:
            return None  # `APP_JWT_SECRET=` means "mobile API off"
        if len(text) < 32:
            raise ValueError("APP_JWT_SECRET must be at least 32 characters (for example `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`)")
        return text

    @field_validator("telegram_api_id", "telegram_api_hash", "telegram_userbot_session", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if hasattr(value, "get_secret_value"):  # a SecretStr passed in code: str() of it is the mask, not the secret
            value = value.get_secret_value()
        text = None if value is None else str(value).strip().strip("'\"").strip()  # a pasted session often arrives wrapped in quotes / whitespace
        return text or None  # `TELEGRAM_API_ID=` in .env means "not configured"

    @property
    def userbot_ready(self) -> bool:
        """The userbot can be used: switched on and all three credentials present."""
        return bool(self.userbot_enabled and self.telegram_api_id and self.telegram_api_hash and self.telegram_userbot_session)

    @field_validator("gemini_proxy", "telegram_proxy", mode="before")
    @classmethod
    def _check_proxy(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None  # `GEMINI_PROXY=` in .env means "no proxy"
        value = str(value).strip()
        if not re.fullmatch(r"(socks5h?|https?)://[^\s/]+:\d{1,5}", value):
            raise ValueError("proxy must look like socks5h://host:1080 (socks5, socks5h, http or https; host and port required)")
        return value

    @field_validator("telegram_bot_username")
    @classmethod
    def _check_bot_username(cls, value: str) -> str:
        value = value.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("TELEGRAM_BOT_USERNAME must be a Telegram username (5-32 letters, digits, underscores)")
        return value

    @field_validator("mini_app_short_name")
    @classmethod
    def _check_short_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", value):
            raise ValueError("MINI_APP_SHORT_NAME must be the short name from @BotFather (letters, digits, underscores)")
        return value

    @property
    def mini_app_direct_link(self) -> str:
        """Direct Link Mini App: opens from any chat, a group included, with a signed initData for whoever pressed it."""
        return f"https://t.me/{self.telegram_bot_username}/{self.mini_app_short_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def normalize_async_url(url: str | None, *, name: str) -> str:
    """Return a SQLAlchemy URL string for the asyncpg driver, or raise ConfigError.

    * ``postgres://`` / ``postgresql://`` are rewritten to ``postgresql+asyncpg://`` (Supabase hands out plain URLs);
    * ``sslmode=`` is rewritten to ``ssl=`` because asyncpg (via SQLAlchemy) does not understand ``sslmode``.
    """
    if not url:
        raise ConfigError(f"{name} is not set (see .env.example)")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix) :]
            break
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg":
        raise ConfigError(f"{name} must use the postgresql+asyncpg driver, got {parsed.drivername!r}")
    query = dict(parsed.query)
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
        parsed = parsed.set(query=query)
    return parsed.render_as_string(hide_password=False)


def get_database_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return normalize_async_url(settings.database_url, name="DATABASE_URL")


def get_migration_database_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return normalize_async_url(settings.migration_database_url, name="MIGRATION_DATABASE_URL")
