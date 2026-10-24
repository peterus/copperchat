"""ERC baseline and delta (E-C10, spec §5.1 "ERC baseline" / "No double runs", §8.3).

The delta compares the current ERC with the ERC of a snapshot's files (the turn
snapshot for an editing turn, the step checkpoint for a plan step). The baseline runs
`kicad-cli sch erc` once on a temporary copy of the snapshot and is cached as
`<snapdir>/erc.json` (the snapshot's own files are never touched; restore only reads
the files its manifest lists).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import tempfile
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from copperchat.db import Database, utcnow_iso
from copperchat.errors import CopperChatError
from copperchat.history.restore import snapshot_plan
from copperchat.history.store import Snapshot
from copperchat.kicad_session import KicadSession, KicadUnavailable
from copperchat.kicad_tools.checks import (
    PROJECT_SUPPORT_FILES,
    check_violations,
    parse_report,
    run_kicad_check_on_dir,
)
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.editing.state import EditTurnState
    from copperchat.history.service import HistoryService

BASELINE_FILE = "erc.json"
COUNTED = ("error", "warning")  # exclusions / ignored / info never count as new
_ID_RE_LEN = 32


class InvalidErcResultId(CopperChatError):
    code = "invalid_argument"
    http_status = 400


class ErcResultNotFound(CopperChatError):
    code = "not_found"
    http_status = 404


def violation_id(v: dict[str, Any]) -> str:
    """Stable id of a violation: type + its items' UUIDs (moving an item keeps it)."""
    uuids = sorted(str(i.get("uuid", "")) for i in v.get("items", []))
    key = "|".join([str(v.get("type", "")), *uuids])
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:12]


@dataclass
class ErcDelta:
    new_errors: list[dict[str, Any]] = field(default_factory=list)
    new_warnings: list[dict[str, Any]] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "new_errors": self.new_errors,
            "new_warnings": self.new_warnings,
            "resolved": self.resolved,
        }


