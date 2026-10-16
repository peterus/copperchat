"""Q2: kipy connects over ipc://; which kipy revision imports and works."""

import importlib
import subprocess
from pathlib import Path

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad


def kipy_revision() -> str:
    out = subprocess.run(["uv", "pip", "show", "kicad-python"], capture_output=True, text=True)
    src = Path(__file__).parent / ".kicad/kicad-python"
    revs = []
    for d in (src, src / "kicad"):
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True,
                           text=True)
        revs.append(f"{d.name}@{r.stdout.strip() or r.stderr.strip()}")
    return out.stdout + "\n" + " ".join(revs)


def main() -> int:
    require_kicad()
    rec = Recorder("q02_connect")
    for mod in ("kipy", "kipy.schematic", "kipy.board", "kipy.board_jobs", "kipy.errors"):
        try:
            importlib.import_module(mod)
            rec.check(f"import_{mod}", True, "ok")
        except Exception as e:  # noqa: BLE001
            rec.check(f"import_{mod}", False, f"{type(e).__name__}: {e}")
    rec.note("kipy_pip_show", kipy_revision())

    from kipy.proto.common.types import DocumentType
    from kipy.schematic import Schematic

    # Preloading a .kicad_pro opens only the project, not its schematic/board.
    with Server(fresh_fixture("ecc83"), open_docs=False) as srv:
        k = srv.client()
        try:
            docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
            rec.note("preload_opens_sch", f"yes: {docs}")
        except Exception as e:  # noqa: BLE001
            rec.note("preload_opens_sch", f"no: {type(e).__name__}: {e}")

    proj = fresh_fixture("ecc83")
    with Server(proj) as srv:
        rec.note("open_document_s", srv.open_s)
        rec.note("socket_url", srv.url)
        k = srv.client()
        k.ping()
        rec.check("ping", True, srv.url)
        ver = k.get_version()
        rec.check("version", ver.major >= 10, ver.full_version)
        sch_docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        pcb_docs = k.get_open_documents(DocumentType.DOCTYPE_PCB)
        rec.check("sch_open", len(sch_docs) >= 1, sch_docs)
        rec.check("pcb_open", len(pcb_docs) >= 1, pcb_docs)
        sch = Schematic(k._client, sch_docs[0])
        rec.check(
            "schematic_accessor", len(sch.get_symbols()) > 0,
            "Schematic(kicad._client, kicad.get_open_documents(DOCTYPE_SCHEMATIC)[0])",
        )
        if hasattr(k, "get_schematic"):
            rec.check("public_get_schematic", len(k.get_schematic().get_symbols()) > 0,
                      "kicad.get_schematic() (clears sheet_path -> all sheets)")
        board = k.get_board()
        rec.check("board_accessor", len(board.get_footprints()) > 0, "kicad.get_board()")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
