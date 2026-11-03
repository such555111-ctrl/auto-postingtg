"""Слой БД: SQLite (то, что можно проверить без сервера) + преобразование для Postgres."""
import asyncio

import pytest

from db import Database, _clean_pg_url, _to_pg


def run(coro):
    return asyncio.run(coro)


def test_placeholders_to_pg():
    assert _to_pg("SELECT * FROM t WHERE a=? AND b IN (?,?)") == "SELECT * FROM t WHERE a=$1 AND b IN ($2,$3)"


def test_clean_pg_url():
    url, ssl = _clean_pg_url("postgresql://u:p@db.example.com:5432/postgres?pgbouncer=true&sslmode=require")
    assert "pgbouncer" not in url and "sslmode=require" in url and ssl is False
    url, ssl = _clean_pg_url("postgresql://u:p@db.example.com/postgres")
    assert ssl is True
    _, ssl = _clean_pg_url("postgresql://u:p@localhost/postgres")
    assert ssl is False


def test_brands_aliases_and_duplicates():
    async def scenario():
        db = Database(":memory:")
        await db.connect()
        bid = await db.add_brand("Chanel")
        assert isinstance(bid, int)
        with pytest.raises(ValueError):
            await db.add_brand("chanel")              # без учёта регистра
        await db.set_aliases(bid, ["шанель", "chanel"])
        await db.add_aliases(bid, ["chanel", "coco"])  # дубль игнорируется
        assert await db.brand_aliases(bid) == ["шанель", "chanel", "coco"]
        assert await db.brand_id_by_name("CHANEL") == bid
        await db.rename_brand(bid, "CHANEL")
        assert [b["name"] for b in await db.list_brands()] == ["CHANEL"]
        await db.delete_brand(bid)
        assert await db.brands_count() == 0
        await db.close()

    run(scenario())


def test_posts_counts_and_claims():
    async def scenario():
        db = Database(":memory:")
        await db.connect()
        b1, b2 = await db.add_brand("Chanel"), await db.add_brand("Chopard")
        p1 = await db.create_post(-100, "g1", 10, "a", "2026-11-03T07:00:00+00:00", "2026-11-03", None)
        p2 = await db.create_post(-100, None, 20, "b", "2026-11-03T08:00:00+00:00", "2026-11-03", None)
        p3 = await db.create_post(-100, None, 30, "c", "2026-11-03T09:00:00+00:00", "2026-11-03", None)
        await db.set_post_brand(p1, b1, False)
        await db.set_post_brand(p2, b1, False)
        await db.set_post_brand(p3, None, False)
        counts = [(r["name"], r["cnt"]) for r in await db.brand_counts("2026-11-03")]
        assert counts == [("Chanel", 2), ("", 1)]      # «не определён» — последним

        sid = await db.create_schedule(1, "2026-11-03", "2026-11-03", "10:00", "11:00", 10, "time", [b1, b2])
        assert await db.schedule_brand_ids(sid) == sorted([b1, b2])
        assert await db.claim_post(sid, p1, 0) is True
        assert await db.claim_post(sid, p1, 1) is False  # дубль невозможен
        pool = await db.pool_posts("2026-11-03", [b1], schedule_id=sid)
        assert [p["id"] for p in pool] == [p2]
        await db.mark_publication(sid, p1, "sent")
        await db.log_slot(sid, 0, "sent", post_id=p1)
        await db.log_slot(sid, 0, "error", error="x")    # повторная запись слота заменяет прежнюю
        assert await db.slot_stats(sid) == {"error": 1}
        await db.close()

    run(scenario())


def test_set_setting_overwrite():
    async def scenario():
        db = Database(":memory:")
        await db.connect()
        await db.set_setting("k", "1")
        await db.set_setting("k", 2)
        assert await db.get_setting("k") == "2"
        await db.close()

    run(scenario())
