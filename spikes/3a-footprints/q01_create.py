"""Q1 (+ acceptance case L201): create a NEW footprint purely via the API.

Path: open a stock seed (DOCTYPE_FOOTPRINT) -> SaveCopyOfDocument("copperchat:<new>") ->
open the copy -> delete its items -> create pads/graphics/texts/fields -> SaveDocument.
Then: read back, footprint-level attributes, kicad-cli fp upgrade round trip.
"""

import shutil
import tempfile
import time
from pathlib import Path

from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import DocumentType

import l201
from fpdoc import FootprintDoc, Timing, kind
from harness import Recorder, Server, cli, main_guard
from repo import LIB, make_repo

SEED = "Inductor_SMD:L_Bourns-SRN4018"


def build(fp: FootprintDoc, r: Recorder) -> None:
    items = fp.items()
    pad_tpl = next(i for i in items if kind(i) == "Pad")
    fields = {i.name: i for i in items if kind(i) == "Field"}
    r.note("seed_items", sorted({kind(i) for i in items}))
    deletable = [i.id.value for i in items if kind(i) != "Field"]
    deletable += [f.text.id.value for f in fields.values() if f.name not in ("Reference", "Value", "Datasheet", "Description")]
    cid = fp.begin()
    res = fp.delete(deletable)
    r.check("delete_seed_items", all(v == "IDS_OK" for v in res.values()), f"{len(res)} deleted: {set(res.values())}")
    # mandatory fields refuse deletion (IDS_IMMUTABLE) -> they are updated instead
    res_m = fp.delete([fields["Reference"].text.id.value])
    r.check("mandatory_field_delete_refused", set(res_m.values()) == {"IDS_IMMUTABLE"}, res_m)

    created = fp.create(
        [l201.pad(pad_tpl, "1", -l201.PAD_X), l201.pad(pad_tpl, "2", l201.PAD_X),
         l201.rect(bt.BoardLayer.BL_F_CrtYd, *l201.CRTYD, 0.05),
         l201.rect(bt.BoardLayer.BL_F_Fab, *l201.FAB, 0.1),
         *l201.silk_items(), l201.fab_ref_text(),
         l201.custom_field("Footprint source", l201.SOURCE, fields["Datasheet"])]
    )
    r.check("create_items", len(created) == 9, [kind(c) for c in created])

    ref, val, desc = fields["Reference"], fields["Value"], fields["Description"]
    ref.text.text.position.x_nm, ref.text.text.position.y_nm = 0, l201.mm(-2.8)
    val.text.text.text = l201.NAME
    val.text.text.position.x_nm, val.text.text.position.y_nm = 0, l201.mm(2.8)
    desc.text.text.text = l201.DESCRIPTION
    upd = fp.update([ref, val, desc])
    r.check("update_mandatory_fields", len(upd) == 3, [u.text.text.text for u in upd])
    fp.end(cid, message="create L201")


def attributes_probe(fp: FootprintDoc, r: Recorder) -> None:
    """Footprint-level data (descr, tags, attr, 3D models) lives on the FOOTPRINT, not on items."""
    parent = next(i.parent.value for i in fp.items() if kind(i) == "Pad")
    inst = fp.items_by_id([parent])
    r.check("footprint_readable_by_parent_id", len(inst) == 1 and kind(inst[0]) == "FootprintInstance",
            [kind(i) for i in inst])
    f = inst[0]
    r.note("footprint_attributes_before", {"description": f.definition.attributes.description or f.attributes.description,
                                           "keywords": f.attributes.keywords,
                                           "models": [i.type_url for i in f.definition.items if "3DModel" in i.type_url]})
    f.attributes.description = l201.DESCRIPTION
    f.attributes.keywords = l201.KEYWORDS
    try:
        fp.update([f])
        r.check("update_footprint_attributes", True, "accepted")
    except RuntimeError as e:
        r.check("update_footprint_attributes_refused", "not a board" in str(e), str(e))
    m = bt.Footprint3DModel(filename="${KICAD10_3DMODEL_DIR}/Inductor_SMD.3dshapes/L_Bourns-SRN4018.step", visible=True)
    try:
        fp.create([m])
        r.check("create_3d_model_item", True, "accepted")
    except RuntimeError as e:
        r.check("create_3d_model_item_refused", "ISC_INVALID_TYPE" in str(e), str(e))


