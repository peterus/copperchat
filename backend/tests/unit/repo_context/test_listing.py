import os
from pathlib import Path, PurePosixPath

import pytest

from copperchat.repo_context import listing
from copperchat.repo_context.listing import Listing, list_repo
from copperchat.repo_context.paths import RepoPolicy

from .conftest import git, init_repo


def git_policy(root: Path) -> RepoPolicy:
    return RepoPolicy(root.resolve(), root.resolve(), True, root.parent.resolve())


def walk_policy(root: Path) -> RepoPolicy:
    return RepoPolicy(root.resolve(), root.resolve(), False, root.parent.resolve())


def paths(result: Listing) -> list[str]:
    return [f.rel.as_posix() for f in result.files]


def write(path: Path, data: str | bytes = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


async def test_git_listing_respects_gitignore(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    write(root / ".gitignore", "build/\n")
    write(root / "README.md", "hello\n")
    write(root / "build" / "out.txt")
    git(root, "add", ".gitignore", "README.md")
    git(root, "commit", "-qm", "init")
    write(root / "new.md")
    result = await list_repo(git_policy(root))
    assert paths(result) == [".gitignore", "README.md", "new.md"]
    assert not result.truncated
    readme = next(f for f in result.files if f.rel == PurePosixPath("README.md"))
    assert readme.size == len("hello\n")


async def test_tracked_env_hidden(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    write(root / ".env", "TOKEN=1\n")
    write(root / "config" / "secrets.yaml", "k: v\n")
    write(root / "config" / "app.yaml", "a: b\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    assert paths(await list_repo(git_policy(root))) == ["config/app.yaml"]


async def test_deleted_tracked_dropped(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    write(root / "gone.txt")
    write(root / "kept.txt")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    (root / "gone.txt").unlink()
    assert paths(await list_repo(git_policy(root))) == ["kept.txt"]


async def test_git_listing_skips_dir_symlinks_and_outside_links(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    write(outside / "x.txt")
    write(root / "docs" / "a.md")
    os.symlink(outside, root / "outdir")
    os.symlink(outside / "x.txt", root / "out.txt")
    os.symlink("docs", root / "docsdir")
    os.symlink("docs/a.md", root / "alias.md")
    write(root / ".env", "T=1\n")
    os.symlink(".env", root / "envlink.txt")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    assert paths(await list_repo(git_policy(root))) == ["alias.md", "docs/a.md"]


@pytest.mark.parametrize("target", ["git", "outside"])
async def test_git_listing_refuses_symlinked_parent(tmp_path: Path, target: str) -> None:
    root = init_repo(tmp_path / "repo")
    write(root / "docs" / "config", "tracked\n")
    write(root / "README.md")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    outside = tmp_path / "outside"
    write(outside / "config", "OUTSIDE\n")
    (root / "docs" / "config").unlink()
    (root / "docs").rmdir()
    os.symlink(root / ".git" if target == "git" else outside, root / "docs")
    assert paths(await list_repo(git_policy(root))) == ["README.md"]


async def test_listing_drops_symlinked_parent_inside_root(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    write(root / "real" / "a.md")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    # a tracked path whose parent became a symlink to another directory inside the root
    write(root / "other" / "a.md")
    (root / "real" / "a.md").unlink()
    (root / "real").rmdir()
    os.symlink("other", root / "real")
    assert "real/a.md" not in paths(await list_repo(git_policy(root)))


async def test_walk_without_git_prunes_blocked_dirs(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    write(root / "README.md")
    write(root / ".kicad-agent" / "history" / "h.json")
    write(root / "x-backups" / "old.zip", b"PK")
    write(root / "sub" / "fp-info-cache")
    write(root / "sub" / "notes.txt")
    write(root / ".env")
    assert paths(await list_repo(walk_policy(root))) == ["README.md", "sub/notes.txt"]


async def test_walk_does_not_follow_dir_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    write(root / "real" / "a.txt")
    outside = tmp_path / "outside"
    write(outside / "b.txt")
    os.symlink(root / "real", root / "loop")
    os.symlink(outside, root / "out")
    os.symlink(outside / "b.txt", root / "b.txt")
    assert paths(await list_repo(walk_policy(root))) == ["real/a.txt"]


async def test_walk_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listing, "WALK_CAP", 5)
    root = tmp_path / "plain"
    for i in range(8):
        write(root / f"f{i}.txt")
    result = await list_repo(walk_policy(root))
    assert result.truncated
    assert len(result.files) == 5


async def test_git_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listing, "WALK_CAP", 3)
    root = init_repo(tmp_path / "repo")
    for i in range(6):
        write(root / f"f{i}.txt")
    result = await list_repo(git_policy(root))
    assert result.truncated
    assert len(result.files) == 3


async def test_kicad_and_binary_listed(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    write(root / "board.kicad_pcb", "(kicad_pcb)\n")
    write(root / "fp-lib-table", "(fp_lib_table)\n")
    write(root / "logo.png", b"\x89PNG\x00\x00")
    assert paths(await list_repo(walk_policy(root))) == [
        "board.kicad_pcb",
        "fp-lib-table",
        "logo.png",
    ]


async def test_listing_is_cached(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    write(root / "a.txt")
    first = await list_repo(walk_policy(root))
    write(root / "b.txt")
    assert await list_repo(walk_policy(root)) is first
    listing.clear_listing_cache()
    assert paths(await list_repo(walk_policy(root))) == ["a.txt", "b.txt"]


def _corrupt_index(root: Path) -> None:
    (root / ".git" / "index").write_bytes(b"DIRC" + b"\xff" * 64)


async def test_failed_ls_files_raises_and_is_not_cached(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    write(root / ".gitignore", "build/\n")
    write(root / "build" / "ignored.txt")
    write(root / "README.md")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    _corrupt_index(root)
    with pytest.raises(listing.ListingError):
        await list_repo(git_policy(root))
    assert root.resolve() not in listing._cache
    (root / ".git" / "index").unlink()
    git(root, "reset", "-q")
    assert "build/ignored.txt" not in paths(await list_repo(git_policy(root)))


async def test_ls_files_output_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = init_repo(tmp_path / "repo")
    for i in range(50):
        write(root / f"file{i:03}.txt")
    monkeypatch.setattr(listing, "LS_FILES_MAX_BYTES", 100)
    result = await list_repo(git_policy(root))
    assert result.truncated
    assert 0 < len(result.files) <= 100 // len("file000.txt")
    assert all(f.rel.name.startswith("file") and f.rel.suffix == ".txt" for f in result.files)
