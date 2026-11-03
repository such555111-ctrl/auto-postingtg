"""«Настроить автопостинг»: пошаговый мастер → подтверждение → создание расписания."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import Database
from keyboards import BTN_SETUP, main_menu, not_menu
from planner import (
    ORDER_LABELS, ORDER_RANDOM, ORDER_RR, ORDER_TIME, build_slots, parse_date, parse_time, suggest_interval,
)
from utils import fmt_date, get_channel_id, h, safe_edit, today_str

alog = logging.getLogger("admin")
router = Router(name="wizard")
router.message.filter(F.chat.type == "private")

CANCEL_BTN = InlineKeyboardButton(text="❌ Отмена", callback_data="wz:cancel")


class Wizard(StatesGroup):
    brands = State()
    date = State()
    start = State()
    end = State()
    interval = State()
    order = State()
    confirm = State()


def _brands_kb(rows, selected: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for r in rows:
        mark = "✅" if r["brand_id"] in selected else "⬜"
        kb.button(text=f"{mark} {r['name']} ({r['cnt']})", callback_data=f"wz:b:{r['brand_id']}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="▶️ Далее", callback_data="wz:next"), CANCEL_BTN)
    return kb.as_markup()


async def _selectable_rows(db: Database, cfg: Config):
    return [r for r in await db.brand_counts(today_str(cfg)) if r["brand_id"] is not None]


# ---------- шаг 1: бренды ----------
@router.message(F.text == BTN_SETUP)
async def wizard_start(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.clear()
    if not await get_channel_id(db, cfg, "source") or not await get_channel_id(db, cfg, "target"):
        await message.answer("Сначала задайте исходный и целевой каналы: «Настройки каналов».")
        return
    rows = await _selectable_rows(db, cfg)
    if not rows:
        await message.answer(
            f"📭 За сегодня ({fmt_date(today_str(cfg))}) нет постов с определённым брендом.\n"
            "Проверьте «Сегодняшние бренды и посты» — возможно, бренды у постов «не определены» "
            "(их можно назначить вручную или добавить алиасы в «Бренды и алиасы»)."
        )
        return
    await state.set_state(Wizard.brands)
    await state.update_data(brand_ids=[])
    await message.answer(
        "🚀 <b>Автопостинг — шаг 1/5</b>\nВыберите бренды (посты за сегодня), которые нужно публиковать:",
        reply_markup=_brands_kb(rows, set()),
    )


@router.callback_query(Wizard.brands, F.data.regexp(r"^wz:b:\d+$"))
async def toggle_brand(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config) -> None:
    bid = int(call.data.split(":")[2])
    selected = set((await state.get_data())["brand_ids"])
    selected.symmetric_difference_update({bid})
    await state.update_data(brand_ids=sorted(selected))
    await safe_edit(call.message, call.message.html_text, _brands_kb(await _selectable_rows(db, cfg), selected))
    await call.answer()


@router.callback_query(Wizard.brands, F.data == "wz:next")
async def brands_next(call: CallbackQuery, state: FSMContext) -> None:
    if not (await state.get_data())["brand_ids"]:
        await call.answer("Выберите хотя бы один бренд", show_alert=True)
        return
    await state.set_state(Wizard.date)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="wz:d:0"),
         InlineKeyboardButton(text="Завтра", callback_data="wz:d:1")],
        [CANCEL_BTN],
    ])
    await safe_edit(call.message, "📅 <b>Шаг 2/5</b> — дата публикаций.\nНажмите кнопку или пришлите дату "
                                  "(<code>ДД.ММ.ГГГГ</code>). По умолчанию — сегодня.", kb)
    await call.answer()


# ---------- шаг 2: дата ----------
async def _ask_start(target: Message, state: FSMContext, day, edit: bool = False) -> None:
    await state.update_data(date=day.isoformat())
    await state.set_state(Wizard.start)
    text = (f"Дата: <b>{fmt_date(day.isoformat())}</b>\n\n🕐 <b>Шаг 3/5</b> — время начала автопостинга "
            "(<code>ЧЧ:ММ</code>, например <code>10:00</code>):")
    kb = InlineKeyboardMarkup(inline_keyboard=[[CANCEL_BTN]])
    if edit:
        await safe_edit(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(Wizard.date, F.data.regexp(r"^wz:d:[01]$"))
async def date_button(call: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    today = datetime.now(cfg.timezone).date()
    day = today + timedelta(days=int(call.data.split(":")[2]))
    await _ask_start(call.message, state, day, edit=True)
    await call.answer()


@router.message(Wizard.date, F.text, not_menu)
async def date_text(message: Message, state: FSMContext, cfg: Config) -> None:
    today = datetime.now(cfg.timezone).date()
    try:
        day = parse_date(message.text, today)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    if day < today:
        await message.answer("❌ Эта дата уже прошла. Пришлите сегодняшнюю или будущую.")
        return
    await _ask_start(message, state, day)


# ---------- шаги 3–4: время ----------
@router.message(Wizard.start, F.text, not_menu)
async def start_text(message: Message, state: FSMContext) -> None:
    try:
        t = parse_time(message.text)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.update_data(start=t.strftime("%H:%M"))
    await state.set_state(Wizard.end)
    await message.answer(f"Начало: <b>{t:%H:%M}</b>\n\n🕔 Время окончания автопостинга (<code>ЧЧ:ММ</code>):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[CANCEL_BTN]]))


@router.message(Wizard.end, F.text, not_menu)
async def end_text(message: Message, state: FSMContext, cfg: Config) -> None:
    data = await state.get_data()
    try:
        end = parse_time(message.text)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    start = parse_time(data["start"])
    if end < start:
        await message.answer("❌ Окончание раньше начала. Пришлите время позже начала.")
        return
    window_end = datetime.combine(datetime.fromisoformat(data["date"]).date(), end, tzinfo=cfg.timezone)
    if window_end < datetime.now(cfg.timezone):
        await message.answer("❌ Это окно уже закончилось. Выберите время окончания в будущем.")
        return
    await state.update_data(end=end.strftime("%H:%M"))
    await state.set_state(Wizard.interval)
    await message.answer(f"Окончание: <b>{end:%H:%M}</b>\n\n⏱ <b>Шаг 4/5</b> — интервал между публикациями "
                         "в минутах (например <code>15</code>):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[CANCEL_BTN]]))


# ---------- шаг 4: интервал ----------
@router.message(Wizard.interval, F.text, not_menu)
async def interval_text(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 1440):
        await message.answer("❌ Введите целое число минут от 1 до 1440.")
        return
    await state.update_data(interval=int(raw))
    await state.set_state(Wizard.order)
    kb = InlineKeyboardBuilder()
    for key in (ORDER_TIME, ORDER_RANDOM, ORDER_RR):
        kb.button(text=ORDER_LABELS[key], callback_data=f"wz:o:{key}")
    kb.adjust(1)
    kb.row(CANCEL_BTN)
    await message.answer(f"Интервал: <b>{raw} мин</b>\n\n🔀 <b>Шаг 5/5</b> — порядок выбора постов:",
                         reply_markup=kb.as_markup())


# ---------- шаг 5: порядок и подтверждение ----------
async def _confirm_text(db: Database, cfg: Config, data: dict) -> str:
    brand_ids = data["brand_ids"]
    names = ", ".join(await db.brand_names(brand_ids))
    pool = len(await db.pool_posts(today_str(cfg), brand_ids, exclude_global=not cfg.allow_repost))
    day = datetime.fromisoformat(data["date"]).date()
    start, end = parse_time(data["start"]), parse_time(data["end"])
    slots = build_slots(day, start, end, data["interval"], cfg.timezone)
    border = datetime.now(cfg.timezone) - timedelta(minutes=cfg.missed_grace_minutes)
    upcoming = [s for s in slots if s >= border]
    will_post = min(pool, len(upcoming))

    lines = [
        "📝 <b>Проверьте настройки</b>\n",
        f"Бренды: <b>{h(names)}</b>",
        f"Посты за: {fmt_date(today_str(cfg))} (сегодня) — доступно к публикации: <b>{pool}</b>",
        f"Публикация: <b>{fmt_date(data['date'])}</b>, {data['start']}–{data['end']}, "
        f"каждые <b>{data['interval']}</b> мин",
        f"Порядок: {ORDER_LABELS[data['order']]}",
        f"Слотов в окне: {len(slots)}" + (f" (из них уже прошло {len(slots) - len(upcoming)} — будут пропущены)"
                                         if len(upcoming) < len(slots) else ""),
        f"Будет опубликовано: <b>{will_post}</b> из {pool}",
    ]
    if pool > len(upcoming):
        lines.append(f"⚠️ Не поместятся в окно: {pool - len(upcoming)} пост(ов) — увеличьте окно или уменьшите интервал.")
    elif pool and len(upcoming) > pool:
        hint = suggest_interval(pool, start, end)
        lines.append(f"ℹ️ Постов меньше, чем слотов: лишние слоты останутся пустыми (новые посты за сегодня "
                     f"будут подхватываться автоматически)."
                     + (f" Чтобы растянуть {pool} постов на всё окно, интервал ≈ {hint} мин." if hint else ""))
    return "\n".join(lines)


@router.callback_query(Wizard.order, F.data.regexp(r"^wz:o:(time|random|round_robin)$"))
async def order_chosen(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.update_data(order=call.data.split(":")[2])
    await state.set_state(Wizard.confirm)
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Запустить", callback_data="wz:go"), CANCEL_BTN,
    ]])
    await safe_edit(call.message, await _confirm_text(db, cfg, data), kb)
    await call.answer()


@router.callback_query(Wizard.confirm, F.data == "wz:go")
async def confirm_go(call: CallbackQuery, state: FSMContext, db: Database, cfg: Config) -> None:
    data = await state.get_data()
    sid = await db.create_schedule(
        created_by=call.from_user.id, source_date=today_str(cfg), publish_date=data["date"],
        start_time=data["start"], end_time=data["end"], interval_min=data["interval"],
        order_mode=data["order"], brand_ids=data["brand_ids"],
    )
    alog.info("admin=%s: создано расписание #%s %s", call.from_user.id, sid, data)
    await state.clear()
    await safe_edit(call.message, f"✅ Автопостинг запущен — расписание <b>#{sid}</b>.\n"
                                  "Смотреть: «Активные расписания», остановить: «Остановить автопостинг».")
    await call.message.answer("Готово.", reply_markup=main_menu())
    await call.answer("Запущено")


# ---------- отмена ----------
@router.callback_query(F.data == "wz:cancel")
async def cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(call.message, "❌ Настройка автопостинга отменена.")
    await call.answer()
