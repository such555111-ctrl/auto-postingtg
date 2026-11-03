"""Сбор постов из исходного канала.

Альбом приходит боту отдельными сообщениями с одинаковым media_group_id — склеиваем их в один пост.
Подпись обычно стоит только на одном из сообщений альбома.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timezone

from aiogram.types import Chat, Message

from brands import detect_brand, load_brand_index
from config import Config
from db import Database

log = logging.getLogger(__name__)
_lock = asyncio.Lock()  # aiogram обрабатывает апдейты параллельно — сериализуем запись постов


def build_link(chat: Chat, message_id: int) -> str | None:
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def media_info(m: Message) -> tuple[str, str | None, str | None]:
    if m.photo:
        p = m.photo[-1]
        return "photo", p.file_id, p.file_unique_id
    for kind in ("video", "animation", "document", "audio", "voice", "video_note", "sticker"):
        obj = getattr(m, kind, None)
        if obj:
            return kind, obj.file_id, obj.file_unique_id
    if m.text:
        return "text", None, None
    return "other", None, None


async def collect(db: Database, cfg: Config, message: Message, edited: bool = False) -> int | None:
    """Сохраняет сообщение канала, возвращает id поста."""
    text = message.text or message.caption or ""
    mtype, file_id, file_uid = media_info(message)
    chat_id = message.chat.id
    group = message.media_group_id
    posted_utc = message.date.astimezone(timezone.utc).isoformat(timespec="seconds")
    local_date = message.date.astimezone(cfg.timezone).date().isoformat()

    async with _lock:
        post = await db.post_by_member(chat_id, message.message_id)
        if post is None and group:
            post = await db.post_by_group(chat_id, group)

        if post is None:
            post_id = await db.create_post(
                chat_id, group, message.message_id, text, posted_utc, local_date,
                build_link(message.chat, message.message_id),
            )
            text_changed = bool(text)
        else:
            post_id = post["id"]
            if message.message_id < post["first_message_id"]:
                await db.update_post_first_message(
                    post_id, message.message_id, build_link(message.chat, message.message_id)
                )
            text_changed = False
            if text and text != post["text"] and (not post["text"] or edited):
                await db.update_post_text(post_id, text)
                text_changed = True

        await db.upsert_post_message(post_id, message.message_id, mtype, file_id, file_uid, message.caption)

        if text_changed:
            fresh = await db.get_post(post_id)
            if not fresh["brand_manual"]:
                found = detect_brand(fresh["text"], await load_brand_index(db))
                await db.set_post_brand(post_id, found.id if found else None, manual=False)
                log.info("Пост #%s (msg %s): бренд = %s", post_id, message.message_id,
                         found.name if found else "не определён")
        return post_id
