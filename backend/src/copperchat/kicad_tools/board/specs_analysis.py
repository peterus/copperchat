"""Specs of the board analysis read tools (P3a Task 5), appended to `board/specs.py`'s
`TOOL_SPECS`. Read tools: `mutating=False`, never the edit lock."""

from __future__ import annotations

from copperchat.board.jobs import MAX_ENTRIES, SCOPE_KEYS
from copperchat.board.metrics import MAX_OFFENDERS, METRIC_IDS
from copperchat.kicad_tools.board.analysis import pcb_analyze, pcb_metrics
from copperchat.tools.spec import ToolSpec

PCB_ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": list(SCOPE_KEYS),
            "description": "Which part of the analysis to return (default all).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

PCB_ANALYZE_DESCRIPTION = (
    "How CopperChat reads the board's circuit (generic rules on references, footprint "
    "libraries, pad counts, net names and topology; board.md 'Ref | Class | Reason' rows "
    "override part classes). Result keys: parts (ref -> class: anchor, connector, "
    "decoupling_cap, switching_regulator, inductor, crystal, module_with_antenna, "
    "mounting_hole, fiducial, test_point, other), nets (net -> kind: ground, supply, "
    "switch_node, diff_pair, crystal, signal), decoupling ({cap, ic, pin, supply_net, "
    "ground_net}), switchers ({ic, inductor, sw_net, vin_net, in_caps, out_caps, "
    "fb_parts}), crystals ({ref, ic, load_caps, nets}), pairs ({p, n, anchor}), blocks "
    "(proposed placement blocks {name, anchor, refs, sheet}), critical_nets, "
    "overrides_applied, board_md_errors (unparseable board.md rows). scope: parts = parts, "
    "decoupling, switchers, crystals, overrides_applied; nets = nets, pairs, critical_nets; "
    "blocks = blocks; all (default) = everything; board_md_errors is always included. "
    f"Every list or mapping K holds at most {MAX_ENTRIES} entries and comes with K_omitted "
    "(entries left out). No board -> no_board. Read-only."
)

PCB_METRICS_SCHEMA = {
    "type": "object",
    "properties": {
        "only": {
            "type": "array",
            "items": {"type": "string", "enum": list(METRIC_IDS)},
            "maxItems": len(METRIC_IDS),
            "uniqueItems": True,
            "description": "Compute only these metrics (default all). Without M2 and M3 "
            "no DRC runs (faster).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

PCB_METRICS_DESCRIPTION = (
    "The board's quality metrics (spec §12): M1 placement (courtyards inside the outline, "
    "no overlaps, SMD on allowed sides), M2 unrouted, M3 DRC errors / unaccepted warnings, "
    "M4 angles (only 45° direction changes: any 90°, acute or off-grid angle fails), M5 "
    "detour, M6 long connections, M7 micro-jogs / short chamfers / bends, M8 decoupling and "
    "crystal distance, M9 switcher hot loop, M10 block grouping, M11 GND plane fill and "
    "voids, M12 widths and pair mismatch, M13 edge parts and antenna keepouts, M14 JLCPCB "
    "fab rules. Result: stage (placement | routed), metrics [{id, value, verdict "
    "(pass | review | fail | n/a), hard, review (thresholds), offenders [{what, refs, nets, "
    f"at, value}}] (at most {MAX_OFFENDERS}), omitted}}], hard_failures, reviews, "
    "placement_hash, board_hash, seconds. Hard thresholds are the owner's and never "
    "relaxed. No board -> no_board. Read-only."
)

ANALYSIS_SPECS: list[ToolSpec] = [
    ToolSpec("pcb_analyze", PCB_ANALYZE_DESCRIPTION, PCB_ANALYZE_SCHEMA, pcb_analyze),
    ToolSpec("pcb_metrics", PCB_METRICS_DESCRIPTION, PCB_METRICS_SCHEMA, pcb_metrics),
]
