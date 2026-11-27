"""Round 2 (R4, plan call 8): intra-block and straight-corridor nets join the critical set."""

from __future__ import annotations

from dataclasses import replace

from copperchat.board.classify import Classification, classify
from copperchat.board.critical import (
    BLOCK_PREFIX,
    critical_with_reasons,
    defined_blocks,
    extra_critical,
)
from copperchat.board.model import BoardModel

from .builders import board, part
from .builders_circuit import pad


def _two(ref: str, x: float, y: float, n1: str, n2: str) -> object:
    """A 2-pad part along x: pad 1 at x - 0.8, pad 2 at x + 0.8 (0.8 x 0.9 mm pads)."""
    return part(
        ref,
        x,
        y,
        half=1.5,
        pads=(
            pad(ref, "1", n1, (x - 0.8, y), w=0.8, h=0.9),
            pad(ref, "2", n2, (x + 0.8, y), w=0.8, h=0.9),
        ),
    )


def _board(*parts: object, groups: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> BoardModel:
    return board(parts=tuple(parts), net_class_of=(), groups=groups)


def _cls(m: BoardModel) -> Classification:
    """`classify(m)` with no class-critical nets (it marks short nets critical itself)."""
    return replace(classify(m), critical_nets=())


def test_extra_critical_intra_block() -> None:
    # R1 and R2 of block "a" share net S (4 mm apart in x and y: no corridor)
    m = _board(_two("R1", 20, 20, "S", "X1"), _two("R2", 26, 24, "S", "X2"))
    got = extra_critical(m, _cls(m), {"a": ("R1", "R2")})
    assert got == {"S": "intra_block"}


def test_extra_critical_inter_block_excluded() -> None:
    m = _board(_two("R1", 20, 20, "S", "X1"), _two("R2", 26, 24, "S", "X2"))
    assert extra_critical(m, _cls(m), {"a": ("R1",), "b": ("R2",)}) == {}
    # a part in no block: not intra-block either
    assert extra_critical(m, _cls(m), {"a": ("R1",)}) == {}


def test_extra_critical_ground_excluded() -> None:
    m = _board(_two("R1", 20, 20, "GND", "X1"), _two("R2", 22.5, 20, "GND", "X2"))
    assert extra_critical(m, _cls(m), {"a": ("R1", "R2")}) == {}


def test_extra_critical_pad_limit() -> None:
    parts = [_two(f"R{i}", 10 + 6 * i, 10 + 5 * (i % 2), "S", f"X{i}") for i in range(1, 10)]
    m = _board(*parts)
    refs = tuple(p.ref for p in parts)  # type: ignore[attr-defined]
    assert extra_critical(m, _cls(m), {"a": refs}) == {}  # 9 pads > 8
    assert extra_critical(m, _cls(m), {"a": refs[:8]}) == {}  # R9 lies outside
    m8 = _board(*parts[:8])
    assert extra_critical(m8, _cls(m8), {"a": refs[:8]}) == {"S": "intra_block"}


def test_extra_critical_corridor_pair() -> None:
    # R1.2 (20.8, 20) and R2.1 (22.7, 20): 1.1 mm edge gap, full overlap -> corridor
    m = _board(_two("R1", 20, 20, "X1", "S"), _two("R2", 23.5, 20, "S", "X2"))
    assert extra_critical(m, _cls(m), {"a": ("R1",), "b": ("R2",)}) == {"S": "corridor"}
    # misaligned by 0.8 mm: the 0.9 mm pads overlap by 0.1 < the track width -> none
    m2 = _board(_two("R1", 20, 20, "X1", "S"), _two("R2", 23.5, 20.8, "S", "X2"))
    assert extra_critical(m2, _cls(m2), {"a": ("R1",), "b": ("R2",)}) == {}


def test_extra_critical_skips_class_critical() -> None:
    m = _board(_two("R1", 20, 20, "X1", "S"), _two("R2", 23.5, 20, "S", "X2"))
    cls = replace(classify(m), critical_nets=("S",))
    assert extra_critical(m, cls, {"a": ("R1", "R2")}) == {}


def test_extra_critical_uses_defined_groups() -> None:
    # the proposals would split R1 / R2; the board's groups merge them (an absorbed block)
    m = _board(
        _two("R1", 20, 20, "S", "X1"),
        _two("R2", 26, 24, "S", "X2"),
        groups=((BLOCK_PREFIX + "a", ("R1", "R2")), ("other", ("R1",))),
    )
    assert defined_blocks(m) == {"a": ("R1", "R2")}
    cls = _cls(m)
    nets, reasons = critical_with_reasons(m, cls)
    assert "S" in nets and reasons["S"] == "intra_block"
    assert set(reasons) == set(nets)


def test_critical_order_class_corridor_intra() -> None:
    m = _board(
        _two("R1", 20, 20, "A", "C"),
        _two("R2", 23.5, 20, "C", "X2"),  # C: corridor R1.2 - R2.1
        _two("R3", 40, 40, "A", "X3"),  # A: R1.1 - R3.1, far apart, one block
        groups=((BLOCK_PREFIX + "a", ("R1", "R3")), (BLOCK_PREFIX + "b", ("R2",))),
    )
    cls = replace(classify(m), critical_nets=("Z",))
    nets, reasons = critical_with_reasons(m, cls)
    assert nets[0] == "Z" and reasons["Z"] == "class"
    assert nets.index("C") < nets.index("A")
    assert reasons["C"] == "corridor" and reasons["A"] == "intra_block"
