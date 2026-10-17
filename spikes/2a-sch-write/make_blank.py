"""Creates fixtures/blank: an empty project + schematic made by KiCad itself
(headless CreateDocument + SaveDocument), plus a byte copy of the ecc83 demo's project
symbol library and its sym-lib-table (${KIPRJMOD}) as the "project library"."""

import shutil
import tempfile
from pathlib import Path

from harness import FIXTURES, Server

from kipy.proto.common.types import DocumentType


def main() -> None:
    dst = FIXTURES / "blank"
    shutil.rmtree(dst, ignore_errors=True)
    work = Path(tempfile.mkdtemp(prefix="s2a-blank-")) / "blank"
    work.mkdir(parents=True)
    with Server(None) as srv:
        k = srv.client(timeout_ms=60000)
        k.create_document(str(work / "blank.kicad_sch"), DocumentType.DOCTYPE_SCHEMATIC)
        k.get_schematic().save()
    for f in ("ecc83-pp.kicad_sym", "sym-lib-table"):
        shutil.copyfile(FIXTURES / "ecc83" / f, work / f)
    shutil.copytree(work, dst, ignore=shutil.ignore_patterns("*.lck"))
    print("blank:", sorted(p.name for p in dst.iterdir()))


if __name__ == "__main__":
    main()
