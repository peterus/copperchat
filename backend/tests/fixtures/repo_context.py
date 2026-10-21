"""A small project repository for the repo-context tests (spec §9 "Fixture repo").

Built from files into a temporary directory by the test (``git init`` + one commit); no
``.git`` is committed to this repository.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from copperchat.projects import Project

ENCLOSURE_SCAD = """// Outer dimensions of the enclosure in millimetres.
width = 80;
depth = 50;
height = 25;
cube([width, depth, height]);
"""

FILES: dict[str, str] = {
    "README.md": "# Widget\n\nA small sensor board in a 3D-printed enclosure.\n",
    "docs/spec.md": "# Spec\n\nThe board fits the enclosure described in mechanics/.\n",
    "mechanics/enclosure.scad": ENCLOSURE_SCAD,
    ".env": "API_TOKEN=do-not-read\n",
    "hw/pcb/x.kicad_sch": "(kicad_sch (version 20231120))\n",
}
BINARY_STL = b"solid case\x00\x00\x00\x80\x3f\x00\x00\x80\x3f\xff\xfe" * 32


@dataclass(frozen=True)
class FixtureRepo:
    projects_root: Path
    root: Path
    project: Project


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def build_fixture_repo(base: Path) -> FixtureRepo:
    """Create ``<base>/projects/widget`` (a git repo) with a KiCad project in ``hw/pcb``."""
    projects_root = base / "projects"
    root = projects_root / "widget"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "mechanics" / "case.stl").write_bytes(BINARY_STL)
    pro = root / "hw" / "pcb" / "x.kicad_pro"
    pro.write_text("{}\n")
    outside = base / "outside-secret.txt"
    outside.write_text("TOP-SECRET-OUTSIDE\n")
    (root / "docs" / "outside.txt").symlink_to(outside)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    project = Project(
        id="widget000000",
        name="x",
        rel_path="widget/hw/pcb/x.kicad_pro",
        dir=pro.parent,
        pro_file=pro,
        sch_file=pro.parent / "x.kicad_sch",
        pcb_file=None,
    )
    return FixtureRepo(projects_root=projects_root, root=root, project=project)
