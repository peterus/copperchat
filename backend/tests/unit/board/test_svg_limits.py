"""SVG limits per render kind (SP4 P1c Task 5, B-C9): the footprint caps stay as they
were, board renders get larger ones."""

from __future__ import annotations

import pytest

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
