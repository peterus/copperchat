"""kipy item builders and dumps for the real-KiCad edit tests (API objects only)."""

from __future__ import annotations

import uuid
from typing import Any

from kipy.geometry import Vector2
from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType
from kipy.schematic_types import LocalLabel, SchematicLine

from copperchat.kicad_session import KicadHandle

MM = 1_000_000


def v2(x_mm: float, y_mm: float) -> Vector2:
    return Vector2.from_xy(round(x_mm * MM), round(y_mm * MM))


def wire(start: tuple[float, float], end: tuple[float, float]) -> Any:
    w = SchematicLine()
    w.start = v2(*start)
    w.end = v2(*end)
    w.type = SchematicLineType.SLT_WIRE
    w.proto.id.value = str(uuid.uuid4())
    return w


def local_label(text: str, at: tuple[float, float]) -> Any:
    label = LocalLabel()
    label.text.value = text
    label.position = v2(*at)
    label.text.position = v2(*at)
    label.proto.id.value = str(uuid.uuid4())
    return label


def item_ids(h: KicadHandle) -> set[str]:
    """UUIDs of every item on every sheet instance (the document's item set)."""
    return {str(i.id.value) for i in h.schematic().get_items()}


def sheet_instance(h: KicadHandle, filename: str, nth: int = 0) -> Any:
    hits = [s for s in h.schematic().get_hierarchy() if str(s.filename) == filename]
    return hits[nth]


def edit_ctx(session: Any) -> Any:
    """ToolContext for edit-tool tests (E2 adds its EditingService here)."""
    from copperchat.tools.spec import ToolContext

    return ToolContext(project=session.project, session=session, settings=session.settings)
