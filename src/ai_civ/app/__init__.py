"""Service layer of the mobile app (iOS / Android): the dialogue core WITHOUT Telegram.

Nothing in this package may import ``aiogram`` or ``ai_civ.bot.handlers`` (``tests/app/test_app_chat.py`` checks it in a clean interpreter). It reuses the
transport-free parts of ``ai_civ.bot`` (prompt, personas, attachments, the tool registry's neutral ``Card``), the one database and the LLM clients.
"""
