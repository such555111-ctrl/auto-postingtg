"""«Настройки каналов»: задать исходный/целевой канал и проверить права бота."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageOriginChannel

from config import Config
from db import Database
from keyboards import BTN_CHANNELS, not_menu
from utils import get_channel_id, h, safe_edit

log = logging.getLogger(__name__)
alog = logging.getLogger("admin")
router = Router(name="channels")
router.message.filter(F.chat.type == "private")

LABEL = {"source": "Исходный канал (откуда берём посты)", "target": "Целевой канал (куда публикуем)"}


class ChannelSet(StatesGroup):
    waiting = State()


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Указать исходный", callback_data="ch:set:source")],
        [InlineKeyboardButton(text="📤 Указать целевой", callback_data="ch:set:target")],
        [InlineKeyboardButton(text="🔍 Проверить доступ", callback_data="ch:check")],
    ])


async def _describe(db: Database, cfg: Config, which: str) -> str:
    cid = await get_channel_id(db, cfg, which)
    title = await db.get_setting(f"{which}_channel_title")
    if not cid:
        return f"• {LABEL[which]}: <b>не задан</b>"
    return f"• {LABEL[which]}: <b>{h(title or '—')}</b> (<code>{cid}</code>)"


async def _screen(db: Database, cfg: Config) -> str:
    return "⚙️ <b>Каналы</b>\n\n" + await _describe(db, cfg, "source") + "\n" + await _describe(db, cfg, "target")


@router.message(F.text == BTN_CHANNELS)
async def show_channels(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.clear()
    await message.answer(await _screen(db, cfg), reply_markup=_kb())


@router.callback_query(F.data.startswith("ch:set:"))
async def ask_channel(call: CallbackQuery, state: FSMContext) -> None:
    which = call.data.split(":")[2]
    await state.set_state(ChannelSet.waiting)
    await state.update_data(which=which)
    await call.message.answer(
        f"{LABEL[which]}.\n\nПерешлите сюда любой пост из этого канала <b>или</b> пришлите его ID "
        "(например <code>-1001234567890</code>) либо @username.\n\n"
        "Бот должен быть администратором канала. /cancel — отмена."
    )
    await call.answer()


@router.message(ChannelSet.waiting, not_menu)
async def receive_channel(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    which = (await state.get_data())["which"]
    chat_id: int | str | None = None
    title: str | None = None

    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        chat_id, title = origin.chat.id, origin.chat.title
    elif message.text:
        raw = message.text.strip()
        if raw.startswith("@"):
            chat_id = raw
        else:
            try:
                chat_id = int(raw)
            except ValueError:
                pass
    if chat_id is None:
        await message.answer("Не понял. Перешлите пост из канала или пришлите числовой ID / @username.")
        return

    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError as e:
        await message.answer(f"❌ Канал недоступен боту: {h(e)}\nДобавьте бота администратором и повторите.")
        return

    await db.set_setting(f"{which}_channel_id", str(chat.id))
    await db.set_setting(f"{which}_channel_title", chat.title or chat.username or str(chat.id))
    alog.info("admin=%s: %s канал = %s (%s)", message.from_user.id, which, chat.id, chat.title)
    await state.clear()
    await message.answer("✅ Сохранено.\n\n" + await _screen(db, cfg), reply_markup=_kb())


async def _check(bot: Bot, chat_id: int, need_post: bool) -> str:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError as e:
        return f"❌ нет доступа ({h(e)})"
    status = getattr(member.status, "value", member.status)
    if status == "creator":
        return "✅ владелец"
    if status != "administrator":
        return f"❌ бот не администратор (статус: {status})"
    if need_post and getattr(member, "can_post_messages", True) is False:
        return "⚠️ администратор, но без права «Публикация сообщений»"
    return "✅ администратор" + (", публикация разрешена" if need_post else "")


@router.callback_query(F.data == "ch:check")
async def check_access(call: CallbackQuery, db: Database, cfg: Config, bot: Bot) -> None:
    lines = ["🔍 <b>Проверка доступа</b>"]
    for which, need_post in (("source", False), ("target", True)):
        cid = await get_channel_id(db, cfg, which)
        lines.append(f"• {LABEL[which]}: " + (await _check(bot, cid, need_post) if cid else "не задан"))
    await safe_edit(call.message, "\n".join(lines), _kb())
    await call.answer()
