"""Доступ только для администраторов из конфигурации + журнал действий админа."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

admin_log = logging.getLogger("admin")
log = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]):
        self.admin_ids = admin_ids

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.admin_ids:
            uid = user.id if user else None
            log.warning("Отказ в доступе: user_id=%s", uid)
            if isinstance(event, Message) and event.chat.type == "private":
                await event.answer(f"⛔ Нет доступа.\nВаш Telegram ID: <code>{uid}</code>\n"
                                   "Чтобы получить доступ, администратор должен добавить его в ADMIN_IDS.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None

        if isinstance(event, Message):
            admin_log.info("admin=%s (@%s) message=%r", user.id, user.username, (event.text or "")[:200])
        elif isinstance(event, CallbackQuery):
            admin_log.info("admin=%s (@%s) callback=%r", user.id, user.username, event.data)
        return await handler(event, data)
