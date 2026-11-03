"""Слой базы данных.

Два режима (выбирается автоматически):
  * DATABASE_URL задан  -> PostgreSQL (asyncpg). Нужен на бесплатном Render: там диск стирается
    при каждом перезапуске, поэтому SQLite-файл на нём не сохранится.
  * DATABASE_URL пуст   -> SQLite-файл (DB_PATH) — для локального запуска и тестов.

SQL во всём файле написан один раз, с плейсхолдерами «?»; для Postgres они автоматически
превращаются в $1, $2, … Таблицы создаются сами при подключении.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brands (
    id   {PK},
    name TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_brands_name ON brands (lower(name));

CREATE TABLE IF NOT EXISTS aliases (
    id       {PK},
    brand_id BIGINT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    alias    TEXT NOT NULL,
    UNIQUE (brand_id, alias)
);

-- Пост = одно сообщение или целый альбом (media group)
CREATE TABLE IF NOT EXISTS posts (
    id               {PK},
    source_chat_id   BIGINT NOT NULL,
    media_group_id   TEXT,
    first_message_id BIGINT NOT NULL,
    text             TEXT NOT NULL DEFAULT '',
    brand_id         BIGINT REFERENCES brands(id) ON DELETE SET NULL,
    brand_manual     INTEGER NOT NULL DEFAULT 0,
    posted_at        TEXT NOT NULL,          -- UTC ISO
    local_date       TEXT NOT NULL,          -- YYYY-MM-DD в часовом поясе бота
    link             TEXT,
    created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_group
    ON posts (source_chat_id, media_group_id) WHERE media_group_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_single
    ON posts (source_chat_id, first_message_id) WHERE media_group_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_posts_date_brand ON posts (local_date, brand_id);

-- Отдельные сообщения поста (элементы альбома) с file_id медиа
CREATE TABLE IF NOT EXISTS post_messages (
    id             {PK},
    post_id        BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    message_id     BIGINT NOT NULL,
    media_type     TEXT NOT NULL,
    file_id        TEXT,
    file_unique_id TEXT,
    caption        TEXT,
    UNIQUE (post_id, message_id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id           {PK},
    created_by   BIGINT NOT NULL,
    source_date  TEXT NOT NULL,      -- за какой день берём посты
    publish_date TEXT NOT NULL,      -- в какой день публикуем
    start_time   TEXT NOT NULL,      -- HH:MM
    end_time     TEXT NOT NULL,      -- HH:MM
    interval_min INTEGER NOT NULL,
    order_mode   TEXT NOT NULL CHECK (order_mode IN ('time', 'random', 'round_robin')),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'paused', 'stopped', 'finished')),
    rr_cursor    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS schedule_brands (
    schedule_id BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    brand_id    BIGINT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    PRIMARY KEY (schedule_id, brand_id)
);

-- Журнал слотов расписания: каждый слот обрабатывается ровно один раз
CREATE TABLE IF NOT EXISTS slot_log (
    schedule_id BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    slot_index  INTEGER NOT NULL,
    status      TEXT NOT NULL,       -- sent | missed | empty | error
    post_id     BIGINT,
    error       TEXT,
    at          TEXT NOT NULL,
    PRIMARY KEY (schedule_id, slot_index)
);

-- Защита от дублей: пост резервируется ДО отправки (UNIQUE), затем помечается sent/failed
CREATE TABLE IF NOT EXISTS publications (
    id          {PK},
    schedule_id BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    post_id     BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    slot_index  INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed')),
    error       TEXT,
    at          TEXT NOT NULL,
    UNIQUE (schedule_id, post_id)
);
"""

PK_SQLITE = "INTEGER PRIMARY KEY AUTOINCREMENT"
PK_PG = "BIGSERIAL PRIMARY KEY"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_PH = re.compile(r"\?")


def _to_pg(sql: str) -> str:
    """«?» -> $1, $2, … (в SQL нет строковых литералов со знаком вопроса)."""
    n = 0

    def repl(_m: re.Match) -> str:
        nonlocal n
        n += 1
        return f"${n}"

    return _PH.sub(repl, sql)


