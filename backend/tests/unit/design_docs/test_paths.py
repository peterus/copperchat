"""Path guard and safe readers/writers (C3 Task 1, Review Focus 3)."""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from copperchat.design_docs import paths
from copperchat.design_docs.paths import (
    DocError,
    parse_doc_path,
    resolve_doc_path,
    safe_read,
    safe_write,
    walk,
)
from copperchat.projects import Project

REFUSED_RAW = [
    "..",
    "/etc/passwd",
    "docs/design/../x.md",
    "docs/design/a\x00.md",
    "docs\\design\\a.md",
    "Docs/design/a.md",
    "docs/design/A.md",
    "docs/design/.hidden.md",
    "docs/design/a/b/c.md",
    "docs/design/a.txt",
    "docs/design/a.md\n",
    "docs/design/a\x07b.md",
    "docs/design/" + "a" * 190 + ".md",
    "docs/parts/x.md",
    "docs/design/",
    "",
    "docs/design/\uff41.md",  # fullwidth look-alike
    "docs/design/a\u2028.md",
    "docs/design/.md",
]


@pytest.mark.parametrize("raw", REFUSED_RAW)
def test_refused_raw_paths(project: Project, raw: str) -> None:
    with pytest.raises(DocError) as err:
        resolve_doc_path(project, raw)
    assert err.value.code == "doc_path_refused"
    assert str(project.dir) not in err.value.why


def test_bare_name_maps_to_docs_design(project: Project) -> None:
    doc = resolve_doc_path(project, "requirements.md")
    assert doc.rel == "docs/design/requirements.md"
    assert doc.fixed
    nested = parse_doc_path("docs/design/notes/usb-pd.md")
    assert nested.rel == "docs/design/notes/usb-pd.md" and not nested.fixed
    assert nested.doc_key == "notes/usb-pd"


def _outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside"
    out.mkdir(exist_ok=True)
    (out / "secret.md").write_text("TOKEN=1\n")
    return out


def _assert_refused_both(project: Project, raw: str) -> None:
    doc = parse_doc_path(raw)
    with pytest.raises(DocError) as e1:
        safe_read(project, doc)
    assert e1.value.code in ("doc_path_refused", "doc_too_large")
    with pytest.raises(DocError) as e2:
        safe_write(project, doc, b"x")
    assert e2.value.code in ("doc_path_refused", "doc_too_large")


def test_symlinked_docs_dir_refused(project: Project, tmp_path: Path) -> None:
    out = _outside(tmp_path)
    (out / "design").mkdir()
    (out / "design" / "requirements.md").write_text("x")
    (project.dir / "docs").symlink_to(out)
    _assert_refused_both(project, "requirements.md")
    assert (out / "design" / "requirements.md").read_text() == "x"


def test_symlinked_design_dir_refused(project: Project, tmp_path: Path) -> None:
    out = _outside(tmp_path)
    (project.dir / "docs").mkdir()
    (project.dir / "docs" / "design").symlink_to(out)
    _assert_refused_both(project, "docs/design/secret.md")
    assert (out / "secret.md").read_text() == "TOKEN=1\n"


def test_symlinked_intermediate_dir_refused(project: Project, design: Path, tmp_path: Path) -> None:
    out = _outside(tmp_path)
    (design / "notes").symlink_to(out)
    _assert_refused_both(project, "docs/design/notes/secret.md")
    assert (out / "secret.md").read_text() == "TOKEN=1\n"


def test_symlinked_target_refused(project: Project, design: Path) -> None:
    (design / "requirements.md").symlink_to("/proc/self/environ")
    _assert_refused_both(project, "requirements.md")
    assert (design / "requirements.md").is_symlink()


def test_hard_link_refused_for_write_only(project: Project, design: Path, tmp_path: Path) -> None:
    out = _outside(tmp_path)
    os.link(out / "secret.md", design / "parts.md")
    doc = parse_doc_path("parts.md")
    data = safe_read(project, doc)
    assert data is not None and data.data == b"TOKEN=1\n"
    with pytest.raises(DocError) as err:
        safe_write(project, doc, b"overwritten")
    assert err.value.code == "doc_path_refused"
    assert (out / "secret.md").read_text() == "TOKEN=1\n"


def test_fifo_read_does_not_block(project: Project, design: Path) -> None:
    os.mkfifo(design / "requirements.md")
    doc = parse_doc_path("requirements.md")
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            safe_read(project, doc)
        except DocError as exc:
            outcome["code"] = exc.code

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(1.0)
    assert not t.is_alive(), "reading a FIFO blocked"
    assert outcome["code"] == "doc_path_refused"
    with pytest.raises(DocError):
        safe_write(project, doc, b"x")


