"""«Сегодняшние бренды и посты» + ручное назначение бренда посту."""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from brands import detect_brand, load_brand_index
from config import Config
from db import Database
from keyboards import BTN_TODAY
from utils import fmt_date, h, media_summary, safe_edit, today_str

alog = logging.getLogger("admin")
router = Router(name="today")
router.message.filter(F.chat.type == "private")


async def overview(db: Database, cfg: Config) -> tuple[str, InlineKeyboardMarkup | None]:
    day = today_str(cfg)
    rows = await db.brand_counts(day)
    if not rows:
        return (
            f"📭 За сегодня ({fmt_date(day)}) бот ещё не сохранил ни одного поста.\n\n"
            "Бот видит только посты, опубликованные после того, как он получил доступ к исходному каналу.",
            None,
        )
    total = sum(r["cnt"] for r in rows)
    lines = [f"📅 <b>Сегодня, {fmt_date(day)}</b> — сохранено постов: {total}\n"]
    kb = InlineKeyboardBuilder()
    for r in rows:
        name = r["name"] or "Не определён"
        lines.append(f"• {h(name)} — {r['cnt']}")
        kb.button(text=f"{name} ({r['cnt']})", callback_data=f"td:b:{r['brand_id'] or 0}")
    kb.adjust(2)
    lines.append("\nНажмите на бренд, чтобы увидеть его посты.")
    return "\n".join(lines), kb.as_markup()


@router.message(F.text == BTN_TODAY)
async def show_today(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.clear()
    text, kb = await overview(db, cfg)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "td:back")
async def back(call: CallbackQuery, db: Database, cfg: Config) -> None:
    text, kb = await overview(db, cfg)
    await safe_edit(call.message, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("td:b:"))
async def brand_posts(call: CallbackQuery, db: Database, cfg: Config) -> None:
    brand_id = int(call.data.split(":")[2]) or None
    day = today_str(cfg)
    posts = await db.posts_for_brand(day, brand_id, limit=15)
    name = "Не определён"
    if brand_id:
        b = await db.get_brand(brand_id)
        name = b["name"] if b else "—"
    media = await db.media_counts([p["id"] for p in posts])

    lines = [f"📂 <b>{h(name)}</b> · {fmt_date(day)}\n"]
    kb = InlineKeyboardBuilder()
    for p in posts:
        t = datetime.fromisoformat(p["posted_at"]).astimezone(cfg.timezone).strftime("%H:%M")
        snippet = " ".join((p["text"] or "").split())[:60] or "(без текста)"
        link = f'<a href="{p["link"]}">открыть</a>' if p["link"] else "—"
        mark = " ✋" if p["brand_manual"] else ""
        lines.append(f"<b>#{p['id']}</b> · {t} · {media_summary(media.get(p['id']))} · {link}{mark}\n   {h(snippet)}")
        kb.button(text=f"🏷 #{p['id']}", callback_data=f"as:{p['id']}")
    kb.adjust(5)
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="td:back"))
    lines.append("\n🏷 — назначить бренд вручную. ✋ — бренд назначен вручную.")
    await safe_edit(call.message, "\n".join(lines), kb.as_markup())
    await call.answer()


@router.callback_query(F.data.regexp(r"^as:\d+$"))
async def choose_brand(call: CallbackQuery, db: Database) -> None:
    post_id = int(call.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    for b in await db.list_brands():
        kb.button(text=b["name"], callback_data=f"as:{post_id}:{b['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="🔄 Определить автоматически", callback_data=f"as:{post_id}:0"))
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="td:back"))
    await safe_edit(call.message, f"Какой бренд назначить посту <b>#{post_id}</b>?", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.regexp(r"^as:\d+:\d+$"))
async def apply_brand(call: CallbackQuery, db: Database) -> None:
    _, pid, bid = call.data.split(":")
    post_id, brand_id = int(pid), int(bid)
    post = await db.get_post(post_id)
    if not post:
        await call.answer("Пост не найден", show_alert=True)
        return
    if brand_id == 0:
        found = detect_brand(post["text"], await load_brand_index(db))
        await db.set_post_brand(post_id, found.id if found else None, manual=False)
        result = found.name if found else "не определён"
    else:
        await db.set_post_brand(post_id, brand_id, manual=True)
        result = (await db.get_brand(brand_id))["name"]
    alog.info("admin=%s: пост #%s -> бренд %s", call.from_user.id, post_id, result)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ К списку брендов", callback_data="td:back")]])
    await safe_edit(call.message, f"✅ Пост #{post_id}: бренд — <b>{h(result)}</b>", kb)
    await call.answer()
