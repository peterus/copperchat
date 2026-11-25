from __future__ import annotations

import re
from dataclasses import replace

from copperchat.board.model import BoardModel, TextItem, board_hash, placement_hash

from .builders import (
    board,
    net_class,
    outline,
    part,
    track,
    via,
    with_part,
    zone,
)


def _ph_differs(a: BoardModel, b: BoardModel) -> None:
    assert placement_hash(a) != placement_hash(b)


def test_ph_move_001() -> None:
    m = board()
    _ph_differs(m, with_part(m, "R1", x=m.part("R1").x + 0.01))


def test_ph_rotate() -> None:
    m = board()
    _ph_differs(m, with_part(m, "R1", rot=90.0))


def test_ph_flip_side() -> None:
    m = board()
    _ph_differs(m, with_part(m, "R1", side="bottom"))


def test_ph_lock_flag() -> None:
    m = board()
    _ph_differs(m, with_part(m, "R1", locked=True))


def test_ph_outline_change() -> None:
    m = board()
    _ph_differs(m, replace(m, outline=outline(w=100.01)))


def _keepout_board(
    poly_dx: float = 0.0, keepout: frozenset[str] = frozenset({"tracks"})
) -> BoardModel:
    poly = tuple((x + poly_dx, y) for x, y in ((1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)))
    return board(
        parts=(part("R1", 30, 30, rule_areas=(poly,)),),
        zones=(zone(name="K", rule_area=True, poly=poly, keepout=keepout),),
    )


def test_ph_rule_area_change() -> None:
    base = _keepout_board()
    _ph_differs(base, _keepout_board(poly_dx=0.01))
    _ph_differs(base, _keepout_board(keepout=frozenset({"tracks", "vias"})))
    # footprint-embedded keepout moved
    m = board(parts=(part("R1", 30, 30, rule_areas=(((1.0, 1.0), (5.0, 1.0), (5.0, 5.0)),)),))
    m2 = board(parts=(part("R1", 30, 30, rule_areas=(((1.01, 1.0), (5.0, 1.0), (5.0, 5.0)),)),))
    _ph_differs(m, m2)


def test_ph_track_add() -> None:
    m = board()
    assert placement_hash(m) == placement_hash(replace(m, tracks=(track(),)))


def test_ph_move_below_rounding() -> None:
    m = board()
    assert placement_hash(m) == placement_hash(with_part(m, "R1", x=m.part("R1").x + 0.004))


def test_ph_rot_360_equiv() -> None:
    m = board()
    assert placement_hash(with_part(m, "R1", rot=0.0)) == placement_hash(
        with_part(m, "R1", rot=360.0)
    )


def test_bh_silk_text_move() -> None:
    m = board()
    p = m.part("R1")
    t = p.texts[0]
    moved = replace(t, at=(t.at[0], t.at[1] + 0.01))
    assert board_hash(m) != board_hash(with_part(m, "R1", texts=(moved,)))


def test_bh_track_add() -> None:
    m = board()
    assert board_hash(m) != board_hash(replace(m, tracks=(track(),)))


def test_bh_via_add() -> None:
    m = board()
    assert board_hash(m) != board_hash(replace(m, vias=(via(),)))


def test_bh_zone_settings() -> None:
    a = board(zones=(zone(settings=(("clearance", "0.2"),)),))
    b = board(zones=(zone(settings=(("clearance", "0.3"),)),))
    assert board_hash(a) != board_hash(b)


def test_bh_rule_change() -> None:
    m = board()
    other = replace(m, rules=(("min_clearance", 0.15), ("min_track_width", 0.127)))
    assert board_hash(m) != board_hash(other)


def test_bh_net_class_change() -> None:
    m = board()
    other = replace(m, net_classes=(net_class(track_width=0.3),))
    assert board_hash(m) != board_hash(other)


def test_bh_net_class_assignment() -> None:
    m = board()
    other = replace(m, net_class_of=(("N1", "Power"),))
    assert board_hash(m) != board_hash(other)


def test_bh_dru_text() -> None:
    m = replace(board(), dru_text="(version 1)\n")
    other = replace(m, dru_text="(version 1)\n(rule x)\n")
    assert board_hash(m) != board_hash(other)


def test_bh_zone_fill_only() -> None:
    a = board(zones=(zone(filled_area_mm2=1.0),))
    b = board(
        zones=(
            zone(
                filled_area_mm2=250.0,
            ),
        )
    )
    b = replace(b, zones=(replace(b.zones[0], filled=(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),)),))
    assert board_hash(a) == board_hash(b)


def test_bh_dru_newlines() -> None:
    a = replace(board(), dru_text="(version 1)\r\n(rule x)\r\n")
    b = replace(board(), dru_text="(version 1)\n(rule x)\n")
    c = replace(board(), dru_text="(version 1)\r(rule x)\r")
    assert board_hash(a) == board_hash(b) == board_hash(c)


def test_bh_warnings_not_hashed() -> None:
    m = board()
    assert board_hash(m) == board_hash(replace(m, warnings=("x",)))
    assert placement_hash(m) == placement_hash(replace(m, warnings=("x",)))


def test_hashes_kiid_independent() -> None:
    m = replace(
        board(
            tracks=(track(),),
            vias=(via(),),
            zones=(zone(),),
        )
    )
    other = replace(
        m,
        parts=tuple(replace(p, kiid="new-" + p.kiid) for p in m.parts),
        tracks=tuple(replace(t, kiid="new-" + t.kiid) for t in m.tracks),
        vias=tuple(replace(v, kiid="new-" + v.kiid) for v in m.vias),
        zones=tuple(replace(z, kiid="new-" + z.kiid) for z in m.zones),
    )
    assert placement_hash(m) == placement_hash(other)
    assert board_hash(m) == board_hash(other)


def test_hash_order_independent() -> None:
    m = board()
    other = replace(m, parts=tuple(reversed(m.parts)))
    assert placement_hash(m) == placement_hash(other)
    assert board_hash(m) == board_hash(other)


def test_hash_is_sha256_hex() -> None:
    m = board()
    for h in (placement_hash(m), board_hash(m)):
        assert re.fullmatch(r"[0-9a-f]{64}", h)


def test_text_item_importable() -> None:
    assert TextItem("reference", "R1", "F.SilkS", (0.0, 0.0), (1.0, 1.0), 0.15, True).visible
