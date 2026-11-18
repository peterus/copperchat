"""`FootprintGeometry` from recorded KiCad API output, canonical hash, equality and
review state (FP-C3, spec §7.1, §9.4).

The fixtures in `tests/fixtures/footprints/` are written by
`backend/scripts/record-fp-fixtures.py` (KiCad api-server output, never hand-made):
`<name>.pb` is a `GetItemsFromLibrary` response (one `Footprint`), `<name>.instance.pb`
a footprint-document `GetItemsById` response (one `FootprintInstance`).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.common.types import base_types_pb2 as base_types
from kipy.util.proto import unpack_any

from copperchat.kicad_tools.footprints.model import (
    APPROVAL_FIELD,
    RESERVED_FIELDS,
    REVIEW_FIELD,
    SOURCE_FIELD,
    FootprintGeometry,
    Model3D,
    Shape,
    Text,
    geometry_equal,
    geometry_from_proto,
    geometry_hash,
    review_state,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "footprints"
SEED = "Inductor_SMD:L_Bourns-SRN4018"
PINHEADER = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
QFN = "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm"
JUMPER = "Jumper:SolderJumper-2_P1.3mm_Open_TrianglePad1.0x1.5mm"


def _message(name: str) -> Any:
    response = ec.GetItemsResponse()
    response.ParseFromString((FIXTURES / f"{name}.pb").read_bytes())
    assert len(response.items) == 1
    return unpack_any(response.items[0])


def instance(name: str, lib_id: str, scope: str = "global") -> FootprintGeometry:
    msg = _message(f"{name}.instance")
    return geometry_from_proto(msg, msg.definition.items, lib_id, scope)


def library(name: str, lib_id: str, scope: str = "global") -> FootprintGeometry:
    msg = _message(name)
    return geometry_from_proto(msg, msg.items, lib_id, scope)


@pytest.fixture
def seed() -> FootprintGeometry:
    return instance("seed_srn4018", SEED)


def _pad(g: FootprintGeometry, number: str) -> Any:
    return next(p for p in g.pads if p.number == number)


def _replace_pad(g: FootprintGeometry, number: str, **changes: Any) -> FootprintGeometry:
    pads = tuple(dataclasses.replace(p, **changes) if p.number == number else p for p in g.pads)
    return dataclasses.replace(g, pads=pads)


# -- protobuf -> geometry ----------------------------------------------------------------


def test_seed_geometry(seed: FootprintGeometry) -> None:
    assert seed.lib_id == SEED and seed.scope == "global"
    assert seed.mounting == "smd"
    assert seed.description.startswith("Bourns SRN4018 series SMD inductor")
    assert seed.keywords == "Bourns SRN4018 SMD inductor"
    assert len(seed.pads) == 2
    for pad in seed.pads:
        assert pad.type == "smd" and pad.shape == "rect"
        assert pad.layers == ("F.Cu", "F.Mask", "F.Paste")
        assert pad.size == (1.5, 3.6) and pad.drill is None and pad.roundrect_ratio is None
    assert sorted(p.at for p in seed.pads) == [(-1.525, 0.0), (1.525, 0.0)]
    summary = seed.layer_summary()
    assert summary["F.CrtYd"]["closed"] is True
    assert summary["F.CrtYd"]["items"] == 4
    assert summary["F.CrtYd"]["bbox"] == [-2.53, -2.25, 2.53, 2.25]
    assert summary["F.SilkS"]["closed"] is False
    assert seed.bbox() == (-2.53, -2.25, 2.53, 2.25)
    assert seed.fields["Reference"] == "REF**"
    assert seed.fields["Value"] == "L_Bourns-SRN4018"
    kinds = {t.kind: t for t in seed.texts}
    assert kinds["reference"].layer == "F.SilkS" and kinds["reference"].at == (0.0, 3.1)
    assert kinds["value"].layer == "F.Fab"
    assert kinds["user"].value == "${REFERENCE}"
    (model,) = seed.models
    assert model.path.endswith("Inductor_SMD.3dshapes/L_Bourns-SRN4018.step")
    assert model.scale == (1.0, 1.0, 1.0) and model.offset == (0.0, 0.0, 0.0)


def test_library_read_matches_document_read_except_instance_data() -> None:
    """`GetItemsFromLibrary` packs only `SerializeDefinition`: no mandatory fields, no
    attributes, no overrides. Pads, shapes, user texts and models are the same."""
    lib = library("seed_srn4018", SEED)
    doc = instance("seed_srn4018", SEED)
    assert lib.mounting == "unspecified"
    assert "Reference" not in lib.fields
    assert {t.kind for t in lib.texts} == {"user"}
    assert sorted(lib.pads, key=lambda p: p.number) == sorted(doc.pads, key=lambda p: p.number)
    assert sorted(lib.shapes, key=repr) == sorted(doc.shapes, key=repr)
    assert lib.models == doc.models


def test_tht_pinheader() -> None:
    g = instance("pinheader_1x02", PINHEADER)
    assert g.mounting == "tht"
    assert [p.number for p in sorted(g.pads, key=lambda p: p.number)] == ["1", "2"]
    for pad in g.pads:
        assert pad.type == "tht"
        assert pad.drill is not None
        assert pad.drill.size == (1.0, 1.0) and pad.drill.shape == "circle"
        assert pad.layers == ("*.Cu", "F.Mask", "B.Mask")
    assert {_pad(g, "1").shape, _pad(g, "2").shape} == {"rect", "circle"}
    assert _pad(g, "2").at == (0.0, 2.54)
    assert g.fields["KiLib_Generator"] == "connector/pin_header_socket"


def test_qfn_roundrect_and_exposed_pad() -> None:
    g = instance("qfn16_ep", QFN)
    numbered = [p for p in g.pads if p.number]
    assert len(numbered) == 17
    pin1 = _pad(g, "1")
    assert pin1.shape == "roundrect" and pin1.roundrect_ratio == 0.25
    assert pin1.size == (0.825, 0.25) and pin1.at == (-1.4625, -0.75)
    ep = _pad(g, "17")
    assert ep.shape == "rect" and ep.size == (1.7, 1.7) and ep.layers == ("F.Cu", "F.Mask")
    paste_only = [p for p in g.pads if not p.number]
    assert len(paste_only) == 4 and all(p.layers == ("F.Paste",) for p in paste_only)
    assert g.layer_summary()["F.CrtYd"]["closed"] is True


def test_custom_shape_pad() -> None:
    g = instance("solderjumper_triangle", JUMPER)
    pad = _pad(g, "1")
    assert pad.shape == "custom" and pad.layers == ("F.Cu", "F.Mask")
    extra = dict(pad.extra)
    (primitive,) = extra["primitives"]
    assert primitive["kind"] == "polygon" and primitive["filled"] is True
    assert len(primitive["points"]) == 5
    # pad 1 at x = -0.725, its triangle tip at local x = +1.0; pad 2 reaches x = -0.65 + 0.725
    x0, y0, x1, y1 = g.bbox()
    assert (x0, y0, x1, y1) == (-1.65, -1.25, 1.65, 1.25)  # courtyard encloses all
    # (attr exclude_from_pos_files exclude_from_bom allow_soldermask_bridges)
    assert g.mounting == "unspecified"
    assert g.exclude_from_pos and g.exclude_from_bom and not g.dnp
    assert ("allow_soldermask_bridges", True) in g.overrides


# -- bbox / to_dict ------------------------------------------------------------------------


def test_bbox_excludes_texts(seed: FootprintGeometry) -> None:
    far = Text(
        kind="user",
        layer="F.SilkS",
        at=(50.0, -50.0),
        size=(1.0, 1.0),
        rotation=0.0,
        value="far away",
    )
    moved = dataclasses.replace(seed, texts=(*seed.texts, far))
    assert moved.bbox() == seed.bbox()


def test_bbox_includes_pads() -> None:
    g = instance("solderjumper_triangle", JUMPER)
    no_shapes = dataclasses.replace(g, shapes=())
    # pad 1 spans x -1.225 .. 0.275 (triangle tip), pad 2 -0.725+0.725 .. 1.225
    x0, y0, x1, y1 = no_shapes.bbox()
    assert x0 == pytest.approx(-1.225) and x1 == pytest.approx(1.225)
    assert y0 == pytest.approx(-0.75) and y1 == pytest.approx(0.75)


def test_summary_has_no_shapes(seed: FootprintGeometry) -> None:
    d = seed.to_dict()
    assert "shapes" not in d
    assert d["lib_id"] == SEED and d["scope"] == "global" and d["mounting"] == "smd"
    assert d["pads"][0]["layers"] == ["F.Cu", "F.Mask", "F.Paste"]
    assert d["pads"][0]["drill"] is None
    assert d["graphics"]["F.CrtYd"] == {
        "bbox": [-2.53, -2.25, 2.53, 2.25],
        "closed": True,
        "items": 4,
    }
    assert d["bbox"] == [-2.53, -2.25, 2.53, 2.25]
    assert d["geometry_hash"] == geometry_hash(seed)
    assert d["models"][0]["scale"] == [1.0, 1.0, 1.0]
    assert {t["kind"] for t in d["texts"]} == {"reference", "value", "user"}
    assert "extra" not in d["pads"][0]


def test_full_has_shapes(seed: FootprintGeometry) -> None:
    d = seed.to_dict("full")
    assert len(d["shapes"]) == len(seed.shapes) == 16
    crtyd = [s for s in d["shapes"] if s["layer"] == "F.CrtYd"]
    assert len(crtyd) == 4
    assert all(s["kind"] == "segment" and len(s["points"]) == 2 for s in crtyd)
    assert crtyd[0]["width"] == 0.05


def test_tht_to_dict_drill() -> None:
    d = instance("pinheader_1x02", PINHEADER).to_dict()
    assert d["pads"][0]["drill"] == {"size": [1.0, 1.0], "shape": "circle"}
    assert d["mounting"] == "tht"


# -- hash ----------------------------------------------------------------------------------


def test_hash_stable(seed: FootprintGeometry) -> None:
    h = geometry_hash(seed)
    assert len(h) == 64 and int(h, 16) >= 0
    assert geometry_hash(instance("seed_srn4018", SEED)) == h
    reordered = dataclasses.replace(
        seed,
        pads=tuple(reversed(seed.pads)),
        shapes=tuple(reversed(seed.shapes)),
        texts=tuple(reversed(seed.texts)),
    )
    assert geometry_hash(reordered) == h


def test_hash_stable_across_fixtures() -> None:
    hashes = {
        geometry_hash(instance(n, lib))
        for n, lib in [
            ("seed_srn4018", SEED),
            ("pinheader_1x02", PINHEADER),
            ("qfn16_ep", QFN),
            ("solderjumper_triangle", JUMPER),
        ]
    }
    assert len(hashes) == 4


def _silk_segment() -> Shape:
    return Shape(
        kind="segment",
        layer="F.SilkS",
        points=((-1.0, 3.0), (1.0, 3.0)),
        center=None,
        size=None,
        width=0.12,
        closed=False,
    )


def _move_reference(g: FootprintGeometry) -> FootprintGeometry:
    texts = tuple(
        dataclasses.replace(t, at=(t.at[0], t.at[1] + 0.5)) if t.kind == "reference" else t
        for t in g.texts
    )
    return dataclasses.replace(g, texts=texts)


MANUFACTURING_CHANGES = {
    "pad_position_1um": lambda g: _replace_pad(
        g, "1", at=(_pad(g, "1").at[0] + 0.001, _pad(g, "1").at[1])
    ),
    "paste_margin": lambda g: _replace_pad(
        g, "1", extra=tuple(sorted((*_pad(g, "1").extra, ("paste_margin", -0.05))))
    ),
    "mounting": lambda g: dataclasses.replace(g, mounting="unspecified"),
    "exclude_from_pos": lambda g: dataclasses.replace(g, exclude_from_pos=True),
    "model_path": lambda g: dataclasses.replace(
        g, models=(dataclasses.replace(g.models[0], path="${KIPRJMOD}/other.step"),)
    ),
    "silk_segment": lambda g: dataclasses.replace(g, shapes=(*g.shapes, _silk_segment())),
    "reference_position": _move_reference,
}


@pytest.mark.parametrize("change", sorted(MANUFACTURING_CHANGES))
def test_hash_changes_on_manufacturing_relevant(seed: FootprintGeometry, change: str) -> None:
    changed = MANUFACTURING_CHANGES[change](seed)
    assert changed != seed
    assert geometry_hash(changed) != geometry_hash(seed)


def test_hash_sees_paste_margin_from_proto() -> None:
    """The paste margin is read from the pad stack (API message changed in memory)."""
    msg = _message("seed_srn4018.instance")
    base_hash = geometry_hash(geometry_from_proto(msg, msg.definition.items, SEED, "global"))
    from kipy.proto.board import board_types_pb2 as bt

    pads = []
    for packed in msg.definition.items:
        if packed.type_url.endswith("board.types.Pad"):
            pad = bt.Pad()
            packed.Unpack(pad)
            pads.append((packed, pad))
    packed, pad = pads[0]
    pad.pad_stack.front_outer_layers.solder_paste_settings.solder_paste_margin.value_nm = -50_000
    packed.Pack(pad)
    g = geometry_from_proto(msg, msg.definition.items, SEED, "global")
    assert ("paste_margin", -0.05) in _pad(g, pad.number).extra
    assert geometry_hash(g) != base_hash


def test_hash_ignores_metadata(seed: FootprintGeometry) -> None:
    h = geometry_hash(seed)
    changed = dataclasses.replace(
        seed,
        description="something else",
        keywords="other words",
        fields={
            **seed.fields,
            SOURCE_FIELD: "Bourns datasheet p. 3",
            REVIEW_FIELD: "yes",
            APPROVAL_FIELD: "approved by Peter, 2026-11-18",
            "Value": "10uH",
        },
    )
    assert geometry_hash(changed) == h
    again = dataclasses.replace(changed, fields={**changed.fields, REVIEW_FIELD: "no"})
    assert geometry_hash(again) == h
    # the reference/value *text* geometry is hashed, but not the field values
    texts = tuple(
        dataclasses.replace(t, value="L1") if t.kind == "reference" else t for t in seed.texts
    )
    assert geometry_hash(dataclasses.replace(seed, texts=texts)) == h


def test_hash_scope_and_lib_id_independent(seed: FootprintGeometry) -> None:
    other = dataclasses.replace(seed, lib_id="mylib:L_copy", scope="local")
    assert geometry_hash(other) == geometry_hash(seed)


# -- geometry_equal ------------------------------------------------------------------------


def test_geometry_equal_tolerance(seed: FootprintGeometry) -> None:
    x, y = _pad(seed, "1").at
    assert geometry_equal(seed, seed) == []
    assert geometry_equal(seed, _replace_pad(seed, "1", at=(x + 0.0005, y))) == []
    diffs = geometry_equal(seed, _replace_pad(seed, "1", at=(x + 0.002, y)))
    assert len(diffs) == 1
    assert "pad 1" in diffs[0]


def test_geometry_equal_reports_fields_and_counts(seed: FootprintGeometry) -> None:
    fewer = dataclasses.replace(seed, pads=seed.pads[:1])
    assert geometry_equal(seed, fewer)
    changed = dataclasses.replace(seed, fields={**seed.fields, SOURCE_FIELD: "x"})
    (diff,) = geometry_equal(seed, changed)
    assert SOURCE_FIELD in diff
    other_model = dataclasses.replace(
        seed,
        models=(Model3D(path="a.step", offset=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1)),),
    )
    assert geometry_equal(seed, other_model)


# -- review state --------------------------------------------------------------------------


def _with_fields(g: FootprintGeometry, **fields: str) -> FootprintGeometry:
    names = {"source": SOURCE_FIELD, "review": REVIEW_FIELD, "approval": APPROVAL_FIELD}
    return dataclasses.replace(
        g, scope="local", fields={**g.fields, **{names[k]: v for k, v in fields.items()}}
    )


def test_review_state(seed: FootprintGeometry) -> None:
    assert review_state(_with_fields(seed, source="ds", review="yes"), pending=False) == (
        "needs_user_review"
    )
    assert review_state(_with_fields(seed, source="ds", review="no"), pending=False) == "approved"
    assert (
        review_state(_with_fields(seed, source="ds", review="no", approval="ok"), pending=False)
        == "approved"
    )
    assert review_state(_with_fields(seed, review="yes"), pending=False) == "unreviewed"
    assert review_state(_with_fields(seed, source="ds"), pending=False) == "unreviewed"
    assert review_state(_with_fields(seed, source="ds", review="yes"), pending=True) == (
        "unreviewed"
    )
    assert review_state(_with_fields(seed, source="ds", review="no"), pending=True) == (
        "unreviewed"
    )
    assert review_state(_with_fields(seed, source="ds", review="maybe"), pending=False) == (
        "unreviewed"
    )


def test_field_constants() -> None:
    assert (REVIEW_FIELD, SOURCE_FIELD, APPROVAL_FIELD) == (
        "needs_user_review",
        "Footprint source",
        "User review",
    )
    assert {
        "reference",
        "value",
        "footprint source",
        "needs_user_review",
        "user review",
    } == RESERVED_FIELDS


# -- review fixes: tolerance-aware pairing, non-length tolerances, opaque items ------------


def _vertical(x: float) -> Shape:
    return Shape(
        kind="segment",
        layer="F.SilkS",
        points=((x, 0.0), (x, 1.0)),
        center=None,
        size=None,
        width=0.12,
        closed=False,
    )


NM = 1e-6


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ((1.0, 1.5), (1.0 - NM, 1.5)),  # "0.999999" sorts differently from "1.0"
        ((0.0, -0.5), (-NM, -0.5)),  # crosses the sign / digit boundary
    ],
)
def test_geometry_equal_pairs_shapes_not_by_index(
    seed: FootprintGeometry, before: tuple[float, float], after: tuple[float, float]
) -> None:
    a = dataclasses.replace(seed, shapes=tuple(_vertical(x) for x in before))
    b = dataclasses.replace(seed, shapes=tuple(_vertical(x) for x in after))
    assert geometry_equal(a, b) == []


def test_geometry_equal_pairs_unnumbered_pads(seed: FootprintGeometry) -> None:
    template = dataclasses.replace(seed.pads[0], number="")
    a = dataclasses.replace(
        seed,
        pads=(
            dataclasses.replace(template, at=(0.0, 0.0)),
            dataclasses.replace(template, at=(0.0, 1.0)),
        ),
    )
    b = dataclasses.replace(
        seed,
        pads=(
            dataclasses.replace(template, at=(NM, 0.0)),
            dataclasses.replace(template, at=(0.0, 1.0)),
        ),
    )
    assert geometry_equal(a, b) == []
    moved = dataclasses.replace(b, pads=(dataclasses.replace(template, at=(0.0, 0.01)), b.pads[1]))
    (diff,) = geometry_equal(a, moved)
    assert "pad (unnumbered)" in diff


def test_geometry_equal_real_shape_move_names_item(seed: FootprintGeometry) -> None:
    a = dataclasses.replace(seed, shapes=(_vertical(1.0), _vertical(1.5)))
    b = dataclasses.replace(seed, shapes=(_vertical(1.01), _vertical(1.5)))
    (diff,) = geometry_equal(a, b)
    assert diff.startswith("shape segment on F.SilkS")


def test_geometry_equal_angles_not_compared_with_mm_tolerance(seed: FootprintGeometry) -> None:
    rotated = _replace_pad(seed, "1", rotation=0.0005)
    (diff,) = geometry_equal(seed, rotated)
    assert "pad 1" in diff and "rotation" in diff
    ratio = _replace_pad(seed, "1", shape="roundrect", roundrect_ratio=0.2505)
    other = _replace_pad(seed, "1", shape="roundrect", roundrect_ratio=0.25)
    assert geometry_equal(ratio, other)


def test_hash_includes_ellipse_rotation_and_arc_angles(seed: FootprintGeometry) -> None:
    def ellipse(rotation: float, angles: tuple[float, float] | None) -> FootprintGeometry:
        shape = Shape(
            kind="ellipse_arc" if angles else "ellipse",
            layer="F.Fab",
            points=(),
            center=(0.0, 0.0),
            size=(2.0, 1.0),
            width=0.1,
            closed=angles is None,
            rotation=rotation,
            arc_angles=angles,
        )
        return dataclasses.replace(seed, shapes=(shape,))

    assert geometry_hash(ellipse(0.0, None)) != geometry_hash(ellipse(30.0, None))
    assert geometry_hash(ellipse(0.0, (0.0, 90.0))) != geometry_hash(ellipse(0.0, (0.0, 180.0)))
    assert ellipse(30.0, None).to_dict("full")["shapes"][0]["rotation"] == 30.0


def _with_zone(x_nm: int, zone_id: str) -> FootprintGeometry:
    """The seed plus a keepout zone (API message built in memory)."""
    from google.protobuf.any_pb2 import Any as PbAny  # type: ignore[import-untyped]
    from kipy.proto.board import board_types_pb2 as bt

    msg = _message("seed_srn4018.instance")
    zone = bt.Zone()
    zone.id.value = zone_id
    zone.type = bt.ZoneType.ZT_RULE_AREA
    zone.layers.append(bt.BoardLayer.BL_F_Cu)
    outline = zone.outline.polygons.add().outline
    for x, y in ((0, 0), (x_nm, 0), (x_nm, 1_000_000)):
        outline.nodes.add().point.CopyFrom(base_types.Vector2(x_nm=x, y_nm=y))
    outline.closed = True
    packed = PbAny()
    packed.Pack(zone)
    return geometry_from_proto(msg, [*msg.definition.items, packed], SEED, "global")


def test_hash_includes_unmodelled_items(seed: FootprintGeometry) -> None:
    zoned = _with_zone(1_000_000, "a")
    assert [name for name, _ in zoned.other] == ["Zone"]
    assert geometry_hash(zoned) != geometry_hash(seed)
    assert geometry_hash(_with_zone(1_000_000, "b")) == geometry_hash(zoned)  # ids ignored
    assert geometry_hash(_with_zone(2_000_000, "a")) != geometry_hash(zoned)
    assert geometry_equal(zoned, _with_zone(2_000_000, "a"))


def _with_group(name: str, members: list[str]) -> FootprintGeometry:
    from google.protobuf.any_pb2 import Any as PbAny  # type: ignore[import-untyped]
    from kipy.proto.board import board_types_pb2 as bt

    msg = _message("seed_srn4018.instance")
    group = bt.Group()
    group.id.value = members[0] + "-group"
    group.name = name
    group.items.extend(base_types.KIID(value=m) for m in members)
    packed = PbAny()
    packed.Pack(group)
    return geometry_from_proto(msg, [*msg.definition.items, packed], SEED, "global")


def test_hash_ignores_group_member_ids() -> None:
    """Group members are per-load KIIDs: they must not change the hash."""
    a = _with_group("g", ["a1", "a2"])
    assert geometry_hash(_with_group("g", ["b1", "b2"])) == geometry_hash(a)
    assert geometry_hash(_with_group("h", ["a1", "a2"])) != geometry_hash(a)
