"""Module 2: Telegram interface (aiogram, Long Polling), orchestrator and proactivity hooks.

Layers (outside in): ``handlers`` (aiogram: tenant middleware, routing of updates) -> ``orchestrator`` (the use cases,
no aiogram types in its signatures except the ``Bot`` used for sending) -> ``prompt`` / ``personas`` / ``routing``
(pure rendering + SQL under ``tenant_session``). The brain stays in PostgreSQL; nothing here keeps state of its own
except short-lived in-process locks.
"""
