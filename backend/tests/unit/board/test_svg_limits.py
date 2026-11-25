"""SVG limits per render kind (SP4 P1c Task 5, B-C9): the footprint caps stay as they
were, board renders get larger ones."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from copperchat.board.model import Rect
from copperchat.errors import StructuredToolError
from copperchat.views import footprint_render as fr
from copperchat.views.board_render import BOARD_SVG_LIMITS


def _svg(body: str, view_box: str = "0 0 100 100") -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}">{body}</svg>').encode()


def test_footprint_limits_unchanged() -> None:
    assert (
        fr.SvgLimits(
            max_bytes=4 * 1024 * 1024,
            max_elements=10_000,
            max_coords=50_000,
            max_element_coords=2_000,
            max_span=1_000.0,
        )
        == fr.FOOTPRINT_SVG_LIMITS
    )
    assert (
        fr.SvgLimits(
            fr.MAX_SVG_BYTES, fr.MAX_ELEMENTS, fr.MAX_COORDS, fr.MAX_ELEMENT_COORDS, fr.MAX_SPAN
        )
        == fr.FOOTPRINT_SVG_LIMITS
    )
    # the default of load_kicad_svg is the footprint set
    with pytest.raises(StructuredToolError):
        fr.load_kicad_svg(_svg('<circle cx="1" cy="1" r="0.1"/>' * fr.MAX_ELEMENTS))


def test_board_limits_accept_large() -> None:
    body = "".join(f'<circle cx="{i % 100}" cy="{i // 600}" r="0.1"/>' for i in range(60_000))
    data = _svg(body)
    root = fr.load_kicad_svg(data, limits=BOARD_SVG_LIMITS)
    assert len(root) == 60_000
    with pytest.raises(StructuredToolError) as exc:
        fr.load_kicad_svg(data)
    assert exc.value.code == "render_failed"
    assert BOARD_SVG_LIMITS.max_bytes > fr.MAX_SVG_BYTES
    assert BOARD_SVG_LIMITS.max_element_coords > fr.MAX_ELEMENT_COORDS


def _prepare(tmp_path: Path, body: str, region: Rect | None) -> tuple[bytes, tuple[float, float]]:
    from copperchat.views import board_render as br
    from tests.unit.board.builders import board

    out = tmp_path / "kicad.svg"
    out.write_bytes(_svg(body))
    ov = br._Overlay(
        mirror=False,
        region=region,
        model=board(),
        hulls=False,
        highlight_nets=(),
        highlight_layers=(),
        markers=(),
        title="t",
    )
    return br._prepare(out, ov)


# 1000 lines across a 100 mm drawing: a span of 1000 drawing extents (cap 20,000)
_LONG_LINES = '<line x1="0" y1="1" x2="100" y2="1" stroke="#fff"/>' * 1000


def test_region_zoom_does_not_inflate_span(tmp_path: Path) -> None:
    """The wrapper's viewBox is the region: a 25x zoom must not count KiCad's lines 25x
    (the span is enforced on KiCad's own drawing, review round 2)."""
    svg, extent = _prepare(tmp_path, _LONG_LINES, Rect(0, 0, 4, 4))
    assert svg.startswith(b"<svg") and max(extent) < 10
    # the span of KiCad's drawing itself is still capped
    with pytest.raises(StructuredToolError) as err:
        _prepare(tmp_path, _LONG_LINES * 21, None)
    assert "kicad-cli wrote an SVG whose lines are longer" in err.value.why


def test_wrapper_rejection_not_blamed_on_kicad_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from copperchat.views import board_render as br

    monkeypatch.setattr(br, "_WRAPPER_LIMITS", replace(br._WRAPPER_LIMITS, max_elements=5))
    with pytest.raises(StructuredToolError) as err:
        _prepare(tmp_path, '<circle cx="1" cy="1" r="0.1"/>' * 3, None)
    assert err.value.code == "render_failed"
    assert err.value.why.startswith("the board drawing with its overlays was rejected (")
    assert "kicad-cli" not in err.value.why


def test_huge_single_path_rejected_cheaply() -> None:
    """One path far over the per-element cap is rejected without parsing all of it
    (review round 2: it cost ~320 MB / 2.7 s); same error as before."""
    d = "M " + "1.5,2.5 " * 750_000  # ~6 MB, 750,000 points
    data = _svg(f'<path d="{d}"/>')
    start = time.monotonic()
    with pytest.raises(StructuredToolError) as err:
        fr.load_kicad_svg(data, limits=BOARD_SVG_LIMITS)
    assert time.monotonic() - start < 1.0
    start = time.monotonic()  # the path's cost stops at the cap (unbounded: ~1 s here)
    assert fr._path_cost(d, BOARD_SVG_LIMITS.max_element_coords)[0] == 50_001
    assert time.monotonic() - start < 0.2
    assert err.value.why == (
        f"kicad-cli wrote an SVG element of more than {BOARD_SVG_LIMITS.max_element_coords} points"
    )


@pytest.mark.parametrize(
    "value", ["", "M 1,2", "M 0,0 L 3,4 L 3,0 Z", "M 0,0 A 1 1 0 0 1 2,2", "1e400,0 1,1"]
)
def test_path_cost_unchanged_under_the_cap(value: str) -> None:
    assert fr._path_cost(value, 50_000) == fr._path_cost(value)
    assert fr._path_cost("1,1 " * 11, 10) == (11, 0.0)
    assert fr._path_cost("1,1 " * 10, 10) == fr._path_cost("1,1 " * 10)
