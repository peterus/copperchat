import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from copperchat.views.agent_render import grid_step, overlay_grid, render_board
from tests.support.images import assert_image_close

G = Path(__file__).resolve().parent.parent / "fixtures" / "golden"
SNAP = json.loads((G / "board_snapshot.json").read_text())
MARKERS = [{"severity": "error", "position_mm": SNAP["footprints"][0]["position_mm"]}]


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("board_agent", {"theme": "agent"}),
        ("board_light", {"theme": "light"}),
        ("board_fcu", {"theme": "agent", "layers": ["F.Cu"]}),
    ],
)
def test_board_goldens(name: str, kwargs: dict[str, Any]) -> None:
    png = render_board(SNAP, region_box=None, markers=MARKERS, **{"layers": None, **kwargs})
    assert_image_close(png, G / "png" / f"{name}.png")


def test_region_crop_golden() -> None:
    u1 = next(f for f in SNAP["footprints"] if f["ref"] == "U1")["bbox_mm"]
    box = (u1["x"] - 5, u1["y"] - 5, u1["w"] + 10, u1["h"] + 10)
    png = render_board(SNAP, region_box=box, layers=None, theme="agent", markers=[])
    assert_image_close(png, G / "png" / "board_region_U1.png")


def test_deterministic_and_bounded() -> None:
    a = render_board(SNAP, region_box=None, layers=None, theme="agent", markers=[])
    b = render_board(SNAP, region_box=None, layers=None, theme="agent", markers=[])
    assert a == b
    huge = render_board(SNAP, region_box=(0, 0, 2000, 500), layers=None, theme="agent", markers=[])
    assert max(Image.open(io.BytesIO(huge)).size) <= 2400


def test_grid_step() -> None:
    assert grid_step(15) == 1 and grid_step(60) == 5 and grid_step(5000) == 500
    for width in (7.0, 333.0, 1e9):  # bounded line count for any region size
        assert width / grid_step(width) <= 20


def test_overlay_grid_keeps_size() -> None:
    page = Image.new("RGB", (300, 200), (255, 255, 255))
    marker = {"severity": "warning", "position_mm": {"x": 5, "y": 5}}
    out = Image.open(io.BytesIO(overlay_grid(page, (0.0, 0.0), 10.0, [marker], "light")))
    assert out.size == (300, 200)
    assert out.getpixel((50, 35)) != (255, 255, 255)  # marker ring at 5 mm, r = 15 px
