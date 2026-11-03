from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

from planner import (
    ORDER_RANDOM, ORDER_RR, ORDER_TIME, build_slots, parse_date, parse_time, pick_next_post, suggest_interval,
)

TZ = ZoneInfo("Europe/Moscow")


def test_parse_time():
    assert parse_time("9:05") == time(9, 5)
    assert parse_time("18.30") == time(18, 30)
    for bad in ("25:00", "10:61", "abc", ""):
        with pytest.raises(ValueError):
            parse_time(bad)


def test_parse_date():
    today = date(2026, 11, 3)
    assert parse_date("сегодня", today) == today
    assert parse_date("завтра", today) == date(2026, 11, 4)
    assert parse_date("05.11.2026", today) == date(2026, 11, 5)
    assert parse_date("05.11", today) == date(2026, 11, 5)
    with pytest.raises(ValueError):
        parse_date("когда-нибудь", today)


def test_build_slots():
    slots = build_slots(date(2026, 11, 3), time(10, 0), time(11, 0), 20, TZ)
    assert [s.strftime("%H:%M") for s in slots] == ["10:00", "10:20", "10:40", "11:00"]
    assert len(build_slots(date(2026, 11, 3), time(10, 0), time(10, 0), 15, TZ)) == 1
    with pytest.raises(ValueError):
        build_slots(date(2026, 11, 3), time(12, 0), time(10, 0), 15, TZ)
    with pytest.raises(ValueError):
        build_slots(date(2026, 11, 3), time(10, 0), time(12, 0), 0, TZ)


def test_suggest_interval():
    assert suggest_interval(7, time(10, 0), time(16, 0)) == 60
    assert suggest_interval(1, time(10, 0), time(16, 0)) is None


POSTS = [
    {"id": 1, "brand_id": 1, "posted_at": "2026-11-03T07:00:00+00:00"},
    {"id": 2, "brand_id": 1, "posted_at": "2026-11-03T08:00:00+00:00"},
    {"id": 3, "brand_id": 2, "posted_at": "2026-11-03T09:00:00+00:00"},
]


def test_order_time():
    post, _ = pick_next_post(POSTS, ORDER_TIME, [1, 2])
    assert post["id"] == 1


def test_order_random_picks_from_pool():
    import random
    post, _ = pick_next_post(POSTS, ORDER_RANDOM, [1, 2], rng=random.Random(1))
    assert post["id"] in {1, 2, 3}


def test_order_round_robin_alternates_and_skips_empty_brand():
    pool = list(POSTS)
    cursor, order = 0, []
    while pool:
        post, cursor = pick_next_post(pool, ORDER_RR, [1, 2], cursor)
        order.append(post["id"])
        pool = [p for p in pool if p["id"] != post["id"]]
    assert order == [1, 3, 2]  # бренд 1 → бренд 2 → у бренда 2 пусто, снова бренд 1


def test_empty_pool():
    assert pick_next_post([], ORDER_TIME, [1]) == (None, 0)