def _clean_pg_url(url: str) -> tuple[str, bool]:
    """Убирает параметры, которых не понимает asyncpg (pgbouncer=true и т.п.).
    Возвращает (url, нужен_ли_ssl)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in {"pgbouncer", "connection_limit", "schema"}]
    has_sslmode = any(k.lower() == "sslmode" for k, _ in query)
    local = (parts.hostname or "") in {"localhost", "127.0.0.1", "::1"}
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return cleaned, (not has_sslmode and not local)


# ---------- «соединение внутри транзакции» для обоих движков ----------
class _SqliteTx:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def fetchall(self, sql: str, params: Sequence[Any] = ()):
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Sequence[Any] = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        cur = await self.conn.execute(sql, params)
        await cur.close()

    async def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, seq)


class _PgTx:
    def __init__(self, conn):
        self.conn = conn

    async def fetchall(self, sql: str, params: Sequence[Any] = ()):
        return list(await self.conn.fetch(_to_pg(sql), *params))

    async def fetchone(self, sql: str, params: Sequence[Any] = ()):
        return await self.conn.fetchrow(_to_pg(sql), *params)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self.conn.execute(_to_pg(sql), *params)

    async def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        await self.conn.executemany(_to_pg(sql), seq)


class Database:
    def __init__(self, path: str = "data/bot.db", database_url: str | None = None):
        self.path = path
        self.database_url = (database_url or "").strip() or None
        self.conn: aiosqlite.Connection | None = None   # SQLite
        self.pool = None                                  # asyncpg.Pool
        self._pg_errors: tuple[type[BaseException], ...] = ()
        self._unique_errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)

    @property
    def backend(self) -> str:
        return "postgres" if self.database_url else "sqlite"

    # ---------- infra ----------
    async def connect(self) -> None:
        if self.database_url:
            await self._connect_pg()
        else:
            self.conn = await aiosqlite.connect(self.path)
            self.conn.row_factory = aiosqlite.Row
            await self.conn.execute("PRAGMA foreign_keys = ON")
            await self.conn.execute("PRAGMA journal_mode = WAL")
            await self.conn.executescript(SCHEMA.replace("{PK}", PK_SQLITE))
            await self.conn.commit()

    async def _connect_pg(self) -> None:
        try:
            import asyncpg
        except ImportError:  # pragma: no cover
            raise SystemExit("Для DATABASE_URL нужен пакет asyncpg: pip install -r requirements.txt")
        self._pg_errors = (
            asyncpg.PostgresConnectionError, asyncpg.InterfaceError, asyncpg.ConnectionDoesNotExistError,
            asyncpg.CannotConnectNowError, OSError, asyncio.TimeoutError,
        )
        self._unique_errors = (sqlite3.IntegrityError, asyncpg.UniqueViolationError)

        dsn, need_ssl = _clean_pg_url(self.database_url)
        kwargs: dict[str, Any] = dict(
            min_size=1, max_size=5, command_timeout=30,
            max_inactive_connection_lifetime=120,   # не держим «мёртвые» соединения (пулеры/облако их обрывают)
            statement_cache_size=0,                 # совместимо с pgbouncer/Supavisor в режиме transaction
        )
        if need_ssl:
            kwargs["ssl"] = "require"
        last: Exception | None = None
        for attempt in range(1, 6):
            try:
                self.pool = await asyncpg.create_pool(dsn, **kwargs)
                break
            except Exception as e:  # БД может просыпаться — пробуем несколько раз
                last = e
                log.warning("Подключение к Postgres не удалось (попытка %s/5): %s", attempt, e)
                await asyncio.sleep(3 * attempt)
        else:
            raise SystemExit(f"Не удалось подключиться к Postgres по DATABASE_URL: {last}")

        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(727272)")  # два экземпляра при деплое не подерутся
                await c.execute(SCHEMA.replace("{PK}", PK_PG))
        log.info("Postgres подключён, схема готова")

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _pg(self, fn):
        """Вызов к Postgres с повтором при обрыве соединения."""
        for attempt in range(3):
            try:
                return await fn()
            except self._pg_errors as e:
                if attempt == 2:
                    raise
                log.warning("Обрыв соединения с Postgres (%s), повтор…", e)
                await asyncio.sleep(1 + attempt)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list:
        if self.pool:
            return list(await self._pg(lambda: self.pool.fetch(_to_pg(sql), *params)))
        return await _SqliteTx(self.conn).fetchall(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()):
        if self.pool:
            return await self._pg(lambda: self.pool.fetchrow(_to_pg(sql), *params))
        return await _SqliteTx(self.conn).fetchone(sql, params)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        if self.pool:
            await self._pg(lambda: self.pool.execute(_to_pg(sql), *params))
            return
        await _SqliteTx(self.conn).execute(sql, params)
        await self.conn.commit()

    async def returning(self, sql: str, params: Sequence[Any] = ()):
        """INSERT … RETURNING …: возвращает первую строку или None (если ON CONFLICT DO NOTHING сработал)."""
        if self.pool:
            return await self._pg(lambda: self.pool.fetchrow(_to_pg(sql), *params))
        row = await _SqliteTx(self.conn).fetchone(sql, params)
        await self.conn.commit()
        return row

    @asynccontextmanager
    async def tx(self):
        """Атомарная группа запросов."""
        if self.pool:
            async with self.pool.acquire() as c:
                async with c.transaction():
                    yield _PgTx(c)
        else:
            try:
                yield _SqliteTx(self.conn)
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()

    # ---------- settings ----------
    async def get_setting(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ---------- brands ----------
    async def brands_count(self) -> int:
        return int((await self.fetchone("SELECT COUNT(*) AS c FROM brands"))["c"])

    async def add_brand(self, name: str) -> int:
        try:
            row = await self.returning("INSERT INTO brands(name) VALUES(?) RETURNING id", (name,))
        except self._unique_errors:
            raise ValueError("Бренд с таким названием уже существует")
        return int(row["id"])

    async def rename_brand(self, brand_id: int, name: str) -> None:
        try:
            await self.execute("UPDATE brands SET name=? WHERE id=?", (name, brand_id))
        except self._unique_errors:
            raise ValueError("Бренд с таким названием уже существует")

    async def delete_brand(self, brand_id: int) -> None:
        await self.execute("DELETE FROM brands WHERE id=?", (brand_id,))

    async def get_brand(self, brand_id: int):
        return await self.fetchone("SELECT * FROM brands WHERE id=?", (brand_id,))

    async def brand_id_by_name(self, name: str) -> int | None:
        row = await self.fetchone("SELECT id FROM brands WHERE lower(name)=lower(?)", (name,))
        return row["id"] if row else None

    async def brand_names(self, ids: Sequence[int]) -> list[str]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = await self.fetchall(f"SELECT name FROM brands WHERE id IN ({marks}) ORDER BY lower(name)", list(ids))
        return [r["name"] for r in rows]

    async def list_brands(self):
        return await self.fetchall("SELECT id, name FROM brands ORDER BY lower(name)")

    async def brand_aliases(self, brand_id: int) -> list[str]:
        rows = await self.fetchall("SELECT alias FROM aliases WHERE brand_id=? ORDER BY id", (brand_id,))
        return [r["alias"] for r in rows]

    async def brands_with_aliases(self) -> list[tuple[int, str, list[str]]]:
        brands = await self.list_brands()
        by_brand: dict[int, list[str]] = {}
        for r in await self.fetchall("SELECT brand_id, alias FROM aliases ORDER BY id"):
            by_brand.setdefault(r["brand_id"], []).append(r["alias"])
        return [(b["id"], b["name"], by_brand.get(b["id"], [])) for b in brands]

    async def set_aliases(self, brand_id: int, aliases: Sequence[str]) -> None:
        async with self.tx() as t:
            await t.execute("DELETE FROM aliases WHERE brand_id=?", (brand_id,))
            await t.executemany(
                "INSERT INTO aliases(brand_id, alias) VALUES(?, ?) ON CONFLICT DO NOTHING",
                [(brand_id, a) for a in aliases],
            )

    async def add_aliases(self, brand_id: int, aliases: Sequence[str]) -> None:
        async with self.tx() as t:
            await t.executemany(
                "INSERT INTO aliases(brand_id, alias) VALUES(?, ?) ON CONFLICT DO NOTHING",
                [(brand_id, a) for a in aliases],
            )

    # ---------- posts ----------
    async def post_by_member(self, chat_id: int, message_id: int):
        return await self.fetchone(
            "SELECT p.* FROM posts p JOIN post_messages m ON m.post_id = p.id "
            "WHERE p.source_chat_id=? AND m.message_id=?",
            (chat_id, message_id),
        )

    async def post_by_group(self, chat_id: int, group_id: str):
        return await self.fetchone(
            "SELECT * FROM posts WHERE source_chat_id=? AND media_group_id=?", (chat_id, group_id)
        )

    async def create_post(self, chat_id: int, group_id: str | None, message_id: int, text: str,
                          posted_at: str, local_date: str, link: str | None) -> int:
        row = await self.returning(
            "INSERT INTO posts(source_chat_id, media_group_id, first_message_id, text, posted_at, "
            "local_date, link, created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (chat_id, group_id, message_id, text, posted_at, local_date, link, utcnow()),
        )
        return int(row["id"])

    async def upsert_post_message(self, post_id: int, message_id: int, media_type: str,
                                  file_id: str | None, file_unique_id: str | None, caption: str | None) -> None:
        await self.execute(
            "INSERT INTO post_messages(post_id, message_id, media_type, file_id, file_unique_id, caption) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(post_id, message_id) DO UPDATE SET "
            "media_type=excluded.media_type, file_id=excluded.file_id, "
            "file_unique_id=excluded.file_unique_id, caption=excluded.caption",
            (post_id, message_id, media_type, file_id, file_unique_id, caption),
        )

    async def update_post_first_message(self, post_id: int, message_id: int, link: str | None) -> None:
        await self.execute("UPDATE posts SET first_message_id=?, link=? WHERE id=?", (message_id, link, post_id))

    async def update_post_text(self, post_id: int, text: str) -> None:
        await self.execute("UPDATE posts SET text=? WHERE id=?", (text, post_id))

    async def set_post_brand(self, post_id: int, brand_id: int | None, manual: bool) -> None:
        await self.execute(
            "UPDATE posts SET brand_id=?, brand_manual=? WHERE id=?", (brand_id, 1 if manual else 0, post_id)
        )

    async def get_post(self, post_id: int):
        return await self.fetchone("SELECT * FROM posts WHERE id=?", (post_id,))

    async def post_message_ids(self, post_id: int) -> list[int]:
        rows = await self.fetchall("SELECT message_id FROM post_messages WHERE post_id=? ORDER BY message_id", (post_id,))
        return [r["message_id"] for r in rows]

    async def auto_posts(self):
        """Посты без ручного бренда — кандидаты на переопределение."""
        return await self.fetchall("SELECT id, text, brand_id FROM posts WHERE brand_manual=0")

    async def brand_counts(self, local_date: str):
        return await self.fetchall(
            "SELECT p.brand_id AS brand_id, COALESCE(b.name, '') AS name, COUNT(*) AS cnt "
            "FROM posts p LEFT JOIN brands b ON b.id = p.brand_id WHERE p.local_date=? "
            "GROUP BY p.brand_id, b.name "
            "ORDER BY (p.brand_id IS NULL), COUNT(*) DESC, lower(COALESCE(b.name, ''))",
            (local_date,),
        )

    async def posts_for_brand(self, local_date: str, brand_id: int | None, limit: int = 15):
        if brand_id is None:
            return await self.fetchall(
                "SELECT * FROM posts WHERE local_date=? AND brand_id IS NULL ORDER BY posted_at, id LIMIT ?",
                (local_date, limit),
            )
        return await self.fetchall(
            "SELECT * FROM posts WHERE local_date=? AND brand_id=? ORDER BY posted_at, id LIMIT ?",
            (local_date, brand_id, limit),
        )

    async def media_counts(self, post_ids: Sequence[int]) -> dict[int, list[tuple[str, int]]]:
        if not post_ids:
            return {}
        marks = ",".join("?" * len(post_ids))
        rows = await self.fetchall(
            f"SELECT post_id, media_type, COUNT(*) AS c FROM post_messages WHERE post_id IN ({marks}) "
            "GROUP BY post_id, media_type",
            list(post_ids),
        )
        out: dict[int, list[tuple[str, int]]] = {}
        for r in rows:
            out.setdefault(r["post_id"], []).append((r["media_type"], int(r["c"])))
        return out

    async def pool_posts(self, source_date: str, brand_ids: Sequence[int], schedule_id: int | None = None,
                         exclude_global: bool = True):
        """Посты, доступные для публикации: нужного дня и брендов, ещё не публиковавшиеся."""
        if not brand_ids:
            return []
        marks = ",".join("?" * len(brand_ids))
        sql = f"SELECT * FROM posts WHERE local_date=? AND brand_id IN ({marks})"
        params: list[Any] = [source_date, *brand_ids]
        if schedule_id is not None:
            sql += " AND id NOT IN (SELECT post_id FROM publications WHERE schedule_id=?)"
            params.append(schedule_id)
        if exclude_global:
            sql += " AND id NOT IN (SELECT post_id FROM publications WHERE status IN ('sending','sent'))"
        sql += " ORDER BY posted_at, id"
        return await self.fetchall(sql, params)

    # ---------- schedules ----------
    async def create_schedule(self, created_by: int, source_date: str, publish_date: str, start_time: str,
                              end_time: str, interval_min: int, order_mode: str, brand_ids: Sequence[int]) -> int:
        async with self.tx() as t:
            row = await t.fetchone(
                "INSERT INTO schedules(created_by, source_date, publish_date, start_time, end_time, interval_min, "
                "order_mode, created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                (created_by, source_date, publish_date, start_time, end_time, interval_min, order_mode, utcnow()),
            )
            sid = int(row["id"])
            await t.executemany(
                "INSERT INTO schedule_brands(schedule_id, brand_id) VALUES(?, ?)", [(sid, b) for b in brand_ids]
            )
        return sid

    async def get_schedule(self, schedule_id: int):
        return await self.fetchone("SELECT * FROM schedules WHERE id=?", (schedule_id,))

    async def live_schedules(self):
        """Активные и приостановленные."""
        return await self.fetchall("SELECT * FROM schedules WHERE status IN ('active','paused') ORDER BY id")

    async def active_schedules(self):
        return await self.fetchall("SELECT * FROM schedules WHERE status='active' ORDER BY id")

    async def schedule_brand_ids(self, schedule_id: int) -> list[int]:
        rows = await self.fetchall("SELECT brand_id FROM schedule_brands WHERE schedule_id=? ORDER BY brand_id", (schedule_id,))
        return [r["brand_id"] for r in rows]

    async def set_schedule_status(self, schedule_id: int, status: str) -> None:
        finished = utcnow() if status in ("stopped", "finished") else None
        await self.execute("UPDATE schedules SET status=?, finished_at=? WHERE id=?", (status, finished, schedule_id))

    async def set_rr_cursor(self, schedule_id: int, cursor: int) -> None:
        await self.execute("UPDATE schedules SET rr_cursor=? WHERE id=?", (cursor, schedule_id))

    async def done_slots(self, schedule_id: int) -> set[int]:
        rows = await self.fetchall("SELECT slot_index FROM slot_log WHERE schedule_id=?", (schedule_id,))
        return {r["slot_index"] for r in rows}

    async def log_slot(self, schedule_id: int, slot_index: int, status: str,
                       post_id: int | None = None, error: str | None = None) -> None:
        await self.execute(
            "INSERT INTO slot_log(schedule_id, slot_index, status, post_id, error, at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(schedule_id, slot_index) DO UPDATE SET status=excluded.status, "
            "post_id=excluded.post_id, error=excluded.error, at=excluded.at",
            (schedule_id, slot_index, status, post_id, error, utcnow()),
        )

    async def slot_stats(self, schedule_id: int) -> dict[str, int]:
        rows = await self.fetchall(
            "SELECT status, COUNT(*) AS c FROM slot_log WHERE schedule_id=? GROUP BY status", (schedule_id,)
        )
        return {r["status"]: int(r["c"]) for r in rows}

    async def claim_post(self, schedule_id: int, post_id: int, slot_index: int) -> bool:
        """Резервирует пост за расписанием. False — пост уже был взят (дубль)."""
        row = await self.returning(
            "INSERT INTO publications(schedule_id, post_id, slot_index, status, at) "
            "VALUES(?,?,?, 'sending', ?) ON CONFLICT DO NOTHING RETURNING id",
            (schedule_id, post_id, slot_index, utcnow()),
        )
        return row is not None

    async def release_claim(self, schedule_id: int, post_id: int) -> None:
        await self.execute("DELETE FROM publications WHERE schedule_id=? AND post_id=? AND status='sending'",
                           (schedule_id, post_id))

    async def mark_publication(self, schedule_id: int, post_id: int, status: str, error: str | None = None) -> None:
        await self.execute(
            "UPDATE publications SET status=?, error=?, at=? WHERE schedule_id=? AND post_id=?",
            (status, error, utcnow(), schedule_id, post_id),
        )
