from harness import *
import tempfile, hashlib
from pathlib import Path
from kipy.proto.common.types import DocumentType
from kipy.proto.schematic.schematic_jobs_pb2 import SJSM_SINGLE_SHEET
proj = fresh_fixture("blank"); tmp = Path(tempfile.mkdtemp())
def png(sch, tag):
    d = tmp / tag; d.mkdir(); sch.for_sheet(list(sch.get_hierarchy())[0]).export_png(str(d / "x.png"), dpi=100, sheet_mode=SJSM_SINGLE_SHEET)
    f = next(d.rglob("*.png")); print(tag, f.stat().st_size, hashlib.md5(f.read_bytes()).hexdigest()[:8])
def job(sch, tag):
    out = tmp / f"{tag}.net"; sch.export_netlist(str(out)); print(tag, "netlist", sorted(netlist_components(out.read_text()).keys()))
with Server(proj) as srv:
    k = srv.client(timeout_ms=60000); sch = k.get_schematic(); load_libraries(k)
    png(sch, "0_empty"); job(sch, "0")
    sch.place_symbol_from_library("Device:R", v2(50.8, 50.8), reference="R1")
    sch.save()
    doc = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)[0]
    t0 = time.monotonic(); k.close_document(doc); k.open_document(str(root_sch(proj)), DocumentType.DOCTYPE_SCHEMATIC); print("close+open s", round(time.monotonic()-t0,3))
    sch = k.get_schematic(); png(sch, "1_after_reopen"); job(sch, "1")
    print([s.reference for s in sch.get_symbols()])
