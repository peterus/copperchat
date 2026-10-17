"""docs/parts/<MPN>/ storage shared by the Parts page and the part-researcher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from copperchat.errors import CopperChatError
from copperchat.fsutil import atomic_write_text
from copperchat.projects import Project
from copperchat.research.paths import parts_root
from copperchat.skills.store import parse_front_matter

_MPN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SEP_RE = re.compile(r"[\s/\\]+")
MAX_NOTES_BYTES = 512 * 1024


class InvalidMpn(CopperChatError):
    code = "invalid_mpn"
    http_status = 400


def mpn_dirname(mpn: str) -> str:
    """Folder name for a manufacturer part number: '/', '\\' and whitespace become '_';
    then letters, digits, '.', '_', '+', '-' only, no leading '_'/'.', no '..'."""
    name = _SEP_RE.sub("_", str(mpn).strip())
    if not _MPN_RE.fullmatch(name) or ".." in name:
        raise InvalidMpn(f"invalid part number: {mpn!r}")
    return name


def _real_root(project: Project) -> Path:
    root = parts_root(project)
    real = root.resolve(strict=False)
    if not real.is_relative_to(project.dir.resolve(strict=False)):
        raise InvalidMpn("docs/parts resolves outside the project")
    return root


def part_dir(project: Project, mpn: str) -> Path:
    """``docs/parts/<mpn_dirname(mpn)>``; refuses symlinked folders and escapes."""
    root = _real_root(project)
    d = root / mpn_dirname(mpn)
    if d.is_symlink() or not d.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise InvalidMpn(f"part folder is a symlink or escapes docs/parts: {mpn!r}")
    return d


def notes_user_edited(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    meta, _ = parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))
    return meta.get("user_edited", "").lower() == "true"


@dataclass(frozen=True)
class PartInfo:
    mpn: str
    has_notes: bool
    has_datasheet: bool
    user_edited: bool
    updated_at: str | None


def _regular(p: Path) -> bool:
    return p.is_file() and not p.is_symlink()


def list_parts(project: Project) -> list[PartInfo]:
    try:
        root = _real_root(project)
    except InvalidMpn:
        return []
    if root.is_symlink() or not root.is_dir():
        return []
    result: list[PartInfo] = []
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir() or not _MPN_RE.fullmatch(d.name):
            continue
        notes, sheet = d / "notes.md", d / "datasheet.pdf"
        mtimes = [p.stat().st_mtime for p in (notes, sheet) if _regular(p)]
        result.append(
            PartInfo(
                mpn=d.name,
                has_notes=_regular(notes),
                has_datasheet=_regular(sheet),
                user_edited=notes_user_edited(notes),
                updated_at=datetime.fromtimestamp(max(mtimes), UTC).isoformat() if mtimes else None,
            )
        )
    return result


def read_notes(project: Project, mpn: str) -> str:
    notes = part_dir(project, mpn) / "notes.md"
    if not _regular(notes):
        return ""
    return notes.read_text(encoding="utf-8", errors="replace")


def write_user_notes(project: Project, mpn: str, text: str) -> None:
    """Save notes edited by the user; forces ``user_edited: true`` so the researcher
    never overwrites them."""
    if len(text.encode("utf-8")) > MAX_NOTES_BYTES:
        raise InvalidMpn("notes exceed 512 KiB")  # mapped to 400 like other bad input
    meta, body = parse_front_matter(text)
    meta.pop("user_edited", None)
    meta.setdefault("mpn", mpn_dirname(mpn))
    header = "".join(f"{k}: {v}\n" for k, v in meta.items())
    content = f"---\n{header}user_edited: true\n---\n{body.lstrip(chr(10))}"
    d = part_dir(project, mpn)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "notes.md"
    if target.is_symlink():
        raise InvalidMpn(f"notes.md of {mpn!r} is a symlink")
    atomic_write_text(target, content)


def datasheet_path(project: Project, mpn: str) -> Path | None:
    try:
        p = part_dir(project, mpn) / "datasheet.pdf"
    except InvalidMpn:
        return None
    return p if _regular(p) else None
