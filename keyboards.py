from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_SETUP = "Настроить автопостинг"
BTN_TODAY = "Сегодняшние бренды и посты"
BTN_ACTIVE = "Активные расписания"
BTN_STOP = "Остановить автопостинг"
BTN_BRANDS = "Бренды и алиасы"
BTN_CHANNELS = "Настройки каналов"

MENU_TEXTS = {BTN_SETUP, BTN_TODAY, BTN_ACTIVE, BTN_STOP, BTN_BRANDS, BTN_CHANNELS}


def not_menu(message) -> bool:
    """Фильтр: сообщение — не нажатие кнопки главного меню (кнопки меню обрабатываются отдельно и сбрасывают шаг)."""
    return not (message.text and message.text.strip() in MENU_TEXTS)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SETUP)],
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_ACTIVE)],
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_BRANDS)],
            [KeyboardButton(text=BTN_CHANNELS)],
        ],
        resize_keyboard=True,
    )
