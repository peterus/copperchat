import os
from pathlib import Path, PurePosixPath

import pytest

from copperchat.gitproc import GitTimeoutError
from copperchat.repo_context import paths as paths_mod
from copperchat.repo_context.paths import (
    RepoPathError,
    RepoPolicy,
    blocked_reason,
    check_rel_path,
    is_kicad_file,
    is_text_bytes,
    is_text_file,
    repo_policy,
    resolve_dir,
    resolve_file,
)

from .conftest import init_repo, make_project


def policy_for(root: Path) -> RepoPolicy:
    return RepoPolicy(
        root=root.resolve(),
        project_dir=root.resolve(),
        is_git=False,
        projects_root=root.parent.resolve(),
    )


async def test_root_is_git_toplevel(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    project = make_project(repo / "hw" / "pcb")
    policy = await repo_policy(project, tmp_path)
    assert policy.root == repo.resolve()
    assert policy.is_git
    assert policy.rel_project_dir == "hw/pcb"
    assert policy.rel_root == "repo"


async def test_root_falls_back_without_git(tmp_path: Path) -> None:
    project = make_project(tmp_path / "plain")
    policy = await repo_policy(project, tmp_path)
    assert policy.root == project.dir.resolve()
    assert not policy.is_git
    assert policy.rel_project_dir == "."


async def test_root_outside_projects_root_refused(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "outer")
    projects_root = repo / "projects"
    project = make_project(projects_root / "board")
    policy = await repo_policy(project, projects_root)
    assert policy.root == project.dir.resolve()
    assert not policy.is_git


async def test_root_equal_projects_root_refused(tmp_path: Path) -> None:
    projects_root = init_repo(tmp_path / "projects")
    project = make_project(projects_root / "board")
    policy = await repo_policy(project, projects_root)
    assert policy.root == project.dir.resolve()
    assert not policy.is_git


async def test_root_is_cached(tmp_path: Path) -> None:
    project = make_project(tmp_path / "plain")
    first = await repo_policy(project, tmp_path)
    init_repo(project.dir)
    assert await repo_policy(project, tmp_path) is first


@pytest.mark.parametrize(
    "raw", ["/etc/passwd", "~/x", "a\\b", "a/../b", "..", "a\x00b", "a\nb", "x" * 401]
)
def test_static_refusals(raw: str) -> None:
    with pytest.raises(RepoPathError) as err:
        check_rel_path(raw)
    assert err.value.message


def test_static_normalises() -> None:
    assert check_rel_path("") == PurePosixPath(".")
    assert check_rel_path(".") == PurePosixPath(".")
    assert check_rel_path("a//b/./c/") == PurePosixPath("a/b/c")


@pytest.mark.parametrize(
    "rel",
    [
        ".git/config",
        "sub/.GIT/HEAD",
        ".ENV",
        ".env.local",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ecdsa.pub",
        "cert.pem",
        "my.key",
        "a.p12",
        "a.pfx",
        "a.keystore",
        "a.jks",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "app_secret.txt",
        "config/Secrets.yaml",
        "vault.kdbx",
        ".kicad-agent/x",
        "foo-backups/a.txt",
        "fp-info-cache",
        "~x.lck",
        ".git-credentials",
        ".ssh/config",
        ".gnupg/pubring.kbx",
        ".aws/config",
        ".pgpass",
        ".htpasswd",
        "infra/terraform.tfstate",
        "terraform.tfstate.backup",
        "prod.tfvars",
        ".envrc",
        ".env-prod",
        ".env_local",
        "prod.env",
        "config/staging.ENV",
        "id_dsa",
        "id_dsa.pub",
    ],
)
def test_block_list(rel: str) -> None:
    assert blocked_reason(PurePosixPath(rel)) is not None


@pytest.mark.parametrize(
    "rel",
    [
        "README.md",
        "enclosure.scad",
        "docs/env.md",
        "environment.md",
        "board.kicad_pcb",
        ".",
    ],
)
def test_block_list_allows(rel: str) -> None:
    assert blocked_reason(PurePosixPath(rel)) is None


@pytest.mark.parametrize(
    "name",
    [
        "a.kicad_sch",
        "a.kicad_pcb",
        "a.kicad_sym",
        "a.kicad_mod",
        "a.KICAD_PRO",
        "a.kicad_prl",
        "a.kicad_dru",
        "a.kicad_wks",
        "sym-lib-table",
        "hw/fp-lib-table",
    ],
)
def test_kicad_files(name: str) -> None:
    assert is_kicad_file(PurePosixPath(name))


def test_not_kicad_file() -> None:
    assert not is_kicad_file(PurePosixPath("README.md"))


def test_resolve_file_ok(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("x")
    path, rel = resolve_file(policy_for(root), "docs/a.md")
    assert path == (root / "docs" / "a.md").resolve()
    assert rel == PurePosixPath("docs/a.md")


def test_resolve_file_missing_and_dir(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "docs").mkdir(parents=True)
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "nope.md")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "docs")