def _where(v: dict[str, Any]) -> tuple[str, float, float]:
    pos = v.get("position_mm") or {}
    return (str(v.get("sheet", "")), float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    sa, xa, ya = _where(a)
    sb, xb, yb = _where(b)
    # Another sheet instance is always farther than any position on the same sheet.
    return math.hypot(xa - xb, ya - yb) + (1e9 if sa != sb else 0.0)


def _match(
    base: list[dict[str, Any]], cur: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(unmatched current, unmatched baseline) of one violation id: closest pairs first,
    so of two violations on the same items the one at a new place is the new one."""
    pairs = sorted(
        ((_distance(b, c), i, j) for i, b in enumerate(base) for j, c in enumerate(cur)),
        key=lambda t: t[0],
    )
    used_b: set[int] = set()
    used_c: set[int] = set()
    for _, i, j in pairs:
        if i not in used_b and j not in used_c:
            used_b.add(i)
            used_c.add(j)
    return (
        [c for j, c in enumerate(cur) if j not in used_c],
        [b for i, b in enumerate(base) if i not in used_b],
    )


def erc_delta(baseline: list[dict[str, Any]], current: list[dict[str, Any]]) -> ErcDelta:
    """New errors / new warnings / resolved violations (errors and warnings only),
    matched by `violation_id` with the position as tie-breaker. Entries gain `id`."""

    def grouped(vs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for v in vs:
            if v.get("severity") in COUNTED:
                out.setdefault(violation_id(v), []).append(v)
        return out

    base, cur = grouped(baseline), grouped(current)
    delta = ErcDelta()
    for vid in sorted(base.keys() | cur.keys()):
        new, gone = _match(base.get(vid, []), cur.get(vid, []))
        for v in new:
            target = delta.new_errors if v["severity"] == "error" else delta.new_warnings
            target.append({"id": vid, **v})
        delta.resolved.extend({"id": vid, **v} for v in gone)
    return delta


# One baseline run per snapshot at a time (turn end and a REST call may race).
_BASELINE_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _baseline_lock(snapshot_id: str) -> asyncio.Lock:
    lock = _BASELINE_LOCKS.get(snapshot_id)
    if lock is None:
        lock = asyncio.Lock()
        _BASELINE_LOCKS[snapshot_id] = lock
    return lock


def _read_cache(path: Path) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not all(isinstance(v, dict) for v in data):
        return None
    return data


def _write_cache(path: Path, violations: list[dict[str, Any]]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(violations), encoding="utf-8")
    tmp.replace(path)


def copy_snapshot_files(sdir: Path, project: Project, dest: Path) -> None:
    """The snapshot's files (manifest-checked) + the project's library tables."""
    for src, dst in snapshot_plan(sdir, dest):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    root = project.dir.resolve()
    for name in PROJECT_SUPPORT_FILES:
        source = project.dir / name
        if (
            not (dest / name).exists()
            and source.is_file()
            and source.resolve().is_relative_to(root)
        ):
            shutil.copyfile(source, dest / name)


async def erc_baseline(
    svc: HistoryService, project: Project, snapshot: Snapshot
) -> list[dict[str, Any]]:
    """The snapshot's ERC violations; computed once, cached in `<snapdir>/erc.json`."""
    if project.sch_file is None:
        return []
    store = svc.store(project)
    sdir = store.snapshot_dir(snapshot.id)
    cache = sdir / BASELINE_FILE
    async with _baseline_lock(snapshot.id):
        cached = await asyncio.to_thread(_read_cache, cache)
        if cached is not None:
            return cached
        session = svc.open_session(project)
        if session is None:
            raise KicadUnavailable("the project is not open in KiCad")
        rel_root = project.sch_file.relative_to(project.dir)
        with tempfile.TemporaryDirectory(prefix="cc-erc-base-") as tmp:
            await asyncio.to_thread(copy_snapshot_files, sdir, project, Path(tmp))
            raw = await run_kicad_check_on_dir(
                session.settings, project, Path(tmp) / rel_root, "sch"
            )
        violations = parse_report(raw, "sch")
        await asyncio.to_thread(_write_cache, cache, violations)
        return violations


def erc_is_fresh(state: EditTurnState | None) -> bool:
    """The turn's last ERC report is newer than its last mutation (no double runs)."""
    if state is None or state.last_erc is None or state.last_erc_seq is None:
        return False
    return state.last_mutation_seq is None or state.last_erc_seq > state.last_mutation_seq


async def current_erc(session: KicadSession, state: EditTurnState | None) -> list[dict[str, Any]]:
    """ERC of the design as KiCad holds it (`save_copy` saves a modified schematic
    first); reuses the turn's last report while no mutation followed it."""
    if erc_is_fresh(state):
        assert state is not None and state.last_erc is not None
        return state.last_erc
    violations = await check_violations(session, "sch")
    if state is not None:
        state.last_erc = violations
        state.last_erc_seq = state.call_seq
    return violations


def validate_erc_result_id(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or len(raw) != _ID_RE_LEN
        or any(c not in "0123456789abcdef" for c in raw)
    ):
        raise InvalidErcResultId(f"invalid ERC result id: {raw!r}")
    return raw


class ErcResultRepo:
    """`erc_results` rows (E-C3): one ERC delta of a turn or plan step."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        project_id: str,
        run_id: str,
        plan_step_id: str | None,
        baseline_snapshot_id: str | None,
        delta: ErcDelta,
        justifications: dict[str, str] | None = None,
        rules: list[dict[str, Any]] | None = None,
    ) -> str:
        rid = uuid.uuid4().hex
        await self._db.execute(
            "INSERT INTO erc_results(id, project_id, run_id, plan_step_id,"
            " baseline_snapshot_id, new_errors, new_warnings, resolved, justifications,"
            " rules, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                project_id,
                run_id,
                plan_step_id,
                baseline_snapshot_id,
                json.dumps(delta.new_errors),
                json.dumps(delta.new_warnings),
                json.dumps(delta.resolved),
                json.dumps(justifications or {}),
                json.dumps(rules or []),
                utcnow_iso(),
            ),
        )
        return rid

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "plan_step_id": row["plan_step_id"],
            "baseline_snapshot_id": row["baseline_snapshot_id"],
            "new_errors": json.loads(row["new_errors"]),
            "new_warnings": json.loads(row["new_warnings"]),
            "resolved": json.loads(row["resolved"]),
            "justifications": json.loads(row["justifications"]),
            "rules": json.loads(row["rules"]),
            "created_at": str(row["created_at"]),
        }

    async def step_justifications(
        self, plan_step_id: str
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """(id -> reason, pattern rules) stored for a plan step (`put_step_justifications`)."""
        row = await self._db.fetchone(
            "SELECT justifications, rules FROM erc_step_justifications WHERE plan_step_id = ?",
            (plan_step_id,),
        )
        if row is None:
            return {}, []
        return json.loads(row["justifications"]), json.loads(row["rules"])

    async def put_step_justifications(
        self, plan_step_id: str, justifications: dict[str, str], rules: list[dict[str, Any]]
    ) -> None:
        await self._db.execute(
            "INSERT INTO erc_step_justifications(plan_step_id, justifications, rules, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(plan_step_id) DO UPDATE SET"
            " justifications = excluded.justifications, rules = excluded.rules,"
            " updated_at = excluded.updated_at",
            (plan_step_id, json.dumps(justifications), json.dumps(rules), utcnow_iso()),
        )

    async def forget_steps(self, plan_step_ids: list[str]) -> None:
        """Drop the stored justifications of steps whose attempt is over."""
        if not plan_step_ids:
            return
        marks = ", ".join("?" for _ in plan_step_ids)
        await self._db.execute(
            f"DELETE FROM erc_step_justifications WHERE plan_step_id IN ({marks})",
            tuple(plan_step_ids),
        )

    async def get(self, project_id: str, result_id: str) -> dict[str, Any]:
        rid = validate_erc_result_id(result_id)
        row = await self._db.fetchone(
            "SELECT * FROM erc_results WHERE id = ? AND project_id = ?", (rid, project_id)
        )
        if row is None:
            raise ErcResultNotFound(f"ERC result {rid} not found")
        return self._row(row)

    async def latest_for_runs(
        self, project_id: str, run_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """run id -> its newest ERC result (turn or step)."""
        if not run_ids:
            return {}
        marks = ", ".join("?" for _ in run_ids)
        rows = await self._db.fetchall(
            f"SELECT * FROM erc_results WHERE project_id = ? AND run_id IN ({marks})"
            " ORDER BY created_at, rowid",
            (project_id, *run_ids),
        )
        return {str(r["run_id"]): self._row(r) for r in rows}
