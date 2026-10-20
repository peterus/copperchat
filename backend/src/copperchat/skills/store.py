"""Three-layer skill storage: built-in (shipped), user (<data_dir>/skills), project.

A layer entry counts only if ``<root>/<name>/`` is a real directory (not a symlink), its
name passes validation, and ``SKILL.md`` is a regular file (not a symlink) of at most
256 KiB whose front matter has ``name`` == directory name and a non-empty ``description``.
Invalid entries are skipped silently in listings (they never reach the SDK).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from copperchat.config import Settings
from copperchat.errors import CopperChatError
from copperchat.fsutil import atomic_write_text
from copperchat.projects import Project, agent_dir

Origin = Literal["builtin", "user", "project"]
BUILTIN_ROOT = Path(__file__).parent / "builtin"
MAX_SKILL_BYTES = 256 * 1024
SIDECAR = ".origin.json"
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class InvalidSkillName(CopperChatError):
    code = "invalid_skill_name"
    http_status = 400


class InvalidSkill(CopperChatError):
    code = "invalid_skill"
    http_status = 400


class SkillNotFound(CopperChatError):
    code = "skill_not_found"
    http_status = 404


class SkillConflict(CopperChatError):
    code = "skill_conflict"
    http_status = 409


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InvalidSkillName(f"invalid skill name: {name!r}")
    return name


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Flat ``key: value`` front matter between ``---`` lines; ({}, text) if there is none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "".join(lines[i + 1 :])
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line[:1].isspace():
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return {}, text


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillLayers:
    builtin_root: Path
    user_root: Path
    project_root: Path | None

    @classmethod
    def for_project(cls, settings: Settings, project: Project | None) -> SkillLayers:
        return cls(
            builtin_root=BUILTIN_ROOT,
            user_root=settings.data_dir / "skills",
            project_root=None if project is None else agent_dir(project) / "skills",
        )


@dataclass(frozen=True)
class EffectiveSkill:
    name: str
    origin: Origin
    description: str
    dir: Path
    shadowed: tuple[Origin, ...]
    has_builtin: bool
    origin_updated: bool
    # Front matter `agent`: the skill belongs to that agent (e.g. part-researcher), not
    # to the main agent.
    agent: str | None = None
    # Front matter `sessions` (comma-separated session types, spec §8.4): the skill is
    # offered only in those types; None = every type.
    sessions: frozenset[str] | None = None


@dataclass(frozen=True)
class _Entry:
    dir: Path
    description: str
    content: str
    agent: str | None
    sessions: frozenset[str] | None = None


def parse_sessions(value: str | None) -> frozenset[str] | None:
    """``sessions: schematic, free`` -> {"schematic", "free"}; missing or blank -> None."""
    types = frozenset(t.strip().lower() for t in (value or "").split(",") if t.strip())
    return types or None


def _load(root: Path | None, name: str) -> _Entry | None:
    if root is None:
        return None
    d = root / name
    md = d / "SKILL.md"
    try:
        if d.is_symlink() or not d.is_dir() or md.is_symlink() or not md.is_file():
            return None
        if md.stat().st_size > MAX_SKILL_BYTES:
            return None
        content = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, _ = parse_front_matter(content)
    if meta.get("name") != name or not meta.get("description"):
        return None
    return _Entry(
        d,
        meta["description"],
        content,
        meta.get("agent") or None,
        parse_sessions(meta.get("sessions")),
    )


def _names(root: Path | None) -> set[str]:
    if root is None or root.is_symlink() or not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if _NAME_RE.fullmatch(p.name)}


def _read_sidecar(user_dir: Path) -> dict[str, str] | None:
    sidecar = user_dir / SIDECAR
    if sidecar.is_symlink() or not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("builtinContent"), str):
        return None
    return {k: str(v) for k, v in data.items()}


def _origin_updated(layers: SkillLayers, name: str, user_dir: Path) -> bool:
    builtin = _load(layers.builtin_root, name)
    data = _read_sidecar(user_dir)
    if builtin is None or data is None:
        return False
    return data.get("builtinSha256") != _sha(builtin.content)


def effective_skills(layers: SkillLayers) -> list[EffectiveSkill]:
    order: list[tuple[Origin, Path | None]] = [
        ("project", layers.project_root),
        ("user", layers.user_root),
        ("builtin", layers.builtin_root),
    ]
    names: set[str] = set().union(*(_names(root) for _, root in order))
    result: list[EffectiveSkill] = []
    for name in sorted(names):
        present = [(o, e) for o, e in ((o, _load(root, name)) for o, root in order) if e]
        if not present:
            continue
        origin, entry = present[0]
        result.append(
            EffectiveSkill(
                name=name,
                origin=origin,
                description=entry.description,
                dir=entry.dir,
                shadowed=tuple(o for o, _ in present[1:]),
                has_builtin=any(o == "builtin" for o, _ in present),
                origin_updated=origin == "user" and _origin_updated(layers, name, entry.dir),
                agent=entry.agent,
                sessions=entry.sessions,
            )
        )
    return result


def get_skill(layers: SkillLayers, name: str) -> EffectiveSkill:
    validate_skill_name(name)
    for s in effective_skills(layers):
        if s.name == name:
            return s
    raise SkillNotFound(f"skill not found: {name}")


def _user_dir(layers: SkillLayers, name: str) -> Path:
    validate_skill_name(name)
    if layers.user_root.is_symlink():
        raise InvalidSkill("the user skill directory must not be a symlink")
    d = layers.user_root / name
    if d.is_symlink():
        raise InvalidSkill(f"user skill {name} is a symlink")
    return d


def _atomic_write(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _sidecar_text(content: str) -> str:
    return json.dumps({"builtinSha256": _sha(content), "builtinContent": content})


def customise(layers: SkillLayers, name: str) -> Path:
    """Copy a built-in skill into the user layer (with an origin sidecar for update diffs)."""
    dest = _user_dir(layers, name)
    builtin = _load(layers.builtin_root, name)
    if builtin is None:
        raise SkillNotFound(f"no built-in skill named {name}")
    if dest.exists():
        raise SkillConflict(f"skill {name} is already customised")
    layers.user_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(builtin.dir, dest, symlinks=True, ignore=_ignore_links)
    _atomic_write(dest / SIDECAR, _sidecar_text(builtin.content))
    return dest


def _ignore_links(directory: str, entries: list[str]) -> set[str]:
    return {e for e in entries if os.path.islink(os.path.join(directory, e))}


def write_user_skill(layers: SkillLayers, name: str, content: str) -> Path:
    dest = _user_dir(layers, name)
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        raise InvalidSkill("SKILL.md exceeds 256 KiB")
    meta, _ = parse_front_matter(content)
    if meta.get("name") != name or not meta.get("description"):
        raise InvalidSkill(
            "front matter must contain 'name: <skill name>' and a non-empty 'description'"
        )
    md = dest / "SKILL.md"
    if md.is_symlink():
        raise InvalidSkill(f"user skill {name}: SKILL.md is a symlink")
    dest.mkdir(parents=True, exist_ok=True)
    _atomic_write(md, content)
    return md


def reset(layers: SkillLayers, name: str) -> None:
    """Delete the user copy of a skill (a customised built-in falls back to the original)."""
    d = _user_dir(layers, name)
    if not d.is_dir():
        raise SkillNotFound(f"no user copy of {name}")
    shutil.rmtree(d)


def origin_diff(layers: SkillLayers, name: str) -> str | None:
    """Unified diff original-when-customised → original-now, or None when unchanged."""
    d = _user_dir(layers, name)
    builtin = _load(layers.builtin_root, name)
    data = _read_sidecar(d)
    if builtin is None or data is None or not _origin_updated(layers, name, d):
        return None
    return "".join(
        difflib.unified_diff(
            data["builtinContent"].splitlines(keepends=True),
            builtin.content.splitlines(keepends=True),
            fromfile=f"{name}/SKILL.md (original when customised)",
            tofile=f"{name}/SKILL.md (original now)",
        )
    )


def acknowledge_update(layers: SkillLayers, name: str) -> None:
    d = _user_dir(layers, name)
    builtin = _load(layers.builtin_root, name)
    if builtin is None or _read_sidecar(d) is None:
        raise SkillNotFound(f"{name} is not a customised built-in")
    _atomic_write(d / SIDECAR, _sidecar_text(builtin.content))


def layer_contents(layers: SkillLayers, name: str) -> dict[Origin, str]:
    """The valid SKILL.md text of `name` per layer that has one (most specific first:
    project, user, builtin) - for documents read per layer, e.g. part preferences."""
    validate_skill_name(name)
    order: list[tuple[Origin, Path | None]] = [
        ("project", layers.project_root),
        ("user", layers.user_root),
        ("builtin", layers.builtin_root),
    ]
    return {origin: entry.content for origin, root in order if (entry := _load(root, name))}
