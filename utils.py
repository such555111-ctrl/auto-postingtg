"""Мелкие общие помощники."""
from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from config import Config
from db import Database

log = logging.getLogger(__name__)

MEDIA_EMOJI = {
    "photo": "📷", "video": "🎬", "animation": "🎞", "document": "📄", "audio": "🎵",
    "voice": "🎤", "video_note": "⭕", "sticker": "🏷", "text": "📝", "other": "❔",
}


def h(value) -> str:
    return html.escape(str(value), quote=False)


def today_str(cfg: Config) -> str:
    return datetime.now(cfg.timezone).date().isoformat()


def fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def media_summary(items: list[tuple[str, int]] | None) -> str:
    if not items:
        return "—"
    return " ".join(f"{MEDIA_EMOJI.get(t, '❔')}{c}" for t, c in sorted(items))


async def safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def get_channel_id(db: Database, cfg: Config, which: str) -> int | None:
    """which: 'source' | 'target'. Значение из бота приоритетнее .env."""
    saved = await db.get_setting(f"{which}_channel_id")
    if saved:
        return int(saved)
    return cfg.source_channel_id if which == "source" else cfg.target_channel_id


async def notify_admins(bot: Bot, cfg: Config, text: str) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:  # админ мог не запускать бота
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)
