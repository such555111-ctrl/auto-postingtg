"""«Активные расписания» и «Остановить автопостинг»."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import Database
from planner import ORDER_LABELS
from keyboards import BTN_ACTIVE, BTN_STOP
from scheduler import schedule_slots
from utils import fmt_date, h, safe_edit

alog = logging.getLogger("admin")
router = Router(name="schedules_ui")
router.message.filter(F.chat.type == "private")


async def describe(db: Database, cfg: Config, sch) -> str:
    sid = sch["id"]
    brands = ", ".join(await db.brand_names(await db.schedule_brand_ids(sid)))
    stats = await db.slot_stats(sid)
    total = len(schedule_slots(sch, cfg))
    queue = len(await db.pool_posts(sch["source_date"], await db.schedule_brand_ids(sid), schedule_id=sid,
                                    exclude_global=not cfg.allow_repost))
    status = "⏸ на паузе" if sch["status"] == "paused" else "▶️ активно"
    return (
        f"<b>#{sid}</b> · {status}\n"
        f"Публикация: {fmt_date(sch['publish_date'])}, {sch['start_time']}–{sch['end_time']}, "
        f"каждые {sch['interval_min']} мин\n"
        f"Бренды: {h(brands)}\n"
        f"Порядок: {ORDER_LABELS[sch['order_mode']]}\n"
        f"Слотов: отправлено {stats.get('sent', 0)} из {total} "
        f"(пропущено {stats.get('missed', 0)}, пустых {stats.get('empty', 0)}) · в очереди постов: {queue}"
    )


async def _render(db: Database, cfg: Config, stop_mode: bool):
    live = await db.live_schedules()
    if not live:
        return "Активных расписаний нет.", None
    title = "⛔ <b>Какое расписание остановить?</b>" if stop_mode else "📋 <b>Активные расписания</b>"
    blocks = [title]
    kb = InlineKeyboardBuilder()
    for sch in live:
        blocks.append(await describe(db, cfg, sch))
        kb.row(InlineKeyboardButton(text=f"⛔ Остановить #{sch['id']}", callback_data=f"sc:stop:{sch['id']}"))
        if sch["status"] == "paused":
            kb.row(InlineKeyboardButton(text=f"▶️ Возобновить #{sch['id']}", callback_data=f"sc:res:{sch['id']}"))
    if stop_mode and len(live) > 1:
        kb.row(InlineKeyboardButton(text="⛔ Остановить все", callback_data="sc:stopall"))
    return "\n\n".join(blocks), kb.as_markup()


@router.message(F.text == BTN_ACTIVE)
async def show_active(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.clear()
    text, kb = await _render(db, cfg, stop_mode=False)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == BTN_STOP)
async def show_stop(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.clear()
    text, kb = await _render(db, cfg, stop_mode=True)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^sc:stop:\d+$"))
async def stop_one(call: CallbackQuery, db: Database, cfg: Config) -> None:
    sid = int(call.data.split(":")[2])
    await db.set_schedule_status(sid, "stopped")
    alog.info("admin=%s: расписание #%s остановлено", call.from_user.id, sid)
    text, kb = await _render(db, cfg, stop_mode=False)
    await safe_edit(call.message, f"⛔ Расписание #{sid} остановлено.\n\n{text}", kb)
    await call.answer("Остановлено")


@router.callback_query(F.data == "sc:stopall")
async def stop_all(call: CallbackQuery, db: Database) -> None:
    live = await db.live_schedules()
    for sch in live:
        await db.set_schedule_status(sch["id"], "stopped")
    alog.info("admin=%s: остановлены все расписания (%s)", call.from_user.id, len(live))
    await safe_edit(call.message, f"⛔ Остановлено расписаний: {len(live)}.")
    await call.answer("Остановлено")


@router.callback_query(F.data.regexp(r"^sc:res:\d+$"))
async def resume(call: CallbackQuery, db: Database, cfg: Config) -> None:
    sid = int(call.data.split(":")[2])
    await db.set_schedule_status(sid, "active")
    alog.info("admin=%s: расписание #%s возобновлено", call.from_user.id, sid)
    text, kb = await _render(db, cfg, stop_mode=False)
    await safe_edit(call.message, text, kb)
    await call.answer("Возобновлено")
