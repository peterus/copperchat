"""The board skills (`session-board`, `board-layout`, `copperchat-tools` Board section) and
the board configuration docs (SP4 P9b Tasks 1, 2 and 5)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from copperchat.agent.profiles import PCB_READ, PCB_WRITES, profile_specs
from copperchat.board.boardmd import PartClass
from copperchat.skills.store import BUILTIN_ROOT, parse_front_matter
from copperchat.tools import registry

REPO = Path(__file__).resolve().parents[4]
SESSION_BOARD = BUILTIN_ROOT / "session-board" / "SKILL.md"
BOARD_LAYOUT = BUILTIN_ROOT / "board-layout" / "SKILL.md"
TOOLS = BUILTIN_ROOT / "copperchat-tools" / "SKILL.md"
INDEX = REPO / "docs/superpowers/plans/2026-11-25-board-plan.md"

STEPS = (
    "## S0 Intake",
    "## S1 Board from schematic",
    "## S2 Outline, holes, rules",
    "## S3 Blocks",
    "## S4 Placement",
    "## S5 Critical routing",
    "## S6 Fill routing",
    "## S7 Planes and pours",
    "## S8 Final check",
    "## S9 Fab",
)
# B-C14: every error code a pcb_* tool can return (session_type_unavailable is HTTP only).
BOARD_ERROR_CODES = (
    "board_tools_disabled",
    "no_board",
    "schematic_unsaved",
    "sync_failed",
    "invalid_argument",
    "not_found",
    "unknown_block",
    "stackup_unavailable",
    "does_not_fit",
    "part_routed",
    "placement_not_approved",
    "not_approved",
    "approval_not_allowed",
    "freerouting_unavailable",
    "fill_not_acceptable",
    "board_compute_timeout",
    "refill_timeout",
    "drc_rejected",
    "drc_timeout",
    "verification_failed",
    "output_path_unsafe",
    "render_failed",
    "export_failed",
    "document_inconsistent",
    "read_only",
    "save_failed",
    "plan_required",
    "plan_step_required",
)
TOOL_NAME = re.compile(r"\b((?:pcb|fp|doc)_[a-z0-9_]+)")
# identifiers in the board skills that look like tool names but are error codes
NOT_TOOLS = {"doc_write"}
DESIGN_REF = re.compile(r"\b[A-Z]{1,3}\d{3,}\b")
# an inline call example in a skill: `pcb_xxx {key: value, bare_key, ...}`
SNIPPET = re.compile(r"`(pcb_[a-z0-9_]+) \{([^`]*)\}`")
CODE_LITERAL = re.compile(r'code="([a-z_]+)"')
SRC = Path(__file__).resolve().parents[3] / "src" / "copperchat"


def _body(path: Path) -> str:
    return parse_front_matter(path.read_text())[1]


def test_session_board_complete() -> None:
    body = _body(SESSION_BOARD)
    assert "Full content: P9b." not in body
    positions = [body.index(step) for step in STEPS]
    assert positions == sorted(positions)
    for i, step in enumerate(STEPS):
        end = positions[i + 1] if i + 1 < len(STEPS) else body.index("## Errors")
        section = body[positions[i] : end]
        assert "**Do:**" in section, step
        assert "stop the turn" in section.lower(), step


def test_eight_questions() -> None:
    body = _body(SESSION_BOARD)
    intake = body[body.index("## S0 Intake") : body.index("## S1 ")]
    items = re.findall(r"^(\d)\. \*\*([^*]+)\*\*", intake, re.M)
    assert [int(n) for n, _ in items] == list(range(1, 9))
    keywords = (
        "size", "layer", "assembly", "edge", "mounting", "keepout", "current", "special nets"
    )  # fmt: skip
    for (_, title), keyword in zip(items, keywords, strict=True):
        assert keyword in title.lower(), (title, keyword)


def test_rules_present() -> None:
    body = _body(SESSION_BOARD)
    for phrase in (
        "45 %",
        "at most 3 images",
        "stop the turn and wait",
        "propose",
        '"Approve this placement?"',
        '"Approve this board for manufacturing?"',
        "Never call the board done with open connections",
        "only after the user accepted",
        "user_quote",
        "pcb_status` at the start of every turn",
        "`layers` and the matching `preset`",
        "never ground or plane-net items",
        "## Stack-up and rules",
        "`Assembly: <method>; sides: top|top+bottom`",
        "| DRC type | Items/refs | Reason | Accepted by |",
    ):
        assert phrase in body, phrase
    for rung in ("(a)", "(b)", "(c)", "(d)"):
        assert f"- **{rung}**" in body, rung
    # the current flow (round 2): floorplan preview, whole-board placement, footprint defects
    for phrase in ("`pcb_floorplan", "`pcb_place_blocks", "keep_channels", "`fp_check`"):
        assert phrase in body, phrase


def test_session_board_tools_exist() -> None:
    specs = registry.all_specs()
    known = {s.name for s in specs}
    board = {s.name for s in profile_specs("board", specs, board_tools=True)}
    for path in (SESSION_BOARD, BOARD_LAYOUT):
        mentioned = set(TOOL_NAME.findall(_body(path))) - NOT_TOOLS
        assert mentioned, path
        assert mentioned <= known, (path.parent.name, sorted(mentioned - known))
        assert mentioned <= board, (path.parent.name, sorted(mentioned - board))


def test_every_board_error_code_explained() -> None:
    errors = _body(SESSION_BOARD).split("## Errors", 1)[1]
    # the board tools' own codes and the plan gate every board write passes (spec §7.3)
    paths = [
        *(SRC / "kicad_tools" / "board").rglob("*.py"),
        *(SRC / "board").rglob("*.py"),
        SRC / "plans" / "gate.py",
    ]
    raised = {code for path in paths for code in CODE_LITERAL.findall(path.read_text())}
    assert raised, "no code= literal found"
    for code in sorted(set(BOARD_ERROR_CODES) | raised):
        assert f"`{code}`" in errors, code


def _top_level_keys(args: str) -> tuple[set[str], bool]:
    """The argument names of a call example (`key: value` or a bare `key`) and whether it
    is elided (`…`: required arguments may be left out)."""
    parts, depth, cur = [], 0, ""
    for ch in args:
        if ch in "[{<":
            depth += 1
        elif ch in "]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    keys: set[str] = set()
    elided = False
    for part in (p.strip() for p in parts):
        if part == "…":
            elided = True
        elif m := re.match(r"([a-z_]+)\s*(:|$)", part):
            keys.add(m.group(1))
    return keys, elided


def test_call_examples_match_the_schemas() -> None:
    """Every inline `pcb_* {…}` example names only real arguments and, unless elided with
    `…`, every required one."""
    specs = {s.name: s.input_schema for s in registry.all_specs()}
    checked = 0
    for path in (SESSION_BOARD, BOARD_LAYOUT, TOOLS):
        for name, args in SNIPPET.findall(_body(path)):
            schema = specs[name]
            keys, elided = _top_level_keys(args)
            props = set(schema.get("properties", {}))
            assert keys <= props, (path.parent.name, name, sorted(keys - props))
            if not elided:
                missing = set(schema.get("required", ())) - keys
                assert not missing, (path.parent.name, name, sorted(missing))
            checked += 1
    assert checked >= 20


def test_no_design_specific_names() -> None:
    for path in (SESSION_BOARD, BOARD_LAYOUT):
        text = path.read_text()
        assert not DESIGN_REF.findall(text), (path.parent.name, DESIGN_REF.findall(text))
        for name in ("OpenDriveHub", "ODH"):
            assert name not in text, (path.parent.name, name)


def test_board_layout_front_matter() -> None:
    meta, _ = parse_front_matter(BOARD_LAYOUT.read_text())
    assert meta["name"] == "board-layout"
    assert meta["description"].startswith("Rules for placing and routing a board by part class")
    assert meta["sessions"] == "board, free"


def test_board_layout_classes() -> None:
    body = _body(BOARD_LAYOUT)
    for cls in get_args(PartClass):
        if cls != "other":
            assert f"`{cls}`" in body, cls


def _hard_line(metric: str) -> str:
    """The owner's hard-threshold clause for one metric, verbatim from the index's Global
    Constraints (D2)."""
    text = INDEX.read_text()
    block = text[text.index("**Owner hard thresholds (D2)") :]
    block = " ".join(block[: block.index("\n- Chamfer/jog geometry")].split())
    clause = re.search(rf"{metric} [^;]*?→ fail", block)
    assert clause, metric
    return clause.group(0)


def test_board_layout_hard_thresholds_verbatim() -> None:
    body = " ".join(_body(BOARD_LAYOUT).split())
    for metric in ("M5", "M7", "M8"):
        assert _hard_line(metric) in body, metric
    # M4's D2 clause minus the T-junction part the owner reversed on 2026-11-26 (spec §22)
    m4 = _hard_line("M4")
    assert m4.startswith("M4 any segment off 0/45/90/135/… by more than ±0.5°, any 90° or acute")
    assert f"{m4.split(', any off-pad', 1)[0]}" in body
    assert "M15 any Y-junction → fail" in body
    assert "M16 any dogleg between adjacent pads, pad-entry loop or pad-entry jog → fail" in body
    assert "Hard thresholds change only with the owner's OK" in body


def test_board_layout_definitions() -> None:
    body = _body(BOARD_LAYOUT)
    assert "`max(1.0 \u00d7 width, 0.25 mm)`" in body  # the spec's multiplication sign
    assert "`2 \u00d7 width`" in body
    assert "Pad entries are not bends" in body
    assert "no via-in-pad" in body


def test_copperchat_tools_lists_board_tools() -> None:
    body = _body(TOOLS)
    start = body.index("## Board")
    board = body[start : body.index("\n## ", start + 1)]
    registered = {s.name for s in registry.all_specs()} & (PCB_READ | PCB_WRITES)
    assert registered == PCB_READ | PCB_WRITES
    for name in sorted(registered):
        assert re.search(rf"\b{name}\b", board), name
    assert "`session-board`" in board and "`board-layout`" in board


def test_fp_writes_in_board_sentence() -> None:
    body = " ".join(_body(TOOLS).split())
    assert "exist only in Schematic, Board and Free sessions" in body
    assert "exist only in Schematic and Free sessions" not in body


def test_readme_lists_board_env() -> None:
    readme = (REPO / "README.md").read_text()
    env = (REPO / ".env.example").read_text()
    for var in ("COPPERCHAT_BOARD_TOOLS", "COPPERCHAT_FREEROUTING", "COPPERCHAT_FREEROUTING_HOME"):
        assert f"`{var}`" in readme, var
        assert var in env, var
    assert "# COPPERCHAT_BOARD_TOOLS=on" in env
    assert "COPPERCHAT_BOARD_TOOLS=off" not in env
