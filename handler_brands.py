"""«Бренды и алиасы»: добавление, переименование, алиасы, удаление брендов."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from brands import parse_aliases, reclassify
from db import Database
from keyboards import BTN_BRANDS, not_menu
from utils import h, safe_edit

alog = logging.getLogger("admin")
router = Router(name="brands_admin")
router.message.filter(F.chat.type == "private")

ALIAS_HELP = (
    "Пришлите алиасы через запятую или с новой строки (регистр и «ё/е» не важны). "
    "Название бренда учитывается автоматически.\n"
    "Знак <code>*</code> в конце — любые окончания: <code>шанел*</code> найдёт «шанель», «шанели».\n"
    "«-» — без алиасов. /cancel — отмена."
)


class BrandEdit(StatesGroup):
    add_name = State()
    add_aliases = State()
    replace_aliases = State()
    append_aliases = State()
    rename = State()


async def _list_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    brands = await db.list_brands()
    kb = InlineKeyboardBuilder()
    for b in brands:
        kb.button(text=b["name"], callback_data=f"br:o:{b['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="➕ Добавить бренд", callback_data="br:add"))
    kb.row(InlineKeyboardButton(text="🔄 Пересчитать бренды у постов", callback_data="br:rc"))
    text = f"🏷 <b>Бренды</b>: {len(brands)}\nВыберите бренд, чтобы изменить его алиасы." if brands else \
        "Брендов пока нет. Добавьте первый."
    return text, kb.as_markup()


async def _card_view(db: Database, brand_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    brand = await db.get_brand(brand_id)
    if not brand:
        return None
    aliases = await db.brand_aliases(brand_id)
    text = (f"🏷 <b>{h(brand['name'])}</b>\n"
            f"Алиасы: {h(', '.join(aliases)) if aliases else '— нет —'}\n\n"
            "Название бренда тоже ищется в тексте автоматически.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Заменить алиасы", callback_data=f"br:ea:{brand_id}")
    kb.button(text="➕ Добавить алиасы", callback_data=f"br:aa:{brand_id}")
    kb.button(text="✏️ Переименовать", callback_data=f"br:rn:{brand_id}")
    kb.button(text="🗑 Удалить", callback_data=f"br:dl:{brand_id}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="◀️ К списку", callback_data="br:list"))
    return text, kb.as_markup()


async def _after_change(message: Message, db: Database, brand_id: int, prefix: str) -> None:
    changed = await reclassify(db)
    view = await _card_view(db, brand_id)
    note = f"{prefix}\nПересчитано брендов у постов: изменилось {changed}.\n\n"
    if view:
        await message.answer(note + view[0], reply_markup=view[1])
    else:
        await message.answer(note)


# ---------- список / карточка ----------
@router.message(F.text == BTN_BRANDS)
async def show_brands(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    text, kb = await _list_view(db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "br:list")
async def to_list(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    text, kb = await _list_view(db)
    await safe_edit(call.message, text, kb)
    await call.answer()


@router.callback_query(F.data.regexp(r"^br:o:\d+$"))
async def open_card(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    view = await _card_view(db, int(call.data.split(":")[2]))
    if not view:
        await call.answer("Бренд не найден", show_alert=True)
        return
    await safe_edit(call.message, *view)
    await call.answer()


@router.callback_query(F.data == "br:rc")
async def recalc(call: CallbackQuery, db: Database) -> None:
    changed = await reclassify(db)
    alog.info("admin=%s: пересчёт брендов, изменилось %s", call.from_user.id, changed)
    await call.answer(f"Готово, изменилось постов: {changed}", show_alert=True)


# ---------- добавление ----------
@router.callback_query(F.data == "br:add")
async def add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BrandEdit.add_name)
    await call.message.answer("Название нового бренда (например <code>Van Cleef & Arpels</code>). /cancel — отмена.")
    await call.answer()


@router.message(BrandEdit.add_name, F.text, not_menu)
async def add_name(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()
    if not name or len(name) > 60:
        await message.answer("Название должно быть от 1 до 60 символов.")
        return
    try:
        brand_id = await db.add_brand(name)
    except ValueError as e:
        await message.answer(f"❌ {e}. Пришлите другое название.")
        return
    alog.info("admin=%s: добавлен бренд %r (#%s)", message.from_user.id, name, brand_id)
    await state.set_state(BrandEdit.add_aliases)
    await state.update_data(brand_id=brand_id)
    await message.answer(f"Бренд «{h(name)}» создан.\n\n{ALIAS_HELP}")


async def _save_aliases(message: Message, state: FSMContext, db: Database, replace: bool) -> None:
    brand_id = (await state.get_data())["brand_id"]
    aliases = parse_aliases(message.text)
    if replace:
        await db.set_aliases(brand_id, aliases)
    else:
        await db.add_aliases(brand_id, aliases)
    alog.info("admin=%s: бренд #%s алиасы %s %s", message.from_user.id, brand_id,
              "заменены на" if replace else "дополнены", aliases)
    await state.clear()
    await _after_change(message, db, brand_id, "✅ Алиасы сохранены.")


@router.message(BrandEdit.add_aliases, F.text, not_menu)
async def add_aliases_msg(message: Message, state: FSMContext, db: Database) -> None:
    await _save_aliases(message, state, db, replace=False)


# ---------- алиасы существующего бренда ----------
@router.callback_query(F.data.regexp(r"^br:(ea|aa):\d+$"))
async def alias_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    _, mode, bid = call.data.split(":")
    await state.set_state(BrandEdit.replace_aliases if mode == "ea" else BrandEdit.append_aliases)
    await state.update_data(brand_id=int(bid))
    prefix = "Текущие алиасы будут <b>заменены</b>.\n\n" if mode == "ea" else "Алиасы будут <b>добавлены</b> к текущим.\n\n"
    await call.message.answer(prefix + ALIAS_HELP)
    await call.answer()


@router.message(BrandEdit.replace_aliases, F.text, not_menu)
async def replace_aliases_msg(message: Message, state: FSMContext, db: Database) -> None:
    await _save_aliases(message, state, db, replace=True)


@router.message(BrandEdit.append_aliases, F.text, not_menu)
async def append_aliases_msg(message: Message, state: FSMContext, db: Database) -> None:
    await _save_aliases(message, state, db, replace=False)


# ---------- переименование ----------
@router.callback_query(F.data.regexp(r"^br:rn:\d+$"))
async def rename_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BrandEdit.rename)
    await state.update_data(brand_id=int(call.data.split(":")[2]))
    await call.message.answer("Новое название бренда. /cancel — отмена.")
    await call.answer()


@router.message(BrandEdit.rename, F.text, not_menu)
async def rename_msg(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()
    if not name or len(name) > 60:
        await message.answer("Название должно быть от 1 до 60 символов.")
        return
    brand_id = (await state.get_data())["brand_id"]
    try:
        await db.rename_brand(brand_id, name)
    except ValueError as e:
        await message.answer(f"❌ {e}. Пришлите другое название.")
        return
    alog.info("admin=%s: бренд #%s переименован в %r", message.from_user.id, brand_id, name)
    await state.clear()
    await _after_change(message, db, brand_id, "✅ Переименовано.")


# ---------- удаление ----------
@router.callback_query(F.data.regexp(r"^br:dl:\d+$"))
async def delete_ask(call: CallbackQuery, db: Database) -> None:
    bid = int(call.data.split(":")[2])
    brand = await db.get_brand(bid)
    if not brand:
        await call.answer("Бренд не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"br:dly:{bid}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"br:o:{bid}"),
    ]])
    await safe_edit(call.message,
                    f"Удалить бренд «{h(brand['name'])}»? Его посты станут «не определёнными», "
                    "а расписания с этим брендом потеряют его.", kb)
    await call.answer()


@router.callback_query(F.data.regexp(r"^br:dly:\d+$"))
async def delete_do(call: CallbackQuery, db: Database) -> None:
    bid = int(call.data.split(":")[2])
    await db.delete_brand(bid)
    alog.info("admin=%s: удалён бренд #%s", call.from_user.id, bid)
    changed = await reclassify(db)
    text, kb = await _list_view(db)
    await safe_edit(call.message, f"🗑 Бренд удалён (посты пересчитаны: {changed}).\n\n{text}", kb)
    await call.answer("Удалено")
