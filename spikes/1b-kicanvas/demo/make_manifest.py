"""Print projects.json for the demo picker; fail if a file was not upgraded by kicad-cli."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# KiCad 11 nightly formats (see docs/spikes/1b-kicanvas.md, Setup).
MIN_VERSION = {".kicad_sch": 20260830, ".kicad_pcb": 20260901}


def check_header(path: Path) -> None:
    head = path.read_text(encoding="utf-8", errors="replace")[:400]
    m = re.search(r"\(version (\d+)\)", head)
    if not m:
        sys.exit(f"FAIL: {path} has no (version …) header")
    version = int(m.group(1))
    gen = re.search(r'\(generator_version "([^"]+)"\)', head)
    if version < MIN_VERSION[path.suffix] or not gen or not gen.group(1).startswith(("10.99", "11")):
        sys.exit(f"FAIL: {path} not upgraded (version {version}, generator {gen and gen.group(1)})")


def sheet_closure(root_sch: Path) -> list[Path]:
    """Root schematic plus every sub-sheet file it references, transitively."""
    seen, todo = [root_sch], [root_sch]
    while todo:
        text = todo.pop().read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r'\(property "Sheetfile" "([^"]+)"', text):
            p = root_sch.parent / name
            if p.exists() and p not in seen:
                seen.append(p)
                todo.append(p)
    return seen


def main(root: Path) -> None:
    projects = []
    for pro in sorted(root.glob("*/*.kicad_pro")):
        d = pro.parent
        root_sch = d / f"{pro.stem}.kicad_sch"
        sch = sheet_closure(root_sch) if root_sch.exists() else []
        pcb = sorted(d.glob("*.kicad_pcb"), key=lambda p: (p.stem != pro.stem, p.name))
        for f in [*sch, *pcb]:
            check_header(f)
        rel = lambda p: p.relative_to(root.parent).as_posix()  # noqa: E731
        projects.append({
            "id": f"{d.name}/{pro.stem}",
            "name": pro.stem,
            "schematics": [rel(p) for p in sch],
            # Only the board named like the project (e.g. not "-unrouted" variants).
            "board": rel(pcb[0]) if pcb and pcb[0].stem == pro.stem else None,
            "bytes": {rel(p): p.stat().st_size for p in [*sch, *pcb[:1]]},
        })
    json.dump(projects, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
