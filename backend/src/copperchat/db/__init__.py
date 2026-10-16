from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

Params = Sequence[Any] | Mapping[str, Any]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """Single aiosqlite connection in autocommit mode; explicit transactions are serialised."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._tx_task: asyncio.Task[Any] | None = None

    @classmethod
    async def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        db = cls(conn)
        await migrate(db)
        return db

    def _in_own_transaction(self) -> bool:
        return self._tx_task is not None and self._tx_task is asyncio.current_task()

    async def execute(self, sql: str, params: Params = ()) -> int:
        if self._in_own_transaction():
            cur = await self._conn.execute(sql, params)
            return cur.lastrowid or 0
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return cur.lastrowid or 0

    async def executescript(self, script: str) -> None:
        async with self._lock:
            await self._conn.executescript(script)

    async def fetchone(self, sql: str, params: Params = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Params = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return self._transaction()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        if self._in_own_transaction():
            raise RuntimeError("nested transactions are not supported")
        async with self._lock:
            self._tx_task = asyncio.current_task()
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            else:
                await self._conn.execute("COMMIT")
            finally:
                self._tx_task = None

    async def close(self) -> None:
        await self._conn.close()


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            continue
        number = int(match.group(1))
        if number in found:
            raise RuntimeError(
                f"duplicate migration number {number:04d}: {found[number].name}, {path.name}"
            )
        found[number] = path
    return sorted(found.items())


async def migrate(db: Database, directory: Path = MIGRATIONS_DIR) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in await db.fetchall("SELECT version FROM schema_migrations")}
    for number, path in list_migrations(directory):
        if number in applied:
            continue
        name = path.name.replace("'", "''")
        highest = max([number, *applied])
        await db.executescript(
            "BEGIN;\n"
            f"{path.read_text(encoding='utf-8')}\n"
            f"INSERT INTO schema_migrations(version, name, applied_at) "
            f"VALUES ({number}, '{name}', '{utcnow_iso()}');\n"
            f"PRAGMA user_version = {highest};\n"
            "COMMIT;"
        )
        applied.add(number)
