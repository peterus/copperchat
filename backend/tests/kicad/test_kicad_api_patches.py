"""The KiCad API patches CopperChat carries (docker/kicad-base/patches/): library table rows,
footprint-level properties in a footprint document, deleting a library item, and the
api-server's footprint bookkeeping. Built around the spike 3A acceptance case L201
(Bourns SRN3012BTA: pads 1.3 x 3.5 mm at x = +-1.1 mm, F.CrtYd 4 x 4 mm, F.Fab 3 x 3 mm)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.any_pb2 import Any as PbAny
from google.protobuf.empty_pb2 import Empty
from kipy.errors import ApiError, LibraryCommandError
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common import ApiStatusCode
from kipy.proto.common.commands import (
    editor_commands_pb2 as ec,
)
from kipy.proto.common.commands import (
    library_commands_pb2 as lc,
)
from kipy.proto.common.commands import (
    project_commands_pb2 as pc,
)
from kipy.proto.common.types import DocumentType
from kipy.proto.common.types import base_types_pb2 as base
from kipy.proto.common.types import enums_pb2 as en
from kipy.proto.common.types.library_types_pb2 import (
    LibraryCommandStatus,
    LibraryTableEntry,
    LibraryTableScope,
    LibraryType,
)
from kipy.util.proto import unpack_any

from copperchat.kicad_session import KicadSession

pytestmark = pytest.mark.kicad

FP = LibraryType.LT_FOOTPRINT
PROJECT = LibraryTableScope.LTS_PROJECT
LIB = "copperchat"
LIB_URI = "${KIPRJMOD}/footprints/copperchat.pretty"
SEED = "Inductor_SMD:L_Bourns-SRN4018"
NAME = "L_Bourns_SRN3012BTA"
DESCRIPTION = "Bourns SRN3012BTA SMD power inductor, 3.0x3.0x1.2 mm"
KEYWORDS = "Bourns SRN3012BTA inductor SMD"
SOURCE = "Bourns SRN3012BTA datasheet, recommended land pattern"
NM = 1_000_000
# The first footprint document loads every footprint library in the tables (spike 3A Q5)
TIMEOUT = 300.0


def mm(v: float) -> int:
    return round(v * NM)


def kiid() -> base.KIID:
    return base.KIID(value=str(uuid.uuid4()))


def pack(items: list[Any]) -> list[PbAny]:
    out = []
    for item in items:
        a = PbAny()
        a.Pack(item)
        out.append(a)
    return out


class FootprintDoc:
    """The server's single footprint document, over raw protobuf (kipy's Board wrapper cannot
    handle footprint fields, spike 3A Q6)."""

    def __init__(self, kicad: Any, lib_id: str) -> None:
        self.kicad = kicad
        self.client = kicad._client
        self.doc = kicad.open_document(lib_id, DocumentType.DOCTYPE_FOOTPRINT)

    def close(self) -> None:
        self.kicad.close_document(self.doc)

    def items(self) -> list[Any]:
        cmd = ec.GetItems()
        cmd.header.document.CopyFrom(self.doc)
        return [unpack_any(a) for a in self.client.send(cmd, ec.GetItemsResponse).items]

    def footprint(self) -> bt.FootprintInstance:
        """The footprint itself (GetItems lists only its children)."""
        parent = next(i.parent.value for i in self.items() if i.DESCRIPTOR.name == "Pad")
        cmd = ec.GetItemsById()
        cmd.header.document.CopyFrom(self.doc)
        cmd.items.append(base.KIID(value=parent))
        (item,) = self.client.send(cmd, ec.GetItemsResponse).items
        fp = unpack_any(item)
        assert isinstance(fp, bt.FootprintInstance)
        return fp

    def _check(self, response: Any, results: Any) -> list[Any]:
        bad = [r.status.error_message for r in results if r.status.code != ec.ISC_OK]
        assert response.status == base.IRS_OK and not bad, bad
        return [unpack_any(r.item) for r in results]

    def create(self, items: list[Any]) -> list[Any]:
        cmd = ec.CreateItems()
        cmd.header.document.CopyFrom(self.doc)
        cmd.items.extend(pack(items))
        r = self.client.send(cmd, ec.CreateItemsResponse)
        return self._check(r, r.created_items)

    def update(self, items: list[Any]) -> list[Any]:
        cmd = ec.UpdateItems()
        cmd.header.document.CopyFrom(self.doc)
        cmd.items.extend(pack(items))
        r = self.client.send(cmd, ec.UpdateItemsResponse)
        return self._check(r, r.updated_items)

    def delete(self, ids: list[str]) -> None:
        cmd = ec.DeleteItems()
        cmd.header.document.CopyFrom(self.doc)
        cmd.item_ids.extend(base.KIID(value=i) for i in ids)
        self.client.send(cmd, ec.DeleteItemsResponse)

    def save(self) -> None:
        cmd = pc.SaveDocument()
        cmd.document.CopyFrom(self.doc)
        self.client.send(cmd, Empty)

    def save_copy(self, target: str) -> None:
        cmd = ec.SaveCopyOfDocument()
        cmd.document.CopyFrom(self.doc)
        cmd.path = target
        self.client.send(cmd, Empty)


def pad(template: bt.Pad, number: str, x: float) -> bt.Pad:
    p = bt.Pad()
    p.CopyFrom(template)
    p.id.CopyFrom(kiid())
    p.number = number
    p.position.x_nm, p.position.y_nm = mm(x), 0
    del p.pad_stack.copper_layers[1:]
    cl = p.pad_stack.copper_layers[0]
    cl.shape = bt.PadStackShape.PSS_RECTANGLE
    cl.size.x_nm, cl.size.y_nm = mm(1.3), mm(3.5)
    return p


def rect(layer: int, w: float, h: float, stroke: float) -> bt.BoardGraphicShape:
    s = bt.BoardGraphicShape()
    s.id.CopyFrom(kiid())
    s.layer = layer
    s.shape.attributes.stroke.width.value_nm = mm(stroke)
    s.shape.attributes.stroke.style = en.StrokeLineStyle.SLS_SOLID
    s.shape.rectangle.top_left.x_nm, s.shape.rectangle.top_left.y_nm = mm(-w / 2), mm(-h / 2)
    s.shape.rectangle.bottom_right.x_nm, s.shape.rectangle.bottom_right.y_nm = mm(w / 2), mm(h / 2)
    return s


def custom_field(template: bt.Field, name: str, value: str) -> bt.Field:
    f = bt.Field()
    f.CopyFrom(template)
    f.ClearField("id")
    f.name = name
    f.text.id.CopyFrom(kiid())
    f.text.text.text = value
    f.visible = False
    return f


def rows(kicad: Any) -> dict[str, Any]:
    return {r.nickname: r for r in kicad.get_library_table(FP, PROJECT).rows}


def library_items(kicad: Any) -> list[str]:
    cmd = lc.GetLibraryItems()
    cmd.type = FP
    cmd.nickname.append(LIB)
    return [i.entry_name for i in kicad._client.send(cmd, lc.LibraryItemsResponse).items]


def build_l201(kicad: Any) -> None:
    """Seed copy -> L201 geometry, fields and footprint-level properties, in one commit."""
    seed = FootprintDoc(kicad, SEED)
    seed.save_copy(f"{LIB}:{NAME}")
    seed.close()

    fp = FootprintDoc(kicad, f"{LIB}:{NAME}")
    items = fp.items()
    template_pad = next(i for i in items if i.DESCRIPTOR.name == "Pad")
    template_field = next(i for i in items if i.DESCRIPTOR.name == "Field")
    commit = fp.kicad._client.send(
        ec.BeginCommit(header=base.ItemHeader(document=fp.doc)), ec.BeginCommitResponse
    )
    fp.delete([i.id.value for i in items if i.DESCRIPTOR.name != "Field"])
    fp.create(
        [
            pad(template_pad, "1", -1.1),
            pad(template_pad, "2", 1.1),
            rect(bt.BoardLayer.BL_F_CrtYd, 4.0, 4.0, 0.05),
            rect(bt.BoardLayer.BL_F_Fab, 3.0, 3.0, 0.1),
            custom_field(template_field, "Footprint source", SOURCE),
        ]
    )

    # G2: the footprint's own properties (refused before the patch: "Tried to create a
    # footprint in Footprint, which is not a board")
    footprint = fp.footprint()
    footprint.definition.attributes.description = DESCRIPTION
    footprint.definition.attributes.keywords = KEYWORDS
    footprint.attributes.mounting_style = bt.FootprintMountingStyle.FMS_SMD
    footprint.attributes.exclude_from_bill_of_materials = True
    footprint.overrides.copper_clearance.value_nm = mm(0.2)
    # Drop the seed's 3D model: the copy must not claim SRN4018's body
    kept = [a for a in footprint.definition.items if not a.type_url.endswith("Footprint3DModel")]
    del footprint.definition.items[:]
    footprint.definition.items.extend(kept)
    (updated,) = fp.update([footprint])
    assert updated.definition.attributes.description == DESCRIPTION

    fp.kicad._client.send(
        ec.EndCommit(id=commit.id, action=ec.CMA_COMMIT, header=base.ItemHeader(document=fp.doc)),
        ec.EndCommitResponse,
    )
    fp.save()
    fp.close()


def read_back(kicad: Any) -> dict[str, Any]:
    fp = FootprintDoc(kicad, f"{LIB}:{NAME}")
    try:
        items = fp.items()
        footprint = fp.footprint()
    finally:
        fp.close()
    pads = sorted(
        (
            p.number,
            p.position.x_nm / NM,
            p.pad_stack.copper_layers[0].size.x_nm / NM,
            p.pad_stack.copper_layers[0].size.y_nm / NM,
        )
        for p in items
        if p.DESCRIPTOR.name == "Pad"
    )
    rects = {}
    for s in items:
        if (
            s.DESCRIPTOR.name == "BoardGraphicShape"
            and s.shape.WhichOneof("geometry") == "rectangle"
        ):
            r = s.shape.rectangle
            rects[bt.BoardLayer.Name(s.layer)] = (
                (r.bottom_right.x_nm - r.top_left.x_nm) / NM,
                (r.bottom_right.y_nm - r.top_left.y_nm) / NM,
            )
    fields = {f.name: f.text.text.text for f in items if f.DESCRIPTOR.name == "Field"}
    return {"pads": pads, "rects": rects, "fields": fields, "footprint": footprint}


def l201_cycle(kicad: Any, project_dir: Path) -> None:
    # G1: a project table row plus an empty library, both written by KiCad
    assert LIB not in rows(kicad)
    kicad.add_library_table_entry(
        FP, PROJECT, LIB, LIB_URI, description="CopperChat footprints", create_library=True
    )
    row = rows(kicad)[LIB]
    assert (row.uri, row.description, row.scope) == (LIB_URI, "CopperChat footprints", PROJECT)
    assert (project_dir / "footprints" / "copperchat.pretty").is_dir()
    table_file = (project_dir / "fp-lib-table").read_text()
    assert f'(name "{LIB}")' in table_file and "copperchat.pretty" in table_file
    response = kicad.get_library_table(FP, PROJECT)
    assert response.table.valid and Path(response.table.path) == project_dir / "fp-lib-table"
    substituted = kicad.get_library_table(FP, PROJECT, substituted=True).rows
    assert (
        Path(next(r.uri for r in substituted if r.nickname == LIB)).resolve()
        == (project_dir / "footprints" / "copperchat.pretty").resolve()
    )
    with pytest.raises(LibraryCommandError) as dup:
        kicad.add_library_table_entry(FP, PROJECT, LIB, LIB_URI)
    assert dup.value.code == LibraryCommandStatus.Code.LCS_ALREADY_EXISTS

    # G2: L201 with its own description, keywords and attributes
    build_l201(kicad)
    back = read_back(kicad)
    assert back["pads"] == [("1", -1.1, 1.3, 3.5), ("2", 1.1, 1.3, 3.5)]
    assert back["rects"] == {"BL_F_CrtYd": (4.0, 4.0), "BL_F_Fab": (3.0, 3.0)}
    assert back["fields"]["Footprint source"] == SOURCE
    footprint = back["footprint"]
    assert footprint.definition.attributes.description == DESCRIPTION
    assert footprint.definition.attributes.keywords == KEYWORDS
    assert footprint.attributes.mounting_style == bt.FootprintMountingStyle.FMS_SMD
    assert footprint.attributes.exclude_from_bill_of_materials
    assert footprint.overrides.copper_clearance.value_nm == mm(0.2)
    assert not [a for a in footprint.definition.items if a.type_url.endswith("Footprint3DModel")]
    text = (project_dir / "footprints" / "copperchat.pretty" / f"{NAME}.kicad_mod").read_text()
    assert f'(descr "{DESCRIPTION}")' in text and f'(tags "{KEYWORDS}")' in text
    assert "(attr smd exclude_from_bom)" in text and "(model " not in text and "SRN4018" not in text

    # G3: delete the footprint from the library. The table change first drops the loaded
    # library, so the delete has to load it itself.
    assert NAME in library_items(kicad)
    kicad.update_library_table_entry(FP, PROJECT, LIB, LIB_URI, description="changed")
    kicad.delete_library_item(FP, LIB, NAME)
    assert NAME not in library_items(kicad)
    assert not (project_dir / "footprints" / "copperchat.pretty" / f"{NAME}.kicad_mod").exists()
    with pytest.raises(LibraryCommandError) as gone:
        kicad.delete_library_item(FP, LIB, NAME)
    assert gone.value.code == LibraryCommandStatus.Code.LCS_NOT_FOUND

    # G1: update and delete the row
    kicad.update_library_table_entry(
        FP, PROJECT, LIB, LIB_URI, description="renamed", disabled=True
    )
    row = rows(kicad)[LIB]
    assert (row.description, row.disabled) == ("renamed", True)
    kicad.delete_library_table_entry(FP, PROJECT, LIB)
    assert LIB not in rows(kicad)
    assert LIB not in (project_dir / "fp-lib-table").read_text()
    with pytest.raises(LibraryCommandError) as missing:
        kicad.delete_library_table_entry(FP, PROJECT, LIB)
    assert missing.value.code == LibraryCommandStatus.Code.LCS_NOT_FOUND


async def test_l201_cycle_schematic_only(blank_nopcb_session: KicadSession) -> None:
    """No board open: the footprint kiface (and its library adapter) is loaded on demand."""
    s = blank_nopcb_session
    await s.call(lambda h: l201_cycle(h.kicad, s.project.dir), timeout=TIMEOUT)


def reopen_without_close(kicad: Any) -> None:
    kicad.open_document(SEED, DocumentType.DOCTYPE_FOOTPRINT)
    second = kicad.open_document("Inductor_SMD:L_Bourns-SRN6028", DocumentType.DOCTYPE_FOOTPRINT)
    open_fps = [
        d for d in kicad.get_open_documents(DocumentType.DOCTYPE_FOOTPRINT) if d.lib_id.entry_name
    ]
    assert [d.lib_id.entry_name for d in open_fps] == ["L_Bourns-SRN6028"]
    kicad.close_document(second)  # failed before: "Requested document does not match"


def bad_requests(kicad: Any, project_dir: Path) -> None:
    for cmd in (
        lc.AddLibraryTableEntry(
            entry=LibraryTableEntry(type=FP, nickname="x", uri="/tmp/x.pretty"),
            scope=LibraryTableScope.LTS_BOTH,
        ),
        lc.AddLibraryTableEntry(entry=LibraryTableEntry(type=FP, nickname="x"), scope=PROJECT),
        lc.AddLibraryTableEntry(
            entry=LibraryTableEntry(type=FP, nickname="a:b", uri="/tmp/x.pretty"), scope=PROJECT
        ),
        lc.DeleteLibraryTableEntry(type=LibraryType.LT_UNKNOWN, nickname="x", scope=PROJECT),
    ):
        with pytest.raises(ApiError) as bad:
            kicad._client.send(cmd, LibraryCommandStatus)
        assert bad.value.code == ApiStatusCode.AS_BAD_REQUEST
    assert "x" not in rows(kicad)

    # A library that cannot be created leaves no row behind
    (project_dir / "blocker").write_text("a file, so no directory can be created below it")
    with pytest.raises(LibraryCommandError) as failed:
        kicad.add_library_table_entry(
            FP, PROJECT, "x", "${KIPRJMOD}/blocker/x.pretty", create_library=True
        )
    assert failed.value.code == LibraryCommandStatus.Code.LCS_ERROR
    assert "x" not in rows(kicad)


async def test_l201_cycle_with_board(blank_session: KicadSession) -> None:
    s = blank_session
    await s.call(lambda h: l201_cycle(h.kicad, s.project.dir), timeout=TIMEOUT)
    await s.call(lambda h: reopen_without_close(h.kicad), timeout=TIMEOUT)
    await s.call(lambda h: bad_requests(h.kicad, s.project.dir), timeout=TIMEOUT)
    # the schematic and the board stay usable
    assert await s.call(lambda h: len(h.board().get_footprints())) == 0
    assert await s.call(lambda h: len(h.schematic().get_symbols())) == 0
