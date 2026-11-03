"""/start, /menu, /cancel."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from keyboards import main_menu

router = Router(name="common")
router.message.filter(F.chat.type == "private")

HELP = (
    "👋 <b>Бот автопостинга по брендам</b>\n\n"
    "Бот собирает новые посты из исходного канала, определяет бренд и по вашему расписанию "
    "публикует выбранные бренды в целевой канал.\n\n"
    "Важно: бот видит только посты, опубликованные <b>после</b> того, как получил доступ к каналу.\n\n"
    "Команды: /menu — меню, /cancel — отменить текущее действие."
)


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())
