"""Project-relative locations used by the part-researcher, the Parts page and uploads."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from copperchat.projects import Project, agent_dir


def parts_root(project: Project) -> Path:
    return project.dir / "docs" / "parts"


def uploads_pdf_root(project: Project) -> Path:
    return parts_root(project) / "_uploads"


def uploads_image_root(project: Project) -> Path:
    return agent_dir(project) / "uploads"


def resolve_inside(raw: str, base: Path, roots: Sequence[Path]) -> Path | None:
    """Resolve ``raw`` (absolute or relative to ``base``) and return it if it lies inside
    one of ``roots``.

    Rejects empty paths, NUL bytes and any ``..`` component before resolving; resolves
    symlinks of both the candidate and the roots, and requires every root to resolve
    inside ``base`` (a ``docs/parts`` symlinked elsewhere is never a valid root).
    """
    if not raw or "\x00" in raw:
        return None
    candidate = Path(raw)
    if ".." in candidate.parts:
        return None
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        real = candidate.resolve(strict=False)
        real_base = base.resolve(strict=False)
    except (OSError, RuntimeError):  # symlink loop
        return None
    for root in roots:
        try:
            real_root = root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if not real_root.is_relative_to(real_base):
            continue
        if real == real_root or real.is_relative_to(real_root):
            # A resolved path still containing a symlink is an unresolvable link (loop).
            node = real
            while node != real_root:
                if node.is_symlink():
                    return None
                node = node.parent
            return real
    return None
