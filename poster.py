"""Публикация поста в целевой канал.

Используем copyMessages: Telegram сам копирует сообщения целиком — альбом остаётся альбомом,
подпись, цитаты (blockquote), ссылки и форматирование сохраняются, пометки «переслано» нет.
Требование: бот — администратор исходного канала (читает) и целевого (публикует).
"""
from __future__ import annotations

import logging
from typing import Sequence

from aiogram import Bot

log = logging.getLogger(__name__)


class PublishError(Exception):
    pass


async def publish_post(bot: Bot, source_chat_id: int, target_chat_id: int, message_ids: Sequence[int]) -> int:
    ids = sorted(set(message_ids))
    if not ids:
        raise PublishError("У поста нет сохранённых сообщений")
    copied = await bot.copy_messages(chat_id=target_chat_id, from_chat_id=source_chat_id, message_ids=ids)
    if not copied:
        raise PublishError("Telegram не скопировал ни одного сообщения (пост удалён в источнике?)")
    if len(copied) < len(ids):
        log.warning("Скопировано %s из %s сообщений поста %s…", len(copied), len(ids), ids[0])
    return len(copied)
