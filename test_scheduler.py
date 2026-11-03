"""Сквозной тест расписания: БД SQLite в памяти + поддельный бот (без сети)."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from brands import seed_default_brands
from config import Config
from db import Database
from scheduler import Scheduler

TZ = ZoneInfo("Europe/Moscow")
DAY = "2026-11-03"
SRC, DST = -1001111111111, -1002222222222


def make_cfg(**kw) -> Config:
    base = dict(bot_token="x", admin_ids=frozenset({1}), source_channel_id=SRC, target_channel_id=DST,
                timezone=TZ, db_path=":memory:", log_level="INFO", log_file="", missed_grace_minutes=10,
                allow_repost=False)
    base.update(kw)
    return Config(**base)


class FakeBot:
    def __init__(self, fail_message_ids=()):
        self.copied, self.notes = [], []
        self.fail = set(fail_message_ids)

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        if self.fail & set(message_ids):
            raise RuntimeError("boom")
        self.copied.append((chat_id, from_chat_id, list(message_ids)))
        return [object() for _ in message_ids]

    async def send_message(self, chat_id, text, **kw):
        self.notes.append(text)


def at(hhmm: str, sec: int = 5) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 3, h, m, sec, tzinfo=TZ)


async def add_post(db, brand, first_msg, album_size=1):
    """Пост-альбом из album_size сообщений; возвращает id поста."""
    bid = await db.brand_id_by_name(brand)
    pid = await db.create_post(SRC, f"g{first_msg}" if album_size > 1 else None, first_msg, f"{brand} пост",
                               f"2026-11-03T0{first_msg % 9}:00:00+00:00", DAY, None)
    await db.set_post_brand(pid, bid, manual=False)
    for i in range(album_size):
        await db.upsert_post_message(pid, first_msg + i, "photo", f"f{first_msg + i}", f"u{first_msg + i}", None)
    return pid


async def make_db() -> Database:
    db = Database(":memory:")
    await db.connect()
    await seed_default_brands(db)
    return db


def test_round_robin_no_duplicates_and_finish():
    async def scenario():
        db, cfg, bot = await make_db(), make_cfg(), FakeBot()
        for i in (1, 2, 3):
            await add_post(db, "Chanel", i * 10, album_size=3)
        for i in (4, 5):
            await add_post(db, "Chopard", i * 10)
        brands = [await db.brand_id_by_name("Chanel"), await db.brand_id_by_name("Chopard")]
        sid = await db.create_schedule(1, DAY, DAY, "10:00", "10:30", 10, "round_robin", brands)

        sch = Scheduler(bot, db, cfg)
        for hhmm in ("10:00", "10:10", "10:20", "10:30"):
            await sch.tick(at(hhmm))
        await sch.tick(at("10:30", 40))  # лишний проход не должен ничего публиковать

        sent_ids = [c[2] for c in bot.copied]
        assert len(sent_ids) == 4 and len({tuple(x) for x in sent_ids}) == 4          # без повторов
        assert sent_ids[0] == [10, 11, 12]                                            # альбом копируется целиком
        assert all(c[0] == DST and c[1] == SRC for c in bot.copied)
        assert (await db.get_schedule(sid))["status"] == "finished"
        assert any("завершено" in n for n in bot.notes)
        await db.close()

    asyncio.run(scenario())


def test_missed_slots_after_downtime_are_skipped():
    async def scenario():
        db, cfg, bot = await make_db(), make_cfg(), FakeBot()
        await add_post(db, "Chanel", 10)
        sid = await db.create_schedule(1, DAY, DAY, "10:00", "10:20", 10, "time",
                                       [await db.brand_id_by_name("Chanel")])
        await Scheduler(bot, db, cfg).tick(at("12:00"))  # бот включили через 1,5 часа после окна
        assert bot.copied == []
        assert (await db.slot_stats(sid)).get("missed") == 3
        assert (await db.get_schedule(sid))["status"] == "finished"
        await db.close()

    asyncio.run(scenario())


def test_failed_post_is_skipped_and_next_one_published_in_same_slot():
    async def scenario():
        db, cfg = await make_db(), make_cfg()
        bot = FakeBot(fail_message_ids={10})
        p_bad = await add_post(db, "Chanel", 10)
        p_ok = await add_post(db, "Chanel", 20)
        sid = await db.create_schedule(1, DAY, DAY, "10:00", "10:00", 10, "time",
                                       [await db.brand_id_by_name("Chanel")])
        await Scheduler(bot, db, cfg).tick(at("10:00"))
        assert [c[2] for c in bot.copied] == [[20]]
        rows = {r["post_id"]: r["status"] for r in await db.fetchall("SELECT * FROM publications WHERE schedule_id=?", (sid,))}
        assert rows == {p_bad: "failed", p_ok: "sent"}
        await db.close()

    asyncio.run(scenario())


def test_post_is_not_repeated_across_schedules():
    async def scenario():
        db, cfg, bot = await make_db(), make_cfg(), FakeBot()
        await add_post(db, "Chanel", 10)
        chanel = [await db.brand_id_by_name("Chanel")]
        await db.create_schedule(1, DAY, DAY, "10:00", "10:00", 10, "time", chanel)
        await db.create_schedule(1, DAY, DAY, "10:00", "10:00", 10, "time", chanel)
        await Scheduler(bot, db, cfg).tick(at("10:00"))
        assert len(bot.copied) == 1  # второе расписание не повторяет уже опубликованный пост
        await db.close()

    asyncio.run(scenario())


def test_schedule_survives_restart(tmp_path):
    """Расписание и посты лежат в файле SQLite: после «перезапуска» продолжаем с того же места."""
    async def scenario():
        path = str(tmp_path / "bot.db")
        cfg = make_cfg(db_path=path)
        db = Database(path)
        await db.connect()
        await seed_default_brands(db)
        await add_post(db, "Chanel", 10)
        await add_post(db, "Chanel", 20)
        sid = await db.create_schedule(1, DAY, DAY, "10:00", "10:10", 10, "time", [await db.brand_id_by_name("Chanel")])
        bot1 = FakeBot()
        await Scheduler(bot1, db, cfg).tick(at("10:00"))
        await db.close()

        db2 = Database(path)
        await db2.connect()
        bot2 = FakeBot()
        await Scheduler(bot2, db2, cfg).tick(at("10:10"))
        assert [c[2] for c in bot1.copied] == [[10]] and [c[2] for c in bot2.copied] == [[20]]
        assert (await db2.get_schedule(sid))["status"] == "finished"
        await db2.close()

    asyncio.run(scenario())
