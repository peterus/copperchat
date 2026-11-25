"""Every board tool schema is bounded (SP4 plan call 6, Global Constraints): a guard over
the specs of the provider `copperchat.kicad_tools.board.specs`, so the specs later phases
append there are covered automatically. The pre-SP4 read tools (`pcb_list_footprints`,
`pcb_get_footprint`, `pcb_get_net` in `kicad_tools/specs.py`) are another provider and
out of scope."""

from __future__ import annotations

import importlib
from typing import Any

from copperchat.tools.registry import _PROVIDERS

BOARD_PROVIDER = "copperchat.kicad_tools.board.specs"


def unbounded(schema: Any, path: str = "$") -> list[str]:
    """JSON paths of the unbounded nodes of `schema`: an array without `maxItems`, a
    string without `maxLength` / `enum` / `pattern`, a number without a lower and an
    upper bound, an object without `additionalProperties: false`."""
    if not isinstance(schema, dict):
        return []
    out: list[str] = []
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "array" in kinds and "maxItems" not in schema:
        out.append(f"{path}: array without maxItems")
    if "string" in kinds and not {"maxLength", "enum", "pattern"} & set(schema):
        out.append(f"{path}: string without maxLength, enum or pattern")
    if {"number", "integer"} & set(kinds):
        if not {"minimum", "exclusiveMinimum"} & set(schema):
            out.append(f"{path}: number without a lower bound")
        if not {"maximum", "exclusiveMaximum"} & set(schema):
            out.append(f"{path}: number without an upper bound")
    if "object" in kinds and schema.get("additionalProperties") is not False:
        out.append(f"{path}: object without additionalProperties: false")
    for name, sub in (schema.get("properties") or {}).items():
        out += unbounded(sub, f"{path}.properties.{name}")
    items = schema.get("items")
    if isinstance(items, list):
        for i, sub in enumerate(items):
            out += unbounded(sub, f"{path}.items[{i}]")
    else:
        out += unbounded(items, f"{path}.items")
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(key) or []):
            out += unbounded(sub, f"{path}.{key}[{i}]")
    return out


def test_every_board_schema_bounded() -> None:
    assert BOARD_PROVIDER in _PROVIDERS
    specs = importlib.import_module(BOARD_PROVIDER).TOOL_SPECS
    names = {s.name for s in specs}
    assert {"pcb_status", "pcb_render"} <= names
    problems = [f"{s.name} {p}" for s in specs for p in unbounded(s.input_schema)]
    assert problems == []


def test_guard_finds_unbounded_nodes() -> None:
    bad = {
        "type": "object",
        "properties": {
            "refs": {"type": "array", "items": {"type": "string"}},
            "x": {"type": "number", "minimum": 0},
            "opt": {"anyOf": [{"type": "integer", "maximum": 3}, {"type": "object"}]},
        },
    }
    assert unbounded(bad) == [
        "$: object without additionalProperties: false",
        "$.properties.refs: array without maxItems",
        "$.properties.refs.items: string without maxLength, enum or pattern",
        "$.properties.x: number without an upper bound",
        "$.properties.opt.anyOf[0]: number without a lower bound",
        "$.properties.opt.anyOf[1]: object without additionalProperties: false",
    ]
