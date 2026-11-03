"""Конфигурация из .env / переменных окружения."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    source_channel_id: int | None
    target_channel_id: int | None
    timezone: ZoneInfo
    db_path: str
    log_level: str
    log_file: str
    missed_grace_minutes: int
    allow_repost: bool
    database_url: str = ""          # postgres://… — если задан, вместо SQLite-файла (нужно на Render)
    port: int | None = None         # порт веб-сервера (Render задаёт PORT сам)
    keepalive_url: str = ""         # публичный адрес сервиса для самопинга (Render: RENDER_EXTERNAL_URL)
    keepalive_seconds: int = 600    # как часто пинговать (Render усыпляет через 15 мин тишины)


def _opt_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} должен быть числом (например -1001234567890), сейчас: {raw!r}")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "да"}


def load_config(env_file: str | None = None) -> Config:
    load_dotenv(env_file)

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его.")

    raw_ids = [x for x in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", "").strip()) if x]
    try:
        admin_ids = frozenset(int(x) for x in raw_ids)
    except ValueError:
        raise SystemExit("ADMIN_IDS должен содержать числовые Telegram ID через запятую.")
    if not admin_ids:
        raise SystemExit("ADMIN_IDS пуст: укажите хотя бы одного администратора.")

    tz_name = os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"Неизвестный часовой пояс TIMEZONE={tz_name!r}")

    database_url = os.getenv("DATABASE_URL", "").strip()
    db_path = os.getenv("DB_PATH", "data/bot.db").strip() or "data/bot.db"
    log_file = os.getenv("LOG_FILE", "logs/bot.log").strip()
    paths = [log_file] if database_url else [db_path, log_file]
    for p in paths:
        if p and p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)

    raw_port = os.getenv("PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else None
    except ValueError:
        raise SystemExit(f"PORT должен быть числом, сейчас: {raw_port!r}")
    try:
        keepalive_seconds = max(30, int(os.getenv("KEEPALIVE_SECONDS", "600")))
    except ValueError:
        raise SystemExit("KEEPALIVE_SECONDS должен быть целым числом")
    keepalive_url = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()

    try:
        grace = int(os.getenv("MISSED_GRACE_MINUTES", "10"))
    except ValueError:
        raise SystemExit("MISSED_GRACE_MINUTES должен быть целым числом")

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        source_channel_id=_opt_int("SOURCE_CHANNEL_ID"),
        target_channel_id=_opt_int("TARGET_CHANNEL_ID"),
        timezone=tz,
        db_path=db_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_file=log_file,
        missed_grace_minutes=max(0, grace),
        allow_repost=_bool("ALLOW_REPOST_ACROSS_SCHEDULES", False),
        database_url=database_url,
        port=port,
        keepalive_url=keepalive_url,
        keepalive_seconds=keepalive_seconds,
    )
