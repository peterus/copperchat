"""Synthetic test repo for spike 3A (library location B):

    repo/hardware/main/main.kicad_{pro,sch,pcb}   (created by KiCad: CreateDocument + save)
    repo/hardware/io/io.kicad_{pro,sch}           (created by KiCad: CreateDocument + save)
    repo/footprints/                              (empty dir; the .pretty is created by the probes)

The project fp-lib-table rows can NOT be written through the API at this KiCad pin
(Add/Update/DeleteLibraryTableEntry and GetLibraryTable have no handler, see q02). The
probes therefore use `standin_fp_lib_table`, a spike-only STAND-IN for the missing API call,
written into this throwaway tmp repo only. It is the single file a probe writes itself.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from kipy.proto.common.types import DocumentType

from harness import Server

LIB = "copperchat"
# ${KIPRJMOD} is hardware/<project>/, the shared library lives at the repo root
LIB_URI = "${KIPRJMOD}/../../footprints/copperchat.pretty"


@dataclass
class Repo:
    root: Path

    @property
    def main_pro(self) -> Path:
        return self.root / "hardware" / "main" / "main.kicad_pro"

    @property
    def io_pro(self) -> Path:
        return self.root / "hardware" / "io" / "io.kicad_pro"

    @property
    def lib_dir(self) -> Path:
        return self.root / "footprints" / f"{LIB}.pretty"


def standin_fp_lib_table(project_dir: Path, rows: list[tuple[str, str]]) -> Path:
    """STAND-IN for AddLibraryTableEntry(scope=project) — the API has no handler for it."""
    lines = ["(fp_lib_table", "\t(version 7)"]
    for name, uri in rows:
        lines.append(f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr ""))')
    lines.append(")")
    path = project_dir / "fp-lib-table"
    path.write_text("\n".join(lines) + "\n")
    return path


def make_repo(base: Path, *, board: bool = True, table: bool = True) -> Repo:
    repo = Repo(base / "repo")
    shutil.rmtree(repo.root, ignore_errors=True)
    for pro in (repo.main_pro, repo.io_pro):
        pro.parent.mkdir(parents=True)
    (repo.root / "footprints").mkdir()
    for pro in (repo.main_pro, repo.io_pro):
        with Server(None) as srv:
            k = srv.client(60000)
            k.create_document(str(pro.with_suffix(".kicad_sch")), DocumentType.DOCTYPE_SCHEMATIC)
            k.get_schematic().save()
    if board:
        with Server(repo.main_pro) as srv:
            k = srv.client(60000)
            k.create_document(str(repo.main_pro.with_suffix(".kicad_pcb")), DocumentType.DOCTYPE_PCB)
            k.get_board().save()
    if table:
        for pro in (repo.main_pro, repo.io_pro):
            standin_fp_lib_table(pro.parent, [(LIB, LIB_URI)])
    for lck in repo.root.rglob("*.lck"):
        lck.unlink()
    return repo
