"""Фоновый планировщик: раз в несколько секунд проверяет активные расписания и публикует посты.

Модель: расписание = набор слотов (start, start+interval, … ≤ end). В момент каждого слота берётся
следующий подходящий пост из «пула» (посты нужного дня и брендов, ещё не опубликованные).
Состояние хранится в базе данных (Postgres или SQLite), поэтому после перезапуска расписание продолжается с того же места.
Слот, опоздавший больше MISSED_GRACE_MINUTES (бот был выключен), помечается «missed» и не публикуется.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from config import Config
from db import Database
from planner import build_slots, parse_time, pick_next_post
from poster import publish_post
from utils import fmt_date, get_channel_id, notify_admins

log = logging.getLogger(__name__)
MAX_POST_ATTEMPTS_PER_SLOT = 3


def schedule_slots(sch, cfg: Config) -> list[datetime]:
    return build_slots(
        date.fromisoformat(sch["publish_date"]), parse_time(sch["start_time"]),
        parse_time(sch["end_time"]), sch["interval_min"], cfg.timezone,
    )


class Scheduler:
    def __init__(self, bot: Bot, db: Database, cfg: Config, tick_seconds: float = 10.0):
        self.bot, self.db, self.cfg = bot, db, cfg
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()
        self._cooldown_until: datetime | None = None  # после FloodWait

    # ---------- цикл ----------
    async def run(self) -> None:
        log.info("Планировщик запущен (проверка каждые %.0f с)", self.tick_seconds)
        while not self._stop.is_set():
            try:
                await self.tick(datetime.now(self.cfg.timezone))
            except Exception:
                log.exception("Ошибка в цикле планировщика")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def tick(self, now: datetime) -> None:
        if self._cooldown_until and now < self._cooldown_until:
            return
        for sch in await self.db.active_schedules():
            try:
                await self._process(sch, now)
            except Exception:
                log.exception("Ошибка обработки расписания #%s", sch["id"])

    # ---------- одно расписание ----------
    async def _process(self, sch, now: datetime) -> None:
        sid = sch["id"]
        slots = schedule_slots(sch, self.cfg)
        done = await self.db.done_slots(sid)
        grace = timedelta(minutes=self.cfg.missed_grace_minutes)

        for idx, when in enumerate(slots):
            if idx in done:
                continue
            if when > now:
                break  # дальше только будущие слоты
            if now - when > grace:
                await self.db.log_slot(sid, idx, "missed")
                log.warning("Расписание #%s: слот %s (%s) пропущен — опоздание больше %s мин",
                            sid, idx, when.strftime("%H:%M"), self.cfg.missed_grace_minutes)
                continue
            outcome = await self._run_slot(sch, idx)
            if outcome == "retry":
                return
            break  # один слот за проход

        if len(await self.db.done_slots(sid)) >= len(slots):
            await self._finish(sch)

    async def _run_slot(self, sch, idx: int) -> str:
        sid = sch["id"]
        source = await get_channel_id(self.db, self.cfg, "source")
        target = await get_channel_id(self.db, self.cfg, "target")
        if not source or not target:
            await self._pause(sch, "не заданы исходный/целевой каналы")
            return "retry"

        brand_ids = await self.db.schedule_brand_ids(sid)
        last_error = None
        for _ in range(MAX_POST_ATTEMPTS_PER_SLOT):
            pool = await self.db.pool_posts(
                sch["source_date"], brand_ids, schedule_id=sid, exclude_global=not self.cfg.allow_repost
            )
            post, new_cursor = pick_next_post(pool, sch["order_mode"], brand_ids, sch["rr_cursor"])
            if post is None:
                await self.db.log_slot(sid, idx, "empty")
                log.info("Расписание #%s: слот %s — подходящих постов нет", sid, idx)
                return "empty"

            pid = post["id"]
            if not await self.db.claim_post(sid, pid, idx):
                continue  # пост уже взят (защита от дубля) — берём следующий

            try:
                await publish_post(self.bot, source, target, await self.db.post_message_ids(pid))
            except TelegramRetryAfter as e:
                await self.db.release_claim(sid, pid)
                self._cooldown_until = datetime.now(self.cfg.timezone) + timedelta(seconds=e.retry_after + 1)
                log.warning("FloodWait %s с — планировщик на паузе", e.retry_after)
                return "retry"
            except (TelegramNetworkError, TelegramServerError) as e:
                await self.db.release_claim(sid, pid)
                log.warning("Сетевая ошибка Telegram, повтор на следующем проходе: %s", e)
                return "retry"
            except TelegramForbiddenError as e:
                await self.db.release_claim(sid, pid)
                await self._pause(sch, f"нет прав в канале: {e}")
                return "retry"
            except TelegramBadRequest as e:
                last_error = str(e)
                await self.db.mark_publication(sid, pid, "failed", last_error)
                log.error("Расписание #%s: пост #%s не опубликован: %s", sid, pid, e)
                await notify_admins(self.bot, self.cfg,
                                    f"⚠️ Расписание #{sid}: пост #{pid} пропущен — Telegram отклонил публикацию:\n{e}")
                continue
            except Exception as e:
                last_error = repr(e)
                await self.db.mark_publication(sid, pid, "failed", last_error)
                log.exception("Расписание #%s: неожиданная ошибка при публикации поста #%s", sid, pid)
                continue

            await self.db.mark_publication(sid, pid, "sent")
            await self.db.log_slot(sid, idx, "sent", post_id=pid)
            await self.db.set_rr_cursor(sid, new_cursor)
            log.info("Расписание #%s: слот %s — опубликован пост #%s (бренд %s)", sid, idx, pid, post["brand_id"])
            return "sent"

        await self.db.log_slot(sid, idx, "error", error=last_error)
        return "error"

    async def _pause(self, sch, reason: str) -> None:
        await self.db.set_schedule_status(sch["id"], "paused")
        log.error("Расписание #%s поставлено на паузу: %s", sch["id"], reason)
        await notify_admins(
            self.bot, self.cfg,
            f"⏸ Расписание #{sch['id']} приостановлено: {reason}\n"
            "Проверьте права бота («Настройки каналов» → «Проверить доступ») и возобновите в «Активные расписания».",
        )

    async def _finish(self, sch) -> None:
        sid = sch["id"]
        await self.db.set_schedule_status(sid, "finished")
        st = await self.db.slot_stats(sid)
        log.info("Расписание #%s завершено: %s", sid, st)
        await notify_admins(
            self.bot, self.cfg,
            f"✅ Расписание #{sid} ({fmt_date(sch['publish_date'])}, {sch['start_time']}–{sch['end_time']}) завершено.\n"
            f"Опубликовано: {st.get('sent', 0)} · пропущено слотов: {st.get('missed', 0)} · "
            f"пустых слотов (не хватило постов): {st.get('empty', 0)} · ошибок: {st.get('error', 0)}",
        )
