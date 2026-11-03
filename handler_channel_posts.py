"""Приём новых и отредактированных постов из исходного канала."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from collector import collect
from config import Config
from db import Database
from utils import get_channel_id

log = logging.getLogger(__name__)
router = Router(name="channel_posts")


async def _handle(message: Message, db: Database, cfg: Config, edited: bool) -> None:
    source = await get_channel_id(db, cfg, "source")
    if source is None or message.chat.id != source:
        log.debug("Пост из постороннего канала %s проигнорирован", message.chat.id)
        return
    try:
        post_id = await collect(db, cfg, message, edited=edited)
        log.debug("Сохранено сообщение %s -> пост #%s", message.message_id, post_id)
    except Exception:
        log.exception("Не удалось сохранить сообщение %s из канала %s", message.message_id, message.chat.id)


@router.channel_post()
async def on_channel_post(message: Message, db: Database, cfg: Config) -> None:
    await _handle(message, db, cfg, edited=False)


@router.edited_channel_post()
async def on_edited_channel_post(message: Message, db: Database, cfg: Config) -> None:
    await _handle(message, db, cfg, edited=True)
