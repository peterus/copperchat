"""Scoped library restore plan (F3 Task 5, spec §10): only this project's own journalled
library writes since the snapshot are undone, never another project's."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from copperchat.db.repos.footprints import ABSENT, FootprintWrite
from copperchat.history.library_restore import (
    LibraryRestorePlan,
    apply_library_plan,
    plan_library_restore,
    validate_copy_target,
    validate_delete,
    verify_blob,
)
from copperchat.history.store import CorruptSnapshot, LibraryLocation, sha256_file
from tests.support.history import ECC83

# KiCad-saved footprints and table, copied (never hand-written) into the test repo.
_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))
_TABLE = ECC83 / "fp-lib-table"
MAX_ID = 10
LIB_REL = "footprints/X.pretty"


def fresh(path: Path) -> str:
    return sha256_file(path) if path.is_file() else ABSENT


def no_foreign(_file_rel: str, _after: int) -> bool:
    return False


class Repo:
    def __init__(self, tmp_path: Path, project_rel: str = "hardware/main") -> None:
        self.root = (tmp_path / "repo").resolve()
        self.project = self.root / project_rel if project_rel else self.root
        self.project.mkdir(parents=True, exist_ok=True)
        self.lib = self.root / "footprints" / "X.pretty"
        self.project_rel = project_rel or "."
        self.blobs = tmp_path / "blobs"
        self.blobs.mkdir()

    @property
    def location(self) -> LibraryLocation:
        return LibraryLocation(
            dir=self.lib,
            repo_root=self.root,
            table=self.project / "fp-lib-table",
            nickname="X",
        )

    def make_lib(self) -> None:
        self.lib.mkdir(parents=True, exist_ok=True)

    def put(self, name: str, src: int) -> str:
        """Copy fixture footprint `src` to the library as `name`; returns its sha."""
        self.make_lib()
        shutil.copyfile(_FOOTPRINTS[src], self.lib / name)
        return sha256_file(self.lib / name)

    def blob(self, name: str) -> str:
        """Store the current bytes of library file `name` as a blob."""
        sha = sha256_file(self.lib / name)
        shutil.copyfile(self.lib / name, self.blobs / sha)
        return sha

    def blob_path(self, sha: str) -> Path:
        return self.blobs / sha

    def lib_file_rel(self, name: str) -> str:
        return f"{LIB_REL}/{name}"

    def table_rel(self) -> str:
        return "fp-lib-table" if self.project_rel == "." else f"{self.project_rel}/fp-lib-table"

    def manifest(
        self,
        files: dict[str, str],
        *,
        existed: bool = True,
        table_existed: bool = True,
        repo_root_rel: str | None = None,
        lib_dir: str = LIB_REL,
        max_id: int = MAX_ID,
    ) -> dict[str, Any]:
        prefix = os.path.relpath(self.root, self.project)
        entries = [
            {
                "relPath": Path(prefix, LIB_REL, name).as_posix()
                if prefix != "."
                else f"{LIB_REL}/{name}",
                "sha256": sha,
                "size": 1,
            }
            for name, sha in files.items()
        ]
        return {
            "version": 2,
            "id": "0" * 32,
            "repoRootRel": self.project_rel if repo_root_rel is None else repo_root_rel,
            "journalMaxId": max_id,
            "files": [],
            "library": {"dir": lib_dir, "existed": existed, "files": entries},
            "table": {"existed": table_existed},
        }

    def plan(
        self,
        manifest: dict[str, Any],
        journal: list[FootprintWrite],
        *,
        foreign_later: Callable[[str, int], bool] = no_foreign,
        current: Callable[[Path], str] = fresh,
        table_rows: list[tuple[str, str]] | None = None,
    ) -> LibraryRestorePlan:
        return plan_library_restore(
            manifest,
            self.location,
            self.project,
            journal,
            foreign_later,
            current,
            table_rows,
        )


def row(
    id: int,
    file_rel: str,
    op: str,
    state: str = "done",
    *,
    post: str | None = None,
    pre: str | None = None,
    post_s2: str | None = None,
    lib_id: str | None = None,
    project_id: str = "p1",
    created_at: str = "2026-11-18T10:00:00.000Z",
) -> FootprintWrite:
    return FootprintWrite(
        id=id,
        project_id=project_id,
        run_id="r1",
        lib_id=lib_id or f"X:{Path(file_rel).stem}",
        file_rel=file_rel,
        op=op,
        state=state,
        pre_sha256=pre,
        post_s2_sha256=post_s2,
        post_sha256=post,
        geometry_hash=None,
        created_at=created_at,
        finalised_at=None,
    )


def is_empty(plan: LibraryRestorePlan) -> bool:
    return (
        not plan.copy
        and not plan.delete
        and plan.rmdir is None
        and not plan.remove_rows
        and not plan.remove_table
    )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


# --- manifest versions ---------------------------------------------------------------


def test_v1_restore_deletes_nothing(repo: Repo) -> None:
    shutil.copyfile(_TABLE, repo.project / "fp-lib-table")
    sha_a = repo.put("A.kicad_mod", 0)
    repo.put("C.kicad_mod", 2)
    v1 = {"id": "0" * 32, "files": [{"relPath": "main.kicad_sch", "sha256": "0" * 64}]}
    journal = [
        row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=fresh(repo.lib / "C.kicad_mod")),
        row(12, repo.table_rel(), "row_add", lib_id="X"),
    ]
    for manifest in (v1, {**v1, "version": 1}):
        plan = repo.plan(manifest, journal, table_rows=[("X", "${KIPRJMOD}/x")])
        assert is_empty(plan)
        assert plan.refused is None
    assert (repo.project / "fp-lib-table").is_file()
    assert fresh(repo.lib / "A.kicad_mod") == sha_a


def test_library_null_is_empty_plan(repo: Repo) -> None:
    repo.put("C.kicad_mod", 2)
    manifest = repo.manifest({})
    manifest["library"] = None
    journal = [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=ABSENT)]
    plan = repo.plan(manifest, journal)
    assert is_empty(plan)
    assert plan.refused is None


# --- per-file decisions ----------------------------------------------------------------


def test_deletes_own_created_after_snapshot(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    sha_c = repo.put("C.kicad_mod", 2)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c)])
    assert plan.delete == [repo.lib / "C.kicad_mod"]
    assert plan.copy == []
    assert plan.skipped == []


def test_copies_back_own_changed_and_deleted(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    sha_b = repo.put("B.kicad_mod", 1)
    manifest = repo.manifest({"A.kicad_mod": sha_a, "B.kicad_mod": sha_b})
    new_a = repo.put("A.kicad_mod", 3)  # replaced here
    (repo.lib / "B.kicad_mod").unlink()  # deleted here
    journal = [
        row(11, repo.lib_file_rel("A.kicad_mod"), "replace", post=new_a, pre=sha_a),
        row(12, repo.lib_file_rel("B.kicad_mod"), "delete", post=ABSENT, pre=sha_b),
    ]
    plan = repo.plan(manifest, journal)
    assert plan.copy == [(sha_a, repo.lib / "A.kicad_mod"), (sha_b, repo.lib / "B.kicad_mod")]
    assert plan.delete == []
    assert plan.skipped == []


def test_skips_file_changed_elsewhere(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    own = repo.put("A.kicad_mod", 3)
    repo.put("A.kicad_mod", 4)  # the KiCad GUI changed it afterwards
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("A.kicad_mod"), "replace", post=own)])
    assert plan.copy == []
    assert len(plan.skipped) == 1
    assert "X:A" in plan.skipped[0]
    assert "changed outside this project, not restored" in plan.skipped[0]


def test_skips_when_foreign_row_later(repo: Repo) -> None:
    sha_c = repo.put("C.kicad_mod", 2)
    manifest = repo.manifest({}, existed=True)
    file_rel = repo.lib_file_rel("C.kicad_mod")
    calls: list[tuple[str, int]] = []

    def foreign(f: str, after: int) -> bool:
        calls.append((f, after))
        return True  # another project approved it after this create

    plan = repo.plan(manifest, [row(11, file_rel, "create", post=sha_c)], foreign_later=foreign)
    assert calls == [(file_rel, MAX_ID)]
    assert plan.delete == []
    assert plan.skipped == ["X:C: changed outside this project, not restored"]


def test_skips_when_foreign_row_between_snapshot_and_own_row(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a}, max_id=9)
    own = repo.put("A.kicad_mod", 3)
    file_rel = repo.lib_file_rel("A.kicad_mod")
    foreign_rows = {10: file_rel}  # another project's row 10, before our own row 11

    def foreign(f: str, after: int) -> bool:
        return any(i > after and fr == f for i, fr in foreign_rows.items())

    plan = repo.plan(manifest, [row(11, file_rel, "replace", post=own)], foreign_later=foreign)
    assert plan.copy == []
    assert plan.skipped == ["X:A: changed outside this project, not restored"]


def test_pending_accepts_any_state(repo: Repo) -> None:
    repo.make_lib()
    manifest = repo.manifest({})
    post_s2 = repo.put("C.kicad_mod", 2)
    repo.put("C.kicad_mod", 3)  # S3 completed before the server died
    journal = [
        row(11, repo.lib_file_rel("C.kicad_mod"), "create", "pending", pre=ABSENT, post_s2=post_s2)
    ]
    plan = repo.plan(manifest, journal)
    assert plan.delete == [repo.lib / "C.kicad_mod"]
    assert plan.skipped == []


def test_pending_replace_is_restored_from_blob(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    repo.blob("A.kicad_mod")
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    repo.put("A.kicad_mod", 3)
    journal = [row(11, repo.lib_file_rel("A.kicad_mod"), "replace", "pending", pre=sha_a)]
    plan = repo.plan(manifest, journal)
    assert plan.copy == [(sha_a, repo.lib / "A.kicad_mod")]
    apply_library_plan(plan, repo.blob_path, location=repo.location, project_dir=repo.project)
    assert fresh(repo.lib / "A.kicad_mod") == sha_a


def test_leftover_restored_unless_changed_by_other_project(repo: Repo) -> None:
    repo.make_lib()
    manifest = repo.manifest({})
    sha_c = repo.put("C.kicad_mod", 2)
    journal = [row(11, repo.lib_file_rel("C.kicad_mod"), "create", "leftover", post=sha_c)]
    assert repo.plan(manifest, journal).delete == [repo.lib / "C.kicad_mod"]

    plan = repo.plan(manifest, journal, foreign_later=lambda f, a: True)
    assert plan.delete == []
    assert plan.skipped == ["X:C: changed outside this project, not restored"]

    repo.put("C.kicad_mod", 4)  # changed since finalisation (no journal row)
    plan = repo.plan(manifest, journal)
    assert plan.delete == []
    assert "changed outside this project" in plan.skipped[0]


def test_compensated_create_already_gone_is_noop(repo: Repo) -> None:
    repo.make_lib()
    manifest = repo.manifest({})
    journal = [row(11, repo.lib_file_rel("C.kicad_mod"), "create", "compensated", post=ABSENT)]
    plan = repo.plan(manifest, journal)
    assert is_empty(plan)
    assert plan.skipped == []


def test_latest_own_row_decides(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    first = repo.put("A.kicad_mod", 3)
    second = repo.put("A.kicad_mod", 4)
    file_rel = repo.lib_file_rel("A.kicad_mod")
    journal = [
        row(12, file_rel, "approve", post=second),
        row(11, file_rel, "replace", post=first),
    ]
    assert repo.plan(manifest, journal).copy == [(sha_a, repo.lib / "A.kicad_mod")]


def test_row_without_value_skipped_and_reported(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    repo.put("A.kicad_mod", 3)
    repo.put("C.kicad_mod", 2)
    journal = [
        row(11, repo.lib_file_rel("A.kicad_mod"), "replace", "done", post=None),
        row(12, repo.lib_file_rel("C.kicad_mod"), "create", "pending"),
    ]
    plan = repo.plan(manifest, journal)
    assert plan.copy == [] and plan.delete == []
    assert plan.skipped == [
        "X:A: no recorded file state, not restored",
        "X:C: no recorded file state, not restored",
    ]


def test_never_touches_untouched_files(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    sha_b = repo.put("B.kicad_mod", 1)
    manifest = repo.manifest({"A.kicad_mod": sha_a, "B.kicad_mod": sha_b})
    repo.put("B.kicad_mod", 3)  # changed by someone, no journal row
    repo.put("D.kicad_mod", 4)  # created by someone, no journal row
    sha_c = repo.put("C.kicad_mod", 2)
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c)])
    assert plan.delete == [repo.lib / "C.kicad_mod"]
    assert plan.copy == []


def test_journal_boundary_by_id(repo: Repo) -> None:
    repo.make_lib()
    manifest = repo.manifest({})
    sha_c = repo.put("C.kicad_mod", 2)
    sha_d = repo.put("D.kicad_mod", 3)
    same_ms = "2026-11-18T10:00:00.000Z"  # the snapshot's own timestamp
    journal = [
        row(MAX_ID, repo.lib_file_rel("D.kicad_mod"), "create", post=sha_d, created_at=same_ms),
        row(MAX_ID + 1, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c, created_at=same_ms),
    ]
    plan = repo.plan(manifest, journal)
    assert plan.delete == [repo.lib / "C.kicad_mod"]


def test_current_is_fresh_hash(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    own = repo.put("A.kicad_mod", 3)
    stale_cache = {repo.lib / "A.kicad_mod": own}  # stat-reused hash recorded earlier
    repo.put("A.kicad_mod", 4)  # changed afterwards, same size could fool a stat check
    seen: list[Path] = []

    def current(path: Path) -> str:
        seen.append(path)
        return fresh(path)

    journal = [row(11, repo.lib_file_rel("A.kicad_mod"), "replace", post=own)]
    plan = repo.plan(manifest, journal, current=current)
    assert seen == [repo.lib / "A.kicad_mod"]
    assert plan.copy == []  # the stale cached hash would have matched
    assert stale_cache[repo.lib / "A.kicad_mod"] == own
    assert "changed outside this project" in plan.skipped[0]


# --- refusal ---------------------------------------------------------------------------


@pytest.mark.parametrize("lib_dir", ["footprints/Y.pretty", "../evil/X.pretty", "footprints"])
def test_forged_library_dir_refused(repo: Repo, lib_dir: str) -> None:
    sha_c = repo.put("C.kicad_mod", 2)
    manifest = repo.manifest({}, lib_dir=lib_dir)
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c)])
    assert plan.refused is not None and "library" in plan.refused
    assert is_empty(plan)


def test_moved_repo_refused(repo: Repo) -> None:
    sha_c = repo.put("C.kicad_mod", 2)
    manifest = repo.manifest({}, repo_root_rel="old/main")
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c)])
    assert plan.refused is not None and "moved" in plan.refused
    assert is_empty(plan)


def test_forged_library_file_entry_corrupt(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    manifest["library"]["files"][0]["relPath"] = "../../hardware/main/main.kicad_sch"
    with pytest.raises(CorruptSnapshot):
        repo.plan(manifest, [])
    manifest = repo.manifest({"A.kicad_mod": "../../x"})
    with pytest.raises(CorruptSnapshot):
        repo.plan(manifest, [])


# --- validation ------------------------------------------------------------------------


def test_symlink_component_never_deleted(repo: Repo, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copyfile(_FOOTPRINTS[2], elsewhere / "C.kicad_mod")
    (repo.root / "footprints").mkdir()
    repo.lib.symlink_to(elsewhere, target_is_directory=True)
    manifest = repo.manifest({})
    sha_c = fresh(elsewhere / "C.kicad_mod")
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c)])
    assert plan.delete == []
    assert plan.rmdir is None
    assert len(plan.skipped) == 1 and "X:C" in plan.skipped[0]
    with pytest.raises(CorruptSnapshot):
        validate_delete(repo.lib / "C.kicad_mod", repo.location, repo.project, table=False)
    assert (elsewhere / "C.kicad_mod").is_file()


def test_symlink_file_never_deleted(repo: Repo, tmp_path: Path) -> None:
    repo.make_lib()
    target = tmp_path / "C.kicad_mod"
    shutil.copyfile(_FOOTPRINTS[2], target)
    (repo.lib / "C.kicad_mod").symlink_to(target)
    plan = repo.plan(
        repo.manifest({}),
        [row(11, repo.lib_file_rel("C.kicad_mod"), "create", post=fresh(target))],
    )
    assert plan.delete == []
    assert len(plan.skipped) == 1


def test_only_kicad_mod_in_lib_dir_or_own_table(repo: Repo) -> None:
    loc, proj = repo.location, repo.project
    repo.put("A.kicad_mod", 0)
    shutil.copyfile(_FOOTPRINTS[1], repo.lib / "notes.txt")
    (repo.lib / "sub").mkdir()
    shutil.copyfile(_FOOTPRINTS[1], repo.lib / "sub" / "B.kicad_mod")
    shutil.copyfile(_FOOTPRINTS[1], repo.root / "footprints" / "B.kicad_mod")
    shutil.copyfile(_TABLE, proj / "fp-lib-table")
    other = repo.root / "hardware" / "io"
    other.mkdir(parents=True)
    shutil.copyfile(_TABLE, other / "fp-lib-table")

    validate_delete(repo.lib / "A.kicad_mod", loc, proj, table=False)
    validate_delete(proj / "fp-lib-table", loc, proj, table=True)
    for bad, table in [
        (repo.lib / "notes.txt", False),
        (repo.lib / "sub" / "B.kicad_mod", False),
        (repo.root / "footprints" / "B.kicad_mod", False),
        (repo.lib / "missing.kicad_mod", False),
        (proj / "fp-lib-table", False),
        (other / "fp-lib-table", True),
        (repo.lib / "A.kicad_mod", True),
        (repo.lib / ".." / "X.pretty" / "A.kicad_mod", False),
    ]:
        with pytest.raises(CorruptSnapshot):
            validate_delete(bad, loc, proj, table=table)


def test_copy_target_symlink_component_refused(repo: Repo, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo.root / "footprints").mkdir()
    repo.lib.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(CorruptSnapshot):
        validate_copy_target(repo.lib / "A.kicad_mod", repo.location)
    repo.lib.unlink()
    repo.make_lib()
    (repo.lib / "A.kicad_mod").symlink_to(elsewhere / "A.kicad_mod")
    with pytest.raises(CorruptSnapshot):
        validate_copy_target(repo.lib / "A.kicad_mod", repo.location)


def test_copy_target_outside_lib_dir_refused(repo: Repo) -> None:
    loc = repo.location
    (repo.root / "footprints").mkdir()
    validate_copy_target(repo.lib / "A.kicad_mod", loc)  # lib dir may be created
    repo.make_lib()
    (repo.lib / "sub").mkdir()
    for bad in [
        repo.lib / "A.txt",
        repo.lib / "sub" / "A.kicad_mod",
        repo.root / "footprints" / "A.kicad_mod",
        repo.project / "fp-lib-table",
        repo.root.parent / "A.kicad_mod",
        repo.lib / "sub",
    ]:
        with pytest.raises(CorruptSnapshot):
            validate_copy_target(bad, loc)


def test_copy_target_lib_dir_only_created_below_real_footprints(repo: Repo) -> None:
    with pytest.raises(CorruptSnapshot):  # footprints/ missing: no recursive creation
        validate_copy_target(repo.lib / "A.kicad_mod", repo.location)


def test_tampered_blob_refused(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    repo.blob("A.kicad_mod")
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    own = repo.put("A.kicad_mod", 3)
    shutil.copyfile(_FOOTPRINTS[4], repo.blob_path(sha_a))  # tampered blob content
    plan = repo.plan(manifest, [row(11, repo.lib_file_rel("A.kicad_mod"), "replace", post=own)])
    assert plan.copy == [(sha_a, repo.lib / "A.kicad_mod")]
    with pytest.raises(CorruptSnapshot):
        apply_library_plan(plan, repo.blob_path, location=repo.location, project_dir=repo.project)
    assert fresh(repo.lib / "A.kicad_mod") == own
    assert sorted(p.name for p in repo.lib.iterdir()) == ["A.kicad_mod"]  # no temp left
    with pytest.raises(CorruptSnapshot):
        verify_blob(repo.blob_path(sha_a), "../../x")


# --- root project ----------------------------------------------------------------------


def test_root_project_restore_uses_library_files(tmp_path: Path) -> None:
    repo = Repo(tmp_path, project_rel="")
    sha_a = repo.put("A.kicad_mod", 0)
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    assert manifest["library"]["files"][0]["relPath"] == "footprints/X.pretty/A.kicad_mod"
    assert manifest["repoRootRel"] == "."
    own = repo.put("A.kicad_mod", 3)
    sha_c = repo.put("C.kicad_mod", 2)
    journal = [
        row(11, repo.lib_file_rel("A.kicad_mod"), "replace", post=own),
        row(12, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c),
    ]
    plan = repo.plan(manifest, journal)
    assert plan.refused is None
    assert plan.copy == [(sha_a, repo.lib / "A.kicad_mod")]
    assert plan.delete == [repo.lib / "C.kicad_mod"]
    plan = repo.plan({**manifest, "repoRootRel": ""}, journal)
    assert plan.refused is None


# --- table rows and the library dir -----------------------------------------------------


def test_row_add_never_deletes_table_file_directly(repo: Repo) -> None:
    shutil.copyfile(_TABLE, repo.project / "fp-lib-table")
    repo.make_lib()
    manifest = repo.manifest({}, table_existed=False)
    journal = [row(11, repo.table_rel(), "row_add", lib_id="X", post=fresh(repo.location.table))]
    plan = repo.plan(manifest, journal, table_rows=None)
    assert plan.delete == [] and plan.copy == []
    assert plan.remove_table is False
    assert plan.remove_rows == []
    assert plan.skipped == ["library row X not removed (project not open)"]


def test_remove_table_only_if_all_rows_own(repo: Repo) -> None:
    shutil.copyfile(_TABLE, repo.project / "fp-lib-table")
    repo.make_lib()
    manifest = repo.manifest({}, table_existed=False)
    journal = [row(11, repo.table_rel(), "row_add", lib_id="X")]
    plan = repo.plan(manifest, journal, table_rows=[("X", "${KIPRJMOD}/../../footprints/X.pretty")])
    assert plan.remove_table is True
    assert plan.remove_rows == []
    assert plan.delete == []

    rows = [("X", "${KIPRJMOD}/x"), ("Mine", "${KIPRJMOD}/m")]
    plan = repo.plan(manifest, journal, table_rows=rows)
    assert plan.remove_table is False
    assert plan.remove_rows == ["X"]

    plan = repo.plan(repo.manifest({}, table_existed=True), journal, table_rows=[("X", "u")])
    assert plan.remove_table is False
    assert plan.remove_rows == ["X"]

    (repo.project / "fp-lib-table").unlink()  # nothing to remove as a file
    plan = repo.plan(manifest, journal, table_rows=[("X", "u")])
    assert plan.remove_table is False


def test_otherwise_remove_rows(repo: Repo) -> None:
    shutil.copyfile(_TABLE, repo.project / "fp-lib-table")
    repo.make_lib()
    manifest = repo.manifest({}, table_existed=True)
    journal = [
        row(11, repo.table_rel(), "row_add", lib_id="X"),
        row(12, repo.table_rel(), "row_add", lib_id="Gone"),
        row(MAX_ID, repo.table_rel(), "row_add", lib_id="Old"),
    ]
    rows = [("Old", "u"), ("X", "u"), ("Theirs", "u")]
    plan = repo.plan(manifest, journal, table_rows=rows)
    assert plan.remove_rows == ["X"]
    assert plan.remove_table is False
    assert plan.skipped == []


def test_rmdir_only_if_created_after_snapshot_by_this_project(repo: Repo) -> None:
    shutil.copyfile(_TABLE, repo.project / "fp-lib-table")
    sha_c = repo.put("C.kicad_mod", 2)
    created = repo.manifest({}, existed=False)
    journal = [
        row(11, repo.table_rel(), "row_add", lib_id="X"),
        row(12, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c),
    ]
    rows = [("X", "u"), ("Other", "u")]
    plan = repo.plan(created, journal, table_rows=rows)
    assert plan.delete == [repo.lib / "C.kicad_mod"]
    assert plan.rmdir == repo.lib

    # the dir existed at snapshot time
    assert repo.plan(repo.manifest({}, existed=True), journal, table_rows=rows).rmdir is None
    # this project did not create it (no own row_add since the snapshot)
    assert repo.plan(created, journal[1:], table_rows=rows).rmdir is None
    # it would not be empty after the deletes (someone else's footprint)
    repo.put("D.kicad_mod", 3)
    assert repo.plan(created, journal, table_rows=rows).rmdir is None
    (repo.lib / "D.kicad_mod").unlink()
    # a skipped delete leaves it non-empty
    plan = repo.plan(created, journal, table_rows=rows, foreign_later=lambda f, a: True)
    assert plan.rmdir is None


# --- apply ------------------------------------------------------------------------------


def test_apply_copies_then_deletes_then_rmdir(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    repo.blob("A.kicad_mod")
    manifest = repo.manifest({"A.kicad_mod": sha_a})
    own = repo.put("A.kicad_mod", 3)
    sha_c = repo.put("C.kicad_mod", 2)
    journal = [
        row(11, repo.lib_file_rel("A.kicad_mod"), "replace", post=own),
        row(12, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c),
    ]
    plan = repo.plan(manifest, journal)
    notes = apply_library_plan(
        plan, repo.blob_path, location=repo.location, project_dir=repo.project
    )
    assert notes == []
    assert fresh(repo.lib / "A.kicad_mod") == sha_a
    assert not (repo.lib / "C.kicad_mod").exists()
    assert sorted(p.name for p in repo.lib.iterdir()) == ["A.kicad_mod"]


def test_apply_removes_created_dir_and_table(repo: Repo) -> None:
    shutil.copyfile(_TABLE, repo.project / "fp-lib-table")
    sha_c = repo.put("C.kicad_mod", 2)
    manifest = repo.manifest({}, existed=False, table_existed=False)
    journal = [
        row(11, repo.table_rel(), "row_add", lib_id="X"),
        row(12, repo.lib_file_rel("C.kicad_mod"), "create", post=sha_c),
    ]
    plan = repo.plan(manifest, journal, table_rows=[("X", "u")])
    assert plan.remove_table and plan.rmdir == repo.lib
    apply_library_plan(plan, repo.blob_path, location=repo.location, project_dir=repo.project)
    assert not repo.lib.exists()
    assert (repo.root / "footprints").is_dir()
    assert not (repo.project / "fp-lib-table").exists()


def test_apply_copy_creates_lib_dir_non_recursively(repo: Repo) -> None:
    sha_a = repo.put("A.kicad_mod", 0)
    repo.blob("A.kicad_mod")
    shutil.rmtree(repo.lib)
    plan = LibraryRestorePlan(copy=[(sha_a, repo.lib / "A.kicad_mod")])
    apply_library_plan(plan, repo.blob_path, location=repo.location, project_dir=repo.project)
    assert fresh(repo.lib / "A.kicad_mod") == sha_a
    assert sorted(p.name for p in repo.lib.iterdir()) == ["A.kicad_mod"]


def test_apply_hashes_each_blob_once(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify_blob only checks the file type; the copy's hash is the content check."""
    sha_a = repo.put("A.kicad_mod", 0)
    repo.blob("A.kicad_mod")
    repo.put("A.kicad_mod", 3)
    real_open = Path.open
    reads: list[Path] = []

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == repo.blob_path(sha_a):
            reads.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    plan = LibraryRestorePlan(copy=[(sha_a, repo.lib / "A.kicad_mod")])
    apply_library_plan(plan, repo.blob_path, location=repo.location, project_dir=repo.project)
    monkeypatch.undo()
    assert reads == [repo.blob_path(sha_a)]
    assert fresh(repo.lib / "A.kicad_mod") == sha_a


def test_failed_replace_removes_remaining_temps(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.history import library_restore as lr

    names = ["A.kicad_mod", "B.kicad_mod", "C.kicad_mod"]
    shas = [repo.put(n, i) for i, n in enumerate(names)]
    for n in names:
        repo.blob(n)
        repo.put(n, 4)  # changed since the snapshot
    plan = LibraryRestorePlan(
        copy=[(sha, repo.lib / n) for sha, n in zip(shas, names, strict=True)]
    )
    real = os.replace
    calls: list[str] = []

    def failing(src: Path, dst: Path) -> None:
        calls.append(Path(dst).name)
        if len(calls) == 2:
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(lr.os, "replace", failing)
    with pytest.raises(OSError, match="disk full"):
        apply_library_plan(plan, repo.blob_path, location=repo.location, project_dir=repo.project)
    assert sorted(p.name for p in repo.lib.iterdir()) == names  # no `.restore-*` temp left
    assert fresh(repo.lib / "A.kicad_mod") == shas[0]
