"""Safe fab output paths and the two-step swap (SP4 P8, spec §7.2 "Checkpoints and output").

The fab exports write only below `<project>/fab/`: `safe_output_dir` refuses a symlinked or
non-directory `fab/` (or target) and anything resolving outside the project; packages are
built in a staging dir next to the target and swapped in with two renames, so a failed
export never leaves a half-written package and keeps the old one. Every rename / rmtree is
guarded again right before it runs (`_guard`): the path must sit directly in a real `fab/`
directory (or one level below it, `fab/3d/<file>`). Pure stdlib, no kipy.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

from copperchat.errors import StructuredToolError

log = logging.getLogger(__name__)

FAB_DIR = "fab"
_CALL_ID = re.compile(r"^[0-9a-f]{12}$")


def new_call_id() -> str:
    """12 hex chars from `uuid4`: names the staging (`.tmp-`) and old (`.old-`) dirs."""
    return uuid.uuid4().hex[:12]


def _unsafe(rel: str, reason: str) -> StructuredToolError:
    why = {
        "symlink": f"{rel} is (or lies under) a symbolic link",
        "outside": f"{rel} is not a directory inside the project's fab/ folder",
        "not_a_dir": f"{rel} exists but is not a directory",
    }[reason]
    return StructuredToolError(
        "The fab output folder is not safe to write",
        why,
        "Ask the user to replace it with a plain directory inside the project (or remove "
        "it), then export again.",
        code="output_path_unsafe",
        details={"path": rel, "reason": reason},
    )


def export_failed(why: str) -> StructuredToolError:
    return StructuredToolError(
        "The export failed",
        why,
        "Nothing was replaced; the previous package (if any) is unchanged. Fix the cause or "
        "tell the user.",
        code="export_failed",
    )


def _check_dir(path: Path, rel: str, *, missing_ok: bool) -> bool:
    """lstat `path`: a symlink or a non-directory → `output_path_unsafe`; returns whether it
    exists (absent and `missing_ok` → False, absent otherwise → `not_a_dir`)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return False
        raise _unsafe(rel, "not_a_dir") from None
    if stat.S_ISLNK(st.st_mode):
        raise _unsafe(rel, "symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(rel, "not_a_dir")
    return True


def _inside(path: Path, root: Path) -> bool:
    return path.resolve() == root.resolve() or path.resolve().is_relative_to(root.resolve())


def safe_output_dir(project_dir: Path, rel: str) -> Path:
    """`project_dir / rel` (rel = "fab/jlcpcb" | "fab/3d") after lstat'ing every component
    below `project_dir`: a symlink → refused, a non-directory → refused, a `..`/absolute/
    empty component → "outside". Creates the intermediate dirs (`fab/`) when missing (mkdir
    of one plain name in a validated parent: nothing is followed); the target itself may be
    absent. The resolved target must lie inside `project_dir.resolve()`."""
    pure = PurePosixPath(rel)
    parts = rel.split("/")
    if (
        not rel
        or pure.is_absolute()
        or any(p in ("", ".", "..") for p in parts)
        or parts[0] != FAB_DIR
        or len(parts) < 2
    ):
        raise _unsafe(rel, "outside")
    cur = project_dir
    for i, part in enumerate(parts):
        cur = cur / part
        shown = "/".join(parts[: i + 1])
        last = i == len(parts) - 1
        if not _check_dir(cur, shown, missing_ok=True) and not last:
            cur.mkdir()
            _check_dir(cur, shown, missing_ok=False)
    if not _inside(cur.parent, project_dir) or (
        os.path.lexists(cur) and not _inside(cur, project_dir)
    ):
        raise _unsafe(rel, "outside")
    return cur


def _fab_root(path: Path) -> Path:
    """The `fab` directory `path` lives in (directly, or one level below it): every dir from
    `fab` down to `path.parent` must be a real directory (lstat, no symlink); otherwise
    `output_path_unsafe` ("outside" when no `fab` ancestor within two levels)."""
    for up in (path.parent, path.parent.parent):
        if up.name == FAB_DIR:
            d = path.parent
            while True:
                _check_dir(d, str(d), missing_ok=False)
                if d == up:
                    return up
                d = d.parent
    raise _unsafe(str(path), "outside")


def _guard(*paths_: Path) -> None:
    """Asserted before every rename / rmtree: each path lies in a real `fab/` dir."""
    for p in paths_:
        _fab_root(p)


def staging_dir(target: Path, call_id: str) -> Path:
    """`target.parent / ".<name>.tmp-<call_id>"`, created empty (never reused: an existing
    one raises `FileExistsError`)."""
    if not _CALL_ID.match(call_id):
        raise ValueError(f"bad call id {call_id!r}")
    tmp = target.parent / f".{target.name}.tmp-{call_id}"
    _guard(tmp)
    tmp.mkdir()
    return tmp


def swap_in(new: Path, target: Path, call_id: str) -> str | None:
    """Two-step swap of the finished package `new` into `target`: (1) an existing target is
    renamed to `<name>.old-<call_id>`; (2) `new` is renamed to `target` - on failure the old
    package is renamed back and `export_failed` raised; (3) the old package is removed. A
    failing removal is logged and returned as a warning (the export still succeeded)."""
    _guard(new, target)
    old = target.parent / f"{target.name}.old-{call_id}"
    moved = False
    if os.path.lexists(target):
        _guard(old)
        os.rename(target, old)
        moved = True
    try:
        os.rename(new, target)
    except OSError as exc:
        if moved:
            os.rename(old, target)
        raise export_failed(
            f"the new package could not be moved into place ({type(exc).__name__}: {exc})"
        ) from None
    if not moved:
        return None
    try:
        _guard(old)
        shutil.rmtree(old)
    except OSError as exc:
        log.warning("removing the previous fab package %s failed: %s", old, exc)
        return f"the previous package {old.name} could not be removed ({type(exc).__name__})"
    return None


def replace_file(new: Path, target: Path) -> None:
    """`os.replace(new, target)` after guarding both (the 3D export's single file)."""
    _guard(new, target)
    os.replace(new, target)


def _leftover_pattern(target_name: str) -> re.Pattern[str]:
    name = re.escape(target_name)
    return re.compile(rf"^(?:\.{name}\.tmp-|{name}\.old-)[0-9a-f]{{12}}$")


def _restore_orphan(fab_dir: Path, target_name: str, out: list[str]) -> None:
    """A crash between the two renames of `swap_in` leaves no target but its
    `<target>.old-<id>`: with the target absent and exactly one such plain dir, it is the
    only copy of the previous package and is renamed back (reported as "<name> (restored)")
    instead of being swept."""
    if os.path.lexists(fab_dir / target_name):
        return
    old = re.compile(rf"^{re.escape(target_name)}\.old-[0-9a-f]{{12}}$")
    found = [
        e
        for e in sorted(os.listdir(fab_dir))
        if old.match(e) and stat.S_ISDIR(os.lstat(fab_dir / e).st_mode)
    ]
    if len(found) != 1:
        return
    _guard(fab_dir / found[0], fab_dir / target_name)
    os.rename(fab_dir / found[0], fab_dir / target_name)
    out.append(f"{found[0]} (restored)")


def sweep_stale(fab_dir: Path, target_name: str) -> list[str]:
    """Remove leftovers of crashed earlier exports in `fab_dir` (call it only with the
    `fab/` that `safe_output_dir` validated; it re-checks that `fab_dir` is a real `fab`
    directory and raises `output_path_unsafe` otherwise). Only entries named
    `.<target>.tmp-<12 hex>` or `<target>.old-<12 hex>` that lstat as a plain directory are
    removed; a symlink or file of that name is skipped and logged, never followed. With the
    target missing, a single `<target>.old-<id>` is renamed back first (`_restore_orphan`).
    Returns the handled names (sorted; a restored one as "<name> (restored)")."""
    if fab_dir.name != FAB_DIR:
        raise _unsafe(str(fab_dir), "outside")
    _check_dir(fab_dir, FAB_DIR, missing_ok=False)
    pattern = _leftover_pattern(target_name)
    removed: list[str] = []
    _restore_orphan(fab_dir, target_name, removed)
    for entry in sorted(os.listdir(fab_dir)):
        if not pattern.match(entry):
            continue
        path = fab_dir / entry
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(st.st_mode):  # S_ISDIR on an lstat result is False for links
            log.warning("fab leftover %s is not a plain directory; skipped", path)
            continue
        _guard(path)
        shutil.rmtree(path)
        removed.append(entry)
    return removed
