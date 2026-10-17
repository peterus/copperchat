"""push_subscriptions table (migration 0005): one row per browser push endpoint."""

from __future__ import annotations

from dataclasses import dataclass

from copperchat.db import Database, utcnow_iso


@dataclass(frozen=True)
class PushSub:
    id: int
    endpoint: str
    p256dh: str
    auth: str


class PushStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, endpoint: str, p256dh: str, auth: str) -> None:
        await self.db.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
            "p256dh = excluded.p256dh, auth = excluded.auth",
            (endpoint, p256dh, auth, utcnow_iso()),
        )

    async def delete(self, endpoint: str) -> bool:
        row = await self.db.fetchone(
            "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        if row is None:
            return False
        await self.delete_id(int(row["id"]))
        return True

    async def delete_id(self, sub_id: int) -> None:
        await self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))

    async def all(self) -> list[PushSub]:
        rows = await self.db.fetchall(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions ORDER BY id"
        )
        return [PushSub(int(r["id"]), r["endpoint"], r["p256dh"], r["auth"]) for r in rows]
