"""Board checkpoints and their evidence (B-C10, spec §8.3, migration `0013_board.sql`).

`board_checkpoints` holds one row per user approval of a stage (`placement`, `board`);
only the latest row per (project, board file, stage) counts. `board_reviews` holds one
row per piece of evidence CopperChat showed in a conversation: a `pcb_render` (`kind =
render`, `view`) or a `pcb_check` (`kind = check`, `check_level`, `hard_failures`,
`m1_pass`), each with both hashes of the state it was produced for."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copperchat.db import Database, utcnow_iso
from copperchat.projects import Project


def board_file(project: Project) -> str:
    """The board path relative to the project directory (POSIX), the key of every row;
    the file name when the board lies outside the directory."""
    assert project.pcb_file is not None
    try:
        rel = project.pcb_file.relative_to(project.dir)
    except ValueError:
        rel = Path(project.pcb_file.name)
    return rel.as_posix()


@dataclass(frozen=True)
class Checkpoint:
    id: int
    project_id: str
    board_file: str
    stage: str
    hash: str
    conversation_id: str
    run_id: str
    user_quote: str
    approved_at: str


@dataclass(frozen=True)
class Review:
    id: int
    project_id: str
    board_file: str
    conversation_id: str | None
    stage: str | None
    placement_hash: str
    board_hash: str
    run_id: str | None
    kind: str
    view: str | None
    check_level: str | None
    hard_failures: int | None
    m1_pass: int | None
    created_at: str


_CP_COLS = (
    "id, project_id, board_file, stage, hash, conversation_id, run_id, user_quote, approved_at"
)
_RV_COLS = (
    "id, project_id, board_file, conversation_id, stage, placement_hash, board_hash, run_id, "
    "kind, view, check_level, hard_failures, m1_pass, created_at"
)


def _checkpoint(r: Any) -> Checkpoint:
    return Checkpoint(*tuple(r))


def _review(r: Any) -> Review:
    return Review(*tuple(r))


class BoardCheckpointRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def approve(
        self,
        project_id: str,
        board_file: str,
        stage: str,
        hash: str,
        conversation_id: str,
        run_id: str,
        user_quote: str,
    ) -> int:
        return await self._db.execute(
            f"INSERT INTO board_checkpoints({_CP_COLS.removeprefix('id, ')}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, board_file, stage, hash, conversation_id, run_id, user_quote,
             utcnow_iso()),
        )  # fmt: skip

    async def latest(self, project_id: str, board_file: str, stage: str) -> Checkpoint | None:
        """The approval that counts: the highest id of (project, board file, stage)."""
        r = await self._db.fetchone(
            f"SELECT {_CP_COLS} FROM board_checkpoints WHERE project_id = ? AND board_file = ? "
            "AND stage = ? ORDER BY id DESC LIMIT 1",
            (project_id, board_file, stage),
        )
        return None if r is None else _checkpoint(r)


class BoardReviewRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def _insert(self, values: dict[str, Any]) -> int:
        values = {**values, "created_at": utcnow_iso()}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return await self._db.execute(
            f"INSERT INTO board_reviews({cols}) VALUES ({marks})", tuple(values.values())
        )

    async def record_render(
        self,
        project_id: str,
        board_file: str,
        conversation_id: str | None,
        run_id: str | None,
        *,
        view: str,
        placement_hash: str,
        board_hash: str,
    ) -> int:
        return await self._insert(
            {
                "project_id": project_id,
                "board_file": board_file,
                "conversation_id": conversation_id,
                "run_id": run_id,
                "kind": "render",
                "view": view,
                "placement_hash": placement_hash,
                "board_hash": board_hash,
            }
        )

    async def record_check(
        self,
        project_id: str,
        board_file: str,
        conversation_id: str | None,
        run_id: str | None,
        *,
        level: str,
        hard_failures: int,
        m1_pass: bool,
        placement_hash: str,
        board_hash: str,
        stage: str,
    ) -> int:
        return await self._insert(
            {
                "project_id": project_id,
                "board_file": board_file,
                "conversation_id": conversation_id,
                "run_id": run_id,
                "kind": "check",
                "check_level": level,
                "hard_failures": hard_failures,
                "m1_pass": 1 if m1_pass else 0,
                "placement_hash": placement_hash,
                "board_hash": board_hash,
                "stage": stage,
            }
        )

    async def evidence(
        self,
        project_id: str,
        board_file: str,
        conversation_id: str,
        *,
        placement_hash: str | None = None,
        board_hash: str | None = None,
    ) -> list[Review]:
        """Every row of this conversation for the board, filtered by the given hashes,
        oldest first."""
        sql = (
            f"SELECT {_RV_COLS} FROM board_reviews WHERE project_id = ? AND board_file = ? "
            "AND conversation_id = ?"
        )
        params: list[Any] = [project_id, board_file, conversation_id]
        if placement_hash is not None:
            sql += " AND placement_hash = ?"
            params.append(placement_hash)
        if board_hash is not None:
            sql += " AND board_hash = ?"
            params.append(board_hash)
        rows = await self._db.fetchall(sql + " ORDER BY id", params)
        return [_review(r) for r in rows]

    async def last_full_check(
        self, project_id: str, board_file: str, board_hash: str
    ) -> Review | None:
        """The newest `full` check of the board for `board_hash` (any conversation)."""
        r = await self._db.fetchone(
            f"SELECT {_RV_COLS} FROM board_reviews WHERE project_id = ? AND board_file = ? "
            "AND board_hash = ? AND kind = 'check' AND check_level = 'full' "
            "ORDER BY id DESC LIMIT 1",
            (project_id, board_file, board_hash),
        )
        return None if r is None else _review(r)

    async def last_check(
        self, project_id: str, board_file: str, conversation_id: str
    ) -> Review | None:
        """The newest check row of the board in this conversation (`pcb_status`)."""
        r = await self._db.fetchone(
            f"SELECT {_RV_COLS} FROM board_reviews WHERE project_id = ? AND board_file = ? "
            "AND conversation_id = ? AND kind = 'check' ORDER BY id DESC LIMIT 1",
            (project_id, board_file, conversation_id),
        )
        return None if r is None else _review(r)
