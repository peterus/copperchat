"""Tool provider: footprint tools (`fp_*`, footprints spec §7)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.footprints.read import (
    DEFAULT_LIST_LIMIT,
    MAX_LIB_ID_CHARS,
    MAX_LIST_LIMIT,
    MAX_QUERY_CHARS,
    fp_get,
    fp_list_local,
)
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
    ToolSpec(
        "fp_list_local",
        "List the footprints of this repository's local footprint library (the one "
        "CopperChat creates footprints in): name, lib_id, description, pad count, review "
        "state (needs_user_review: unreviewed / needs_user_review / approved), source and "
        "the references of this project's symbols that use it (used_by). library is null "
        "while the project has no local library yet.",
        _obj(
            {
                "query": {
                    "type": "string",
                    "maxLength": MAX_QUERY_CHARS,
                    "description": "Only footprints whose name or description contain every word.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_LIMIT,
                    "default": DEFAULT_LIST_LIMIT,
                    "description": f"Maximum number of footprints (default {DEFAULT_LIST_LIMIT}).",
                },
            },
            [],
        ),
        fp_list_local,
    ),
]
