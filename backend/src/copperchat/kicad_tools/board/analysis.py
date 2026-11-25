"""Board analysis read tools (SP4 spec §7.1, P3a Task 5): `pcb_analyze` (part/net
classification, B-C6) and `preset_for` (B-C6 fab preset rule). Read tools: one board-model
read, the `board.md` tables, the computation in the CPU worker (`run_cpu`, job functions in
`copperchat.board.jobs`); never the edit lock, nothing written."""

from __future__ import annotations

from typing import Any

from copperchat.board.boardmd import BoardDoc, read_board_doc
from copperchat.board.jobs import analyse_job
from copperchat.board.model import BoardModel
from copperchat.board.presets import PRESETS, FabPreset
from copperchat.board.worker import run_cpu
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.tools.spec import ToolContext, ToolResult

# run_cpu budget of one analysis job (plan Task 5)
ANALYSIS_TIMEOUT_S = 30.0


def preset_for(model: BoardModel, doc: BoardDoc) -> FabPreset | None:
    """The fab preset of a board (B-C6): the one `board.md` names when it is known, else the
    JLCPCB preset of the copper layer count (None for counts without a preset)."""
    if doc.preset is not None and doc.preset in PRESETS:
        return PRESETS[doc.preset]
    return PRESETS.get(f"jlcpcb-{len(model.copper_layers)}l")


async def pcb_analyze(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Classification of the live board (`analyse_job`) for `args["scope"]` (default all);
    `no_board` without a board."""
    require_board(ctx)
    scope = args.get("scope", "all")
    model = await board_model(ctx)
    doc = await read_board_doc(ctx)
    data = await run_cpu(analyse_job, model, doc, scope, timeout_s=ANALYSIS_TIMEOUT_S)
    return ToolResult(data=data)
