"""Чистая логика расписаний: разбор ввода, слоты времени, выбор следующего поста. Без Telegram и БД."""
from __future__ import annotations

import random
import re
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Mapping, Sequence

ORDER_TIME = "time"
ORDER_RANDOM = "random"
ORDER_RR = "round_robin"
ORDER_LABELS = {
    ORDER_TIME: "по времени (старые первыми)",
    ORDER_RANDOM: "случайный",
    ORDER_RR: "по очереди (бренды чередуются)",
}


def parse_time(text: str) -> time:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m:
        raise ValueError("Формат времени: ЧЧ:ММ, например 10:30")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ValueError("Такого времени не бывает. Формат: ЧЧ:ММ, например 10:30")
    return time(h, mi)


def parse_date(text: str, today: date) -> date:
    t = (text or "").strip().lower()
    if t in ("сегодня", "today"):
        return today
    if t in ("завтра", "tomorrow"):
        return today + timedelta(days=1)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", t)
    if m:
        try:
            return date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    raise ValueError("Формат даты: ДД.ММ.ГГГГ (или ДД.ММ, «сегодня», «завтра»)")


def build_slots(day: date, start: time, end: time, interval_min: int, tz: tzinfo) -> list[datetime]:
    """Моменты публикации: start, start+interval, ... не позже end (включительно)."""
    if interval_min <= 0:
        raise ValueError("Интервал должен быть больше нуля")
    cur = datetime.combine(day, start, tzinfo=tz)
    stop = datetime.combine(day, end, tzinfo=tz)
    if stop < cur:
        raise ValueError("Время окончания раньше времени начала")
    step = timedelta(minutes=interval_min)
    slots: list[datetime] = []
    while cur <= stop:
        slots.append(cur)
        cur += step
    return slots


def suggest_interval(posts: int, start: time, end: time) -> int | None:
    """Интервал, при котором posts постов растянутся ровно на всё окно."""
    if posts < 2:
        return None
    window = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if window <= 0:
        return None
    return max(1, window // (posts - 1))


def pick_next_post(candidates: Sequence[Mapping[str, Any]], mode: str, brand_ids: Sequence[int],
                   rr_cursor: int = 0, rng: Any = None) -> tuple[Mapping[str, Any] | None, int]:
    """Выбирает следующий пост. Возвращает (пост | None, новый курсор round-robin)."""
    if not candidates:
        return None, rr_cursor
    rng = rng or random
    by_time = lambda p: (p["posted_at"], p["id"])  # noqa: E731

    if mode == ORDER_RANDOM:
        return rng.choice(list(candidates)), rr_cursor

    if mode == ORDER_RR:
        order = sorted(set(brand_ids))
        n = len(order)
        if n == 0:
            return None, rr_cursor
        for k in range(n):
            idx = (rr_cursor + k) % n
            pool = [p for p in candidates if p["brand_id"] == order[idx]]
            if pool:
                return min(pool, key=by_time), (idx + 1) % n
        return None, rr_cursor

    return min(candidates, key=by_time), rr_cursor
