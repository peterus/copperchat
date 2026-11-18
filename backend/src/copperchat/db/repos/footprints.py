"""Footprint write journal (FP-C7, spec §9.3): one row per library write, written as
``pending`` before the first stage and finalised afterwards. ``file_rel`` is
repo-root-relative so projects of one repo name the same file the same way."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copperchat.db import Database, utcnow_iso

ABSENT = "absent"  # sha placeholder: the file did not exist
OPS = frozenset({"row_add", "create", "replace", "approve", "delete"})
STATES = frozenset({"pending", "done", "compensated", "leftover"})
FINAL_STATES = STATES - {"pending"}

_COLUMNS = (
    "id, project_id, run_id, lib_id, file_rel, op, state, pre_sha256, post_s2_sha256, "
    "post_sha256, geometry_hash, created_at, finalised_at"
)


@dataclass(frozen=True)
class FootprintWrite:
    id: int
    project_id: str
    run_id: str | None
    lib_id: str
    file_rel: str
    op: str
    state: str
    pre_sha256: str | None
    post_s2_sha256: str | None
    post_sha256: str | None
    geometry_hash: str | None
    created_at: str
    finalised_at: str | None


def _row(r: Any) -> FootprintWrite:
    return FootprintWrite(*tuple(r))


class FootprintWriteRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert_pending(
        self,
        project_id: str,
        run_id: str | None,
        lib_id: str,
        file_rel: str,
        op: str,
        pre_sha256: str | None,
        geometry_hash: str | None = None,
    ) -> int:
        if op not in OPS:
            raise ValueError(f"invalid footprint write op: {op!r}")
        return await self._db.execute(
            "INSERT INTO footprint_writes(project_id, run_id, lib_id, file_rel, op, state, "
            "pre_sha256, geometry_hash, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (project_id, run_id, lib_id, file_rel, op, pre_sha256, geometry_hash, utcnow_iso()),
        )

    async def set_post_s2(self, write_id: int, sha: str | None) -> None:
        await self._db.execute(
            "UPDATE footprint_writes SET post_s2_sha256 = ? WHERE id = ?", (sha, write_id)
        )

    async def finalise(self, write_id: int, state: str, post_sha256: str | None) -> None:
        if state not in FINAL_STATES:
            raise ValueError(f"invalid final footprint write state: {state!r}")
        await self._db.execute(
            "UPDATE footprint_writes SET state = ?, post_sha256 = ?, finalised_at = ? WHERE id = ?",
            (state, post_sha256, utcnow_iso(), write_id),
        )

    async def pending(self, project_id: str) -> list[FootprintWrite]:
        rows = await self._db.fetchall(
            f"SELECT {_COLUMNS} FROM footprint_writes "
            "WHERE project_id = ? AND state = 'pending' ORDER BY id",
            (project_id,),
        )
        return [_row(r) for r in rows]

    async def latest_for_file(self, file_rel: str) -> FootprintWrite | None:
        r = await self._db.fetchone(
            f"SELECT {_COLUMNS} FROM footprint_writes WHERE file_rel = ? ORDER BY id DESC LIMIT 1",
            (file_rel,),
        )
        return None if r is None else _row(r)

    async def later_foreign(self, file_rel: str, project_id: str, after_id: int) -> bool:
        """True iff another project journalled a write to ``file_rel`` after ``after_id``."""
        r = await self._db.fetchone(
            "SELECT 1 FROM footprint_writes WHERE file_rel = ? AND id > ? AND project_id != ? "
            "LIMIT 1",
            (file_rel, after_id, project_id),
        )
        return r is not None

    async def foreign_after(self, project_id: str, after_id: int) -> set[str]:
        """Every ``file_rel`` another project journalled a write to after ``after_id``
        (``later_foreign`` for all files in one query)."""
        rows = await self._db.fetchall(
            "SELECT DISTINCT file_rel FROM footprint_writes WHERE id > ? AND project_id != ?",
            (after_id, project_id),
        )
        return {str(r[0]) for r in rows}

    async def max_id(self) -> int:
        r = await self._db.fetchone("SELECT COALESCE(MAX(id), 0) FROM footprint_writes")
        return 0 if r is None else int(r[0])

    async def after_id(self, project_id: str, after_id: int) -> list[FootprintWrite]:
        rows = await self._db.fetchall(
            f"SELECT {_COLUMNS} FROM footprint_writes WHERE project_id = ? AND id > ? ORDER BY id",
            (project_id, after_id),
        )
        return [_row(r) for r in rows]


@dataclass(frozen=True)
class FootprintReview:
    """What CopperChat showed in one conversation for one footprint geometry (FP-C7,
    spec §7.7 guard 3): when it was first rendered and first checked (`fp_render`,
    `fp_check`, `fp_create`'s read-back check) and the error count of the latest check."""

    conversation_id: str
    lib_id: str
    geometry_hash: str
    run_id: str | None
    rendered_at: str | None
    checked_at: str | None
    check_errors: int | None


_REVIEW_COLUMNS = (
    "conversation_id, lib_id, geometry_hash, run_id, rendered_at, checked_at, check_errors"
)


class FootprintReviewRepo:
    """`footprint_reviews` (FP-C7): the server-side evidence `fp_approve` checks."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        conversation_id: str,
        lib_id: str,
        geometry_hash: str,
        run_id: str | None,
        *,
        rendered: bool = False,
        checked: bool = False,
        check_errors: int | None = None,
    ) -> None:
        """Upsert the row: `rendered` / `checked` stamp `rendered_at` / `checked_at` with
        the current time unless already stamped (the first time this geometry was shown
        counts, so a re-render or re-check in the approval turn keeps the evidence);
        `run_id` and `check_errors` take the latest value."""
        now = utcnow_iso()
        await self._db.execute(
            f"INSERT INTO footprint_reviews({_REVIEW_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id, lib_id, geometry_hash) DO UPDATE SET "
            "run_id = COALESCE(excluded.run_id, footprint_reviews.run_id), "
            "rendered_at = COALESCE(footprint_reviews.rendered_at, excluded.rendered_at), "
            "checked_at = COALESCE(footprint_reviews.checked_at, excluded.checked_at), "
            "check_errors = COALESCE(excluded.check_errors, footprint_reviews.check_errors)",
            (
                conversation_id,
                lib_id,
                geometry_hash,
                run_id,
                now if rendered else None,
                now if checked else None,
                check_errors,
            ),
        )

    async def get(
        self, conversation_id: str, lib_id: str, geometry_hash: str
    ) -> FootprintReview | None:
        r = await self._db.fetchone(
            f"SELECT {_REVIEW_COLUMNS} FROM footprint_reviews "
            "WHERE conversation_id = ? AND lib_id = ? AND geometry_hash = ?",
            (conversation_id, lib_id, geometry_hash),
        )
        return None if r is None else FootprintReview(*tuple(r))
