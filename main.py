"""Точка входа: python main.py

Что делает, кроме самого бота:
  * поднимает крошечный веб-сервер на $PORT (/ и /health) — Render требует, чтобы Web Service
    слушал порт, и по этим запросам понимает, что сервис жив;
  * раз в KEEPALIVE_SECONDS пингует собственный публичный адрес, чтобы бесплатный Render
    не усыплял сервис (он засыпает после 15 минут без входящих запросов).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import handler_brands
import handler_channel_posts
import handler_channels
import handler_common
import handler_schedules
import handler_today
import handler_wizard
from brands import seed_default_brands
from config import Config, load_config
from db import Database
from logging_setup import setup_logging
from middlewares import AdminOnlyMiddleware
from scheduler import Scheduler

log = logging.getLogger("bot")


def build_dispatcher(cfg: Config, db: Database) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), db=db, cfg=cfg)

    guard = AdminOnlyMiddleware(cfg.admin_ids)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)

    dp.include_routers(
        handler_common.router, handler_wizard.router, handler_today.router, handler_schedules.router,
        handler_brands.router, handler_channels.router,
        handler_channel_posts.router,  # посты каналов: без проверки админа (их присылает Telegram)
    )
    return dp


# ---------- веб-сервер для Render ----------
async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_web(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Веб-сервер слушает порт %s", port)
    return runner


async def keepalive(url: str, every: int) -> None:
    target = url.rstrip("/") + "/health"
    log.info("Самопинг включён: %s каждые %s с", target, every)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while True:
            await asyncio.sleep(every)
            try:
                async with session.get(target) as resp:
                    log.debug("Самопинг: HTTP %s", resp.status)
            except Exception as e:
                log.warning("Самопинг не удался: %s", e)


# ---------- запуск ----------
async def main() -> None:
    cfg = load_config()
    setup_logging(cfg)

    runner = await start_web(cfg.port) if cfg.port else None  # порт открываем первым — Render ждёт его

    if os.getenv("RENDER") and not cfg.database_url:
        log.warning("DATABASE_URL не задан: на бесплатном Render диск стирается при каждом перезапуске, "
                    "поэтому расписания и посты будут ПОТЕРЯНЫ. Подключите Postgres (см. README).")

    db = Database(cfg.db_path, cfg.database_url)
    await db.connect()
    log.info("База данных: %s", db.backend)
    await seed_default_brands(db)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = build_dispatcher(cfg, db)
    scheduler = Scheduler(bot, db, cfg)

    me = await bot.get_me()
    log.info("Бот запущен: @%s (id=%s), админы: %s, TZ=%s", me.username, me.id, sorted(cfg.admin_ids), cfg.timezone)

    scheduler_task = asyncio.create_task(scheduler.run())
    keepalive_task = (asyncio.create_task(keepalive(cfg.keepalive_url, cfg.keepalive_seconds))
                      if cfg.keepalive_url else None)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.stop()
        await scheduler_task
        if keepalive_task:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive_task
        await db.close()
        await bot.session.close()
        if runner:
            await runner.cleanup()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
