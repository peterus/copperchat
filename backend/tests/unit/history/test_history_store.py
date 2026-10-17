import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from copperchat.db import Database
from copperchat.history.store import (
    GITIGNORE_LINES,
    HISTORY_KEEP,
    DocumentHashRepo,
    HistoryStore,
    InvalidSnapshotId,
    SnapshotNotFound,
    compute_hashes,
    document_files,
    ensure_gitignore,
    validate_snapshot_id,
)
from copperchat.projects import agent_dir
from tests.support.history import ECC83, HIERARCHY, copy_project


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


async def test_snapshot_copies_documents_byte_for_byte(db: Database, tmp_path: Path) -> None:
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    snap = await store.snapshot("run-1", "add decoupling", preview_png=b"\x89PNG fake")
    sdir = store.snapshot_dir(snap.id)
    assert sdir.parent == (agent_dir(project) / "history").resolve()
    for src in document_files(project):
        rel = src.relative_to(project.dir)
        assert (sdir / rel).read_bytes() == src.read_bytes()
    manifest = json.loads((sdir / "manifest.json").read_text())
    assert manifest["id"] == snap.id and manifest["runId"] == "run-1"
    assert {f["relPath"] for f in manifest["files"]} == set(snap.files)
    assert {"ecc83-pp.kicad_pro", "ecc83-pp.kicad_sch", "ecc83-pp.kicad_pcb"} == set(snap.files)
    assert (sdir / "preview.png").exists() and snap.has_preview
    assert [s.id for s in await store.list()] == [snap.id]
    latest = await store.latest_for_run("run-1")
    assert latest is not None and latest.id == snap.id
    assert await store.latest_for_run("other") is None
    assert not list((agent_dir(project) / "history").glob(".tmp-*"))


async def test_sub_sheets_are_included_and_caches_are_not(db: Database, tmp_path: Path) -> None:
    project = copy_project(HIERARCHY, tmp_path / "projects")
    view = agent_dir(project) / "view"
    view.mkdir(parents=True)
    (view / "ampli_ht.kicad_sch").write_bytes((project.dir / "ampli_ht.kicad_sch").read_bytes())
    files = [p.relative_to(project.dir).as_posix() for p in document_files(project)]
    assert sorted(files) == [
        "ampli_ht.kicad_sch",
        "complex_hierarchy.kicad_pcb",
        "complex_hierarchy.kicad_pro",
        "complex_hierarchy.kicad_sch",
    ]
    snap = await HistoryStore(db, project).snapshot("r", "x")
    assert sorted(snap.files) == sorted(files)


async def test_retention_keeps_last_50(db: Database, tmp_path: Path) -> None:
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    ids = [(await store.snapshot(f"run-{i}", str(i))).id for i in range(HISTORY_KEEP + 3)]
    listed = [s.id for s in await store.list()]
    assert len(listed) == HISTORY_KEEP
    assert listed[0] == ids[-1] and listed[-1] == ids[3]
    for gone in ids[:3]:
        assert not (agent_dir(project) / "history" / gone).exists()
    assert len(list((agent_dir(project) / "history").iterdir())) == HISTORY_KEEP


@pytest.mark.parametrize(
    "bad",
    [
        "../etc",
        "..",
        "/etc/passwd",
        "a" * 31,
        "A" * 32,
        "0" * 31 + "/",
        "",
        "x" * 32,
        "0" * 32 + "\n",
        "0" * 33,
        "../" + "0" * 32,
    ],
)
def test_invalid_snapshot_ids_rejected(bad: str) -> None:
    with pytest.raises(InvalidSnapshotId):
        validate_snapshot_id(bad)


async def test_unknown_and_traversal_ids_rejected_by_store(db: Database, tmp_path: Path) -> None:
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    with pytest.raises(SnapshotNotFound):
        await store.get("0" * 32)
    for bad in ("../../x", str(tmp_path)):
        with pytest.raises(InvalidSnapshotId):
            await store.get(bad)
        with pytest.raises(InvalidSnapshotId):
            store.snapshot_dir(bad)


async def test_snapshot_of_other_project_is_not_found(db: Database, tmp_path: Path) -> None:
    a = copy_project(ECC83, tmp_path / "projects")
    b = copy_project(HIERARCHY, tmp_path / "projects")
    snap = await HistoryStore(db, a).snapshot("r", "x")
    with pytest.raises(SnapshotNotFound):
        await HistoryStore(db, b).get(snap.id)


async def test_gitignore_and_hash_repo(db: Database, tmp_path: Path) -> None:
    project = copy_project(ECC83, tmp_path / "projects")
    ensure_gitignore(project)
    text = (agent_dir(project) / ".gitignore").read_text()
    assert text.splitlines() == list(GITIGNORE_LINES)
    assert {"history/", "view/", "renders/", "uploads/", "images/"} == set(GITIGNORE_LINES)
    hashes = compute_hashes(project)
    assert len(hashes) == 3 and all(len(h) == 64 for h in hashes.values())
    repo = DocumentHashRepo(db)
    assert await repo.get(project.id) == {}
    await repo.replace(project.id, hashes)
    assert await repo.get(project.id) == hashes
    await repo.replace(project.id, {"a": "b"})
    assert await repo.get(project.id) == {"a": "b"}


async def test_orphaned_snapshot_dirs_are_swept(db: Database, tmp_path: Path) -> None:
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    store.root.mkdir(parents=True)
    (store.root / ("f" * 32)).mkdir()  # no DB row: killed between rename and INSERT
    (store.root / ".tmp-abc").mkdir()
    snap = await store.snapshot("r", "x")
    assert sorted(p.name for p in store.root.iterdir()) == [snap.id]
