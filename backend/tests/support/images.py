from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image, ImageChops


def assert_image_close(
    actual: bytes, golden: Path, *, threshold: int = 16, max_ratio: float = 0.01
) -> None:
    """Fails if more than `max_ratio` of pixels differ by more than `threshold` (any channel).

    `UPDATE_GOLDENS=1` rewrites the golden instead (scripts/update-goldens.sh).
    """
    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_bytes(actual)
        return
    a = Image.open(io.BytesIO(actual)).convert("RGB")
    g = Image.open(golden).convert("RGB")
    assert a.size == g.size, f"size {a.size} != golden {g.size} ({golden.name})"
    diff = ImageChops.difference(a, g).convert("L").point(lambda v: 255 if v > threshold else 0)
    bad = diff.histogram()[255]
    ratio = bad / (a.width * a.height)
    if ratio > max_ratio:
        out = golden.with_suffix(".actual.png")
        out.write_bytes(actual)
        raise AssertionError(
            f"{golden.name}: {ratio:.3%} pixels differ (> {max_ratio:.1%}); "
            f"wrote {out.name}; run scripts/update-goldens.sh if intended"
        )