def main() -> int:
    r = Recorder("q01_create")
    base = Path(tempfile.mkdtemp(prefix="c3a-q1-"))
    repo = make_repo(base)
    repo.lib_dir.mkdir()  # Q2: the one non-API step (empty dir)
    t = Timing()
    with Server(repo.main_pro) as srv:
        k = srv.client(60000)
        k.open_document(str(repo.main_pro.with_suffix(".kicad_sch")), DocumentType.DOCTYPE_SCHEMATIC)
        t0 = time.monotonic()
        seed = FootprintDoc(k, SEED, t)
        seed.save_copy(f"{LIB}:{l201.NAME}")
        r.check("save_copy_creates_file", (repo.lib_dir / f"{l201.NAME}.kicad_mod").exists(),
                sorted(p.name for p in repo.lib_dir.iterdir()))
        seed.close()  # the server keeps one footprint document; see q04 for what happens otherwise
        fp = FootprintDoc(k, f"{LIB}:{l201.NAME}", t)
        r.check("open_copy_without_reload", fp.doc.lib_id.entry_name == l201.NAME, fp.doc.lib_id)
        build(fp, r)
        r.note("modified_after_commit", fp.modified())
        attributes_probe(fp, r)
        fp.save()
        r.note("create_wall_s", round(time.monotonic() - t0, 3))
        r.note("modified_after_save", fp.modified())

        # read back from disk: close + reopen (fresh load through the library adapter)
        fp.close()
        fp2 = FootprintDoc(k, f"{LIB}:{l201.NAME}", t)
        g = l201.geometry(fp2.items())
        r.note("readback", g)
        pads = g["pads"]
        r.check("pads", [(p["number"], p["type"], p["shape"], p["pos"], p["size"]) for p in pads]
                == [("1", "PT_SMD", "PSS_RECTANGLE", (-1.1, 0.0), (1.3, 3.5)),
                    ("2", "PT_SMD", "PSS_RECTANGLE", (1.1, 0.0), (1.3, 3.5))], pads)
        r.check("pad_layers", all(p["layers"] == ["BL_F_Cu", "BL_F_Mask", "BL_F_Paste"] for p in pads),
                [p["layers"] for p in pads])
        rects = {s["layer"]: s for s in g["shapes"] if s["geometry"] == "rectangle"}
        r.check("courtyard_4x4", rects.get("BL_F_CrtYd", {}).get("size") == (4.0, 4.0), rects.get("BL_F_CrtYd"))
        r.check("fab_3x3", rects.get("BL_F_Fab", {}).get("size") == (3.0, 3.0), rects.get("BL_F_Fab"))
        silk = [s for s in g["shapes"] if s["layer"] == "BL_F_SilkS"]
        r.check("silk_outside_pads", len(silk) == 3 and all(
            max(abs(s["start"][0]), abs(s["end"][0])) + s["stroke"] / 2 < 0.45
            or min(abs(s["start"][0]), abs(s["end"][0])) - s["stroke"] / 2 > 1.75 for s in silk), silk)
        f = g["fields"]
        r.check("fields", f["Value"]["text"] == l201.NAME and f["Description"]["text"] == l201.DESCRIPTION
                and f.get("Footprint source", {}).get("text") == l201.SOURCE, f)
        r.check("pin1_left", pads[0]["number"] == "1" and pads[0]["pos"][0] < 0, pads[0])
        fp2.close()
        r.note("timing_s", {n: [round(x, 4) for x in v] for n, v in t.calls.items()})
        r.check("server_alive", srv.alive(), srv.log()[-300:])

    mod = repo.lib_dir / f"{l201.NAME}.kicad_mod"
    text = mod.read_text()
    head = [ln.strip() for ln in text.splitlines()[:8]]
    r.note("file_head", head)
    r.note("file_descr_tags_model", [ln.strip() for ln in text.splitlines()
                                     if ln.strip().startswith(("(descr", "(tags", "(model", "(attr"))])
    r.check("file_version", any(ln.startswith("(version ") for ln in head), head)
    # G2: the Description FIELD was updated, the footprint-level (descr) is still the seed's
    descr = next(ln.strip() for ln in text.splitlines() if ln.strip().startswith("(descr "))
    r.check("file_descr_is_seeds_not_l201", "SRN4018" in descr and l201.DESCRIPTION not in descr, descr)
    r.check("file_description_field_is_l201", f'(property "Description" "{l201.DESCRIPTION}"' in text, "")

    # kicad-cli round trip: upgrade --force into a copy must not change the content
    out = base / "upgraded"
    p = cli("fp", "upgrade", "--force", str(repo.lib_dir), "-o", str(out))
    r.note("fp_upgrade", (p.returncode, p.stdout[-300:], p.stderr[-300:]))
    up = out / mod.name
    same = up.exists() and up.read_text() == text
    if up.exists() and not same:
        import difflib
        r.note("upgrade_diff", list(difflib.unified_diff(text.splitlines(), up.read_text().splitlines(), lineterm="", n=0))[:40])
    r.check("fp_upgrade_roundtrip_identical", p.returncode == 0 and same, f"rc={p.returncode} identical={same}")
    shutil.copytree(repo.lib_dir, Path(__file__).parent / "results" / "copperchat.pretty", dirs_exist_ok=True)
    return r.finish()


if __name__ == "__main__":
    main_guard(main)
