"""Tool provider: footprint tools (`fp_*`, footprints spec §7)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.footprints.read import MAX_LIB_ID_CHARS, fp_get
from copperchat.tools.spec import ToolSpec


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_LIB_ID = {
    "type": "string",
    "minLength": 3,
    "maxLength": MAX_LIB_ID_CHARS,
    "description": "<library nickname>:<footprint name>, e.g. 'Inductor_SMD:L_Bourns-SRN4018'.",
}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "fp_get",
        "One footprint's geometry by lib_id, from any footprint library (the repo's local "
        "library, project or stock): pads (number, type, shape, position, size, drill, "
        "layers), per-layer graphics summary, texts, 3D models, bbox (pads + shapes, mm, "
        "y down), mounting, fields and geometry_hash; for local footprints also `source` "
        "and `review` (needs_user_review / approved / unreviewed). detail='full' adds the "
        "shape list. Large footprints are capped (pads_truncated / truncated, exact "
        "pad_count). The first footprint call of a project without a board "
        "loads the footprint libraries (several seconds, see notes).",
        _obj(
            {
                "lib_id": _LIB_ID,
                "detail": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "description": "'summary' (default, no shape list) or 'full'.",
                },
            },
            ["lib_id"],
        ),
        fp_get,
    ),
]