def test_oversized_file_refused(project: Project, design: Path) -> None:
    (design / "requirements.md").write_bytes(b"a" * (paths.MAX_DOC_BYTES + 1))
    doc = parse_doc_path("requirements.md")
    with pytest.raises(DocError) as err:
        safe_read(project, doc)
    assert err.value.code == "doc_too_large"
    with pytest.raises(DocError) as err2:
        safe_write(project, doc, b"a" * (paths.MAX_DOC_BYTES + 1))
    assert err2.value.code == "doc_too_large"


def test_missing_doc_reads_none_and_write_creates_dirs(project: Project) -> None:
    doc = parse_doc_path("docs/design/notes/usb.md")
    assert safe_read(project, doc) is None
    safe_write(project, doc, b"hello\n")
    data = safe_read(project, doc)
    assert data is not None and data.data == b"hello\n"
    # no temp files left behind
    assert sorted(p.name for p in (project.dir / "docs/design/notes").iterdir()) == ["usb.md"]


def test_read_race_symlink_swapped_in_after_check(
    project: Project, design: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = design / "requirements.md"
    target.write_text("ok")

    def swap(stage: str, **_: Any) -> None:
        if stage == "checked":
            target.unlink()
            target.symlink_to("/proc/self/environ")

    monkeypatch.setattr(paths, "_race_hook", swap)
    with pytest.raises(DocError) as err:
        safe_read(project, parse_doc_path("requirements.md"))
    assert err.value.code == "doc_path_refused"


def test_write_race_dir_swapped_after_dir_fd_opened(
    project: Project, design: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outside(tmp_path)
    moved = project.dir / "docs" / "design.orig"

    def swap(stage: str, **_: Any) -> None:
        if stage == "dir_opened":
            design.rename(moved)
            design.symlink_to(out)

    monkeypatch.setattr(paths, "_race_hook", swap)
    with contextlib.suppress(DocError):
        safe_write(project, parse_doc_path("requirements.md"), b"data")
    assert not (out / "requirements.md").exists()
    assert sorted(p.name for p in out.iterdir()) == ["secret.md"]
    # it landed in the original directory (now renamed) or nowhere
    assert {p.name for p in moved.iterdir()} <= {"requirements.md"}


def test_write_race_dir_swapped_before_dir_fd_opened(
    project: Project, design: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outside(tmp_path)

    def swap(stage: str, **_: Any) -> None:
        if stage == "checked":
            shutil.rmtree(design)
            design.symlink_to(out)

    monkeypatch.setattr(paths, "_race_hook", swap)
    with pytest.raises(DocError) as err:
        safe_write(project, parse_doc_path("requirements.md"), b"data")
    assert err.value.code == "doc_path_refused"
    assert sorted(p.name for p in out.iterdir()) == ["secret.md"]


def test_walk_bounds_and_ignored(project: Project, design: Path, tmp_path: Path) -> None:
    out = _outside(tmp_path)
    (design / "requirements.md").write_text("r")
    (design / "architecture.md").symlink_to("/proc/self/environ")
    os.mkfifo(design / "parts.md")
    (design / "big.md").write_bytes(b"a" * (paths.MAX_DOC_BYTES + 1))
    (design / "Upper.md").write_text("u")
    (design / "linked").symlink_to(out)
    sub = design / "notes"
    sub.mkdir()
    (sub / "usb.md").write_text("u")
    (sub / "deep").mkdir()
    (sub / "deep" / "x.md").write_text("x")
    for i in range(60):
        (design / f"extra{i:02d}.md").write_text("e")
    res = walk(project)
    rels = [e.doc.rel for e in res.docs]
    assert len(rels) == paths.MAX_DOCS
    assert rels[0] == "docs/design/requirements.md"
    reasons = {i.path: i.reason for i in res.ignored}
    assert reasons["docs/design/architecture.md"] == "symlink"
    assert reasons["docs/design/parts.md"] == "not a regular file"
    assert reasons["docs/design/big.md"] == "too large"
    assert reasons["docs/design/Upper.md"] == "bad name"
    assert reasons["docs/design/linked"] == "symlink"
    assert reasons["docs/design/notes/deep"] == "bad name"
    assert "too many docs" in reasons.values()
    assert "docs/design/notes/x.md" not in rels


def test_walk_symlinked_root_is_ignored(project: Project, tmp_path: Path) -> None:
    out = _outside(tmp_path)
    (project.dir / "docs").mkdir()
    (project.dir / "docs" / "design").symlink_to(out)
    res = walk(project)
    assert res.docs == ()
    assert res.ignored[0].reason == "symlink"


def test_walk_opens_no_file(
    project: Project, design: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (design / "requirements.md").write_text("r")
    os.mkfifo(design / "parts.md")
    real_open = os.open
    opened: list[str] = []

    def spy(path: Any, flags: int, *a: Any, **kw: Any) -> int:
        if not flags & os.O_DIRECTORY:
            opened.append(str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    walk(project)
    assert opened == []