def test_resolve_dir(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "docs").mkdir(parents=True)
    (root / "f.txt").write_text("x")
    assert resolve_dir(policy_for(root), "")[0] == root.resolve()
    assert resolve_dir(policy_for(root), "docs")[1] == PurePosixPath("docs")
    with pytest.raises(RepoPathError):
        resolve_dir(policy_for(root), "f.txt")
    with pytest.raises(RepoPathError):
        resolve_dir(policy_for(root), "missing")


def test_resolve_blocked_given_path(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / ".env").write_text("SECRET=1")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), ".env")


def test_symlink_out_of_root_refused(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x")
    os.symlink(outside / "x.txt", root / "link.txt")
    os.symlink(outside, root / "linkdir")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "link.txt")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "linkdir/x.txt")
    with pytest.raises(RepoPathError):
        resolve_dir(policy_for(root), "linkdir")


def test_symlink_to_git_config_refused(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "r")
    os.symlink(root / ".git" / "config", root / "harmless.txt")
    os.symlink(root / ".git", root / "gitdir")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "harmless.txt")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "gitdir/config")


def test_symlink_to_env_refused(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / ".env").write_text("SECRET=1")
    os.symlink(".env", root / "notes.txt")
    with pytest.raises(RepoPathError):
        resolve_file(policy_for(root), "notes.txt")


def test_symlink_inside_ok(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("x")
    os.symlink("docs/a.md", root / "alias.md")
    path, rel = resolve_file(policy_for(root), "alias.md")
    assert path == (root / "docs" / "a.md").resolve()
    assert rel == PurePosixPath("alias.md")


def test_text_detection(tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("hällo\n", encoding="utf-8")
    nul = tmp_path / "nul.bin"
    nul.write_bytes(b"abc\x00def")
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe")
    cut = tmp_path / "cut.txt"
    cut.write_bytes(b"a" * 8191 + "ä".encode() + b"tail")
    assert is_text_file(plain)
    assert not is_text_file(nul)
    assert not is_text_file(bad)
    assert is_text_file(cut)
    assert not is_text_file(tmp_path / "missing")


def test_text_bytes() -> None:
    assert is_text_bytes("hällo\n".encode())
    assert not is_text_bytes(b"abc\x00def")
    assert not is_text_bytes(b"\xff\xfe")
    assert is_text_bytes(b"a" * 8191 + "ä".encode())  # cut at the sniff edge
    assert not is_text_bytes(b"abc" + "ä".encode()[:1])  # short and incomplete


async def test_transient_git_failure_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    project = make_project(repo / "board")
    real = paths_mod.run_git
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitTimeoutError("slow")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paths_mod, "run_git", flaky)
    first = await repo_policy(project, tmp_path)
    assert not first.is_git
    second = await repo_policy(project, tmp_path)
    assert second.is_git and second.root == repo.resolve()
    assert await repo_policy(project, tmp_path) is second


async def test_other_rev_parse_failure_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    project = make_project(repo / "board")
    real = paths_mod.run_git
    calls = 0

    async def dubious(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 128, b"", b"fatal: detected dubious ownership in repository at '/x'\n"
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(paths_mod, "run_git", dubious)
    first = await repo_policy(project, tmp_path)
    assert not first.is_git
    second = await repo_policy(project, tmp_path)
    assert second.is_git and second.root == repo.resolve()


async def test_not_a_git_repository_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path / "plain")
    calls = 0

    async def no_repo(*_args: object, **_kwargs: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        return 128, b"", b"fatal: not a git repository (or any of the parent directories): .git\n"

    monkeypatch.setattr(paths_mod, "run_git", no_repo)
    first = await repo_policy(project, tmp_path)
    assert not first.is_git
    assert await repo_policy(project, tmp_path) is first
    assert calls == 1
