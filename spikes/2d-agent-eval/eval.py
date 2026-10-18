"""Spike 2D agent eval: one scenario run with the real model via the Claude Agent SDK.

Setup mirrors the backend's main agent (sdk_backend.build_options): no built-in tools
except Skill, an in-process MCP server with only the prototype tools, a guard that allows
exactly those, skills passed as a local plugin, setting_sources=[]. One user message per
run (no follow-up turns); the agent works until it ends its turn.

Usage: ./run.sh python eval.py --scenario s1 --run 1 [--tools 1|2] [--tag v2-] [--model claude-opus-5-5] [--budget 4]
Writes results/runs/<scenario>-<tag>r<run>/{result.json,transcript.md,final.png,render-*.png}.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from harness import Server

import kit
import scenarios
import tools

HERE = Path(__file__).parent
SERVER = "copperchat"

SYSTEM_PROMPT = """You are CopperChat, an assistant for KiCad schematic and PCB design, running \
inside the self-hosted CopperChat web app (Powered by Claude).

You work on exactly one KiCad project: "{project}".
- You have no file, shell or web access. Everything you know about the design comes from your \
CopperChat tools, which query and edit the running KiCad API server.
- Never guess design data: call a tool, then answer from its result. Report coordinates in \
millimetres.
- You can edit the schematic with the sch_* tools. Read the kicad-schematic-editing skill before \
your first edit.
- Use render_view to look at the design when spatial reasoning matters, and run_erc to check it."""

PIN = {"type": "string", "description": "REF.PIN: reference + pin number or unique pin name, e.g. U1.3 or U1.VIN"}
PINS = {"oneOf": [PIN, {"type": "array", "items": PIN}]}
XY = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2, "description": "[x_mm, y_mm], y grows downwards"}

SPECS: dict[str, tuple[str, dict]] = {
    "lib_search_symbols": (
        "Search the symbol libraries (stock KiCad + project). Returns lib_id, description, keywords, unit and pin count, "
        "default footprint and footprint filters.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}, "library": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    ),
    "lib_get_symbol": (
        "Inspect one library symbol before placing it: pins (number, name, electrical type, offset from the origin at "
        "rotation 0, direction the pin points), units, body size, default footprint.",
        {"type": "object", "properties": {"lib_id": {"type": "string"}}, "required": ["lib_id"]},
    ),
    "lib_search_footprints": (
        "Search footprint names (all words must match), e.g. 'R 0805' or 'SOT-223'.",
        {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
    ),
    "sch_list_symbols": (
        "List the placed symbols: ref, lib_id, value, footprint, position, rotation, bounding box, unconnected pins.",
        {"type": "object", "properties": {"include_power": {"type": "boolean"}}},
    ),
    "sch_get_pin": (
        "Absolute position, direction, net, net members and attached items of one or more pins.",
        {"type": "object", "properties": {"pins": {"type": "array", "items": PIN}}, "required": ["pins"]},
    ),
    "sch_get_net": (
        "Members (REF.PIN) of a named net.",
        {"type": "object", "properties": {"net": {"type": "string"}}, "required": ["net"]},
    ),
    "sch_place_symbol": (
        "Place a library symbol. Reference auto-assigned when omitted. Refuses overlaps (body, pins, field text + "
        "1.27 mm) and suggests the nearest free position. Returns the placed pins with absolute positions and directions.",
        {
            "type": "object",
            "properties": {
                "lib_id": {"type": "string"},
                "at": XY,
                "rotation": {"type": "integer", "enum": [0, 90, 180, 270]},
                "mirror": {"type": "string", "enum": ["none", "x", "y"], "description": "y = mirror left/right, x = up/down"},
                "unit": {"type": "integer"},
                "reference": {"type": "string"},
                "value": {"type": "string"},
                "footprint": {"type": "string", "description": "lib:footprint, e.g. Resistor_SMD:R_0805_2012Metric"},
                "fields": {"type": "object", "description": "extra fields, e.g. {MPN: ..., Manufacturer: ...}"},
            },
            "required": ["lib_id", "at"],
        },
    ),
    "sch_set_fields": (
        "Change fields of a placed symbol (value, footprint, datasheet, MPN, any custom field) and DNP.",
        {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "fields": {"type": "object"}, "dnp": {"type": "boolean"}},
            "required": ["ref"],
        },
    ),
    "sch_connect": (
        "Connect two pins. via=auto: computed orthogonal wire route that never touches other pins/wires; falls back to "
        "net labels at both pins when no clean route exists (needs net_name unless a pin already has a named net). "
        "via=wire: wire or error. via=label: stub + label at both pins. 'to' may also be a net name (label at 'from'). "
        "Verified against KiCad's netlist.",
        {
            "type": "object",
            "properties": {
                "from": PIN,
                "to": {"type": "string", "description": "REF.PIN or a net name"},
                "via": {"type": "string", "enum": ["auto", "wire", "label"]},
                "net_name": {"type": "string"},
                "kind": {"type": "string", "enum": ["local", "global"]},
            },
            "required": ["from", "to"],
        },
    ),
    "sch_add_label": (
        "Net label at one or more pins (short stub + label; if the pin is already wired, at the free end of its wire). "
        "Pins with the same label name are connected.",
        {
            "type": "object",
            "properties": {"net": {"type": "string"}, "at": PINS, "kind": {"type": "string", "enum": ["local", "global"]}},
            "required": ["net", "at"],
        },
    ),
    "sch_add_power": (
        "Power symbol(s) from the power: library on one or more pins (stub + symbol, supplies up, GND down). "
        "net = GND, +3V3, +5V, VCC, … or PWR_FLAG (marks a net as driven, needed once per externally supplied rail).",
        {
            "type": "object",
            "properties": {"net": {"type": "string"}, "at": PINS, "symbol": {"type": "string", "description": "explicit lib_id, optional"}},
            "required": ["net", "at"],
        },
    ),
    "sch_no_connect": (
        "No-connect markers on unused pins.",
        {"type": "object", "properties": {"pins": {"type": "array", "items": PIN}}, "required": ["pins"]},
    ),
    "run_erc": (
        "Save and run KiCad's ERC. Returns error/warning counts and violations with positions.",
        {"type": "object", "properties": {}},
    ),
    "render_view": (
        "Render the schematic (KiCad's own plot) as an image with a mm ruler. Default: everything placed. Optional "
        "around=[refs] (+margin mm) or region=[x0,y0,x1,y1] mm.",
        {
            "type": "object",
            "properties": {
                "around": {"type": "array", "items": {"type": "string"}},
                "margin": {"type": "number"},
                "region": {"type": "array", "items": {"type": "number"}},
            },
        },
    ),
}


V2_SPECS: dict[str, tuple[str, dict]] = {
    "sch_find_space": (
        "Free position for a part (lib_id at rotation) close to an anchor: near=REF.PIN (the part goes on the side the "
        "pin points to), a reference, or [x, y]. Keeps clearance for stubs, labels and power symbols.",
        {
            "type": "object",
            "properties": {
                "lib_id": {"type": "string"},
                "near": {"oneOf": [{"type": "string"}, XY]},
                "rotation": {"type": "integer", "enum": [0, 90, 180, 270]},
                "clearance": {"type": "number"},
            },
            "required": ["lib_id", "near"],
        },
    ),
    "sch_delete": (
        "Delete symbols by reference, together with the wires, labels, power symbols, junctions and no-connects that only "
        "served their pins (with_attached=true, default). Reports nets that changed.",
        {
            "type": "object",
            "properties": {"refs": {"type": "array", "items": {"type": "string"}}, "with_attached": {"type": "boolean"}},
            "required": ["refs"],
        },
    ),
    "sch_move": (
        "Move / rotate / mirror a symbol whose pins are not connected yet (prototype limit). Fields move with it.",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "to": XY,
                "rotation": {"type": "integer", "enum": [0, 90, 180, 270]},
                "mirror": {"type": "string", "enum": ["none", "x", "y"]},
            },
            "required": ["ref"],
        },
    ),
}


def build_server(ctx: tools.Ctx, specs: dict):
    lock = asyncio.Lock()

    def make(name: str, desc: str, schema: dict):
        @tool(name, desc, schema)
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            async with lock:  # one KiCad client; calls are serial anyway
                if name == "render_view":
                    t0 = time.monotonic()
                    try:
                        png, meta = await asyncio.to_thread(tools.render_view, ctx, args)
                    except Exception as e:  # noqa: BLE001
                        ctx.calls.append({"tool": name, "args": args, "ok": False, "ms": 0, "item_changes": 0, "error": str(e)})
                        return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "is_error": True}
                    ctx.calls.append(
                        {
                            "tool": name,
                            "args": args,
                            "ok": True,
                            "ms": round((time.monotonic() - t0) * 1000),
                            "item_changes": 0,
                            "error": None,
                        }
                    )
                    return {
                        "content": [
                            {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"},
                            {"type": "text", "text": json.dumps(meta)},
                        ]
                    }
                res = await asyncio.to_thread(tools.call, ctx, name, args)
                return {"content": [{"type": "text", "text": json.dumps(res)}], "is_error": "error" in res}

        return handler

    return create_sdk_mcp_server(SERVER, tools=[make(n, d, s) for n, (d, s) in specs.items()])


def build_plugin(dest: Path, version: int) -> list[str]:
    shutil.rmtree(dest, ignore_errors=True)
    (dest / ".claude-plugin").mkdir(parents=True)
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": SERVER, "version": "1.0.0", "description": "spike 2D skills"}))
    shutil.copytree(HERE / "skill" / f"v{version}" / "kicad-schematic-editing", dest / "skills" / "kicad-schematic-editing")
    style = HERE.parent.parent / "backend/src/copperchat/skills/builtin/kicad-schematic-style"
    shutil.copytree(style, dest / "skills" / "kicad-schematic-style")
    return [f"{SERVER}:{p.name}" for p in (dest / "skills").iterdir()]


def short(obj, n=300) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n})"


async def run(scenario: str, run_no: int, model: str | None, budget: float, max_turns: int, version: int, tag: str) -> dict:
    out = HERE / "results" / "runs" / f"{scenario}-{tag}r{run_no}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    proj = kit.fresh_blank()
    transcript: list[str] = [f"# {scenario} run {run_no}\n", f"**Prompt:** {scenarios.PROMPTS[scenario]}\n"]
    with Server(proj) as srv:
        k = srv.client(timeout_ms=120000)
        kit.load_libraries(k)
        ctx = tools.Ctx(k=k, sch=k.get_schematic(), lib=kit.Library(k), project_dir=proj, out_dir=out, version=version)
        specs = SPECS | (V2_SPECS if version >= 2 else {})
        allowed = {f"mcp__{SERVER}__{n}" for n in specs}

        async def guard(name, inp, _c):
            if name in allowed or name == "Skill":
                return PermissionResultAllow(updated_input=inp)
            return PermissionResultDeny(message=f"Tool '{name}' is not available in CopperChat.", interrupt=False)

        cfg = Path("/tmp/claude-config")
        cfg.mkdir(exist_ok=True)
        skills = build_plugin(kit.WORK / "plugin", version)
        opts = ClaudeAgentOptions(
            tools=["Skill"],
            allowed_tools=[],
            mcp_servers={SERVER: build_server(ctx, specs)},
            system_prompt=SYSTEM_PROMPT.format(project=proj.name),
            permission_mode="default",
            can_use_tool=guard,
            cwd=str(proj),
            env={"CLAUDE_CONFIG_DIR": str(cfg), "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
            setting_sources=[],
            plugins=[{"type": "local", "path": str(kit.WORK / "plugin")}],
            skills=skills,
            max_turns=max_turns,
            max_budget_usd=budget,
            model=model,
        )

        async def prompt():
            yield {"type": "user", "message": {"role": "user", "content": scenarios.PROMPTS[scenario]}, "parent_tool_use_id": None}

        t0 = time.monotonic()
        result: ResultMessage | None = None
        init_model, skill_calls, tool_uses = None, 0, 0
        async for msg in query(prompt=prompt(), options=opts):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                init_model = msg.data.get("model")
                transcript.append(f"_init: model={init_model}, tools={len(msg.data.get('tools', []))}, skills={msg.data.get('skills')}_\n")
            elif isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        transcript.append(f"**assistant:** {b.text.strip()}\n")
                    elif isinstance(b, ThinkingBlock):
                        transcript.append(f"_thinking: {short(b.thinking, 400)}_\n")
                    elif isinstance(b, ToolUseBlock):
                        tool_uses += 1
                        skill_calls += b.name == "Skill"
                        transcript.append(f"→ `{b.name.removeprefix(f'mcp__{SERVER}__')}` {short(b.input, 400)}\n")
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for b in msg.content:
                    c = getattr(b, "content", None)
                    if c is None:
                        continue
                    text = (
                        c
                        if isinstance(c, str)
                        else " ".join(x.get("text", "[image]") if isinstance(x, dict) and x.get("type") == "text" else "[image]" for x in c)
                    )
                    transcript.append(f"  ← {'ERROR ' if getattr(b, 'is_error', False) else ''}{short(text, 500)}\n")
            elif isinstance(msg, ResultMessage):
                result = msg
        wall = round(time.monotonic() - t0, 1)
        tools.render_png(ctx, name="final.png")
        sc = scenarios.score(scenario, ctx)
    usage = (result.usage or {}) if result else {}
    summary = {
        "scenario": scenario,
        "run": run_no,
        "tools_version": version,
        "model": init_model,
        "model_usage": getattr(result, "model_usage", None),
        "stop": getattr(result, "subtype", None),
        "num_turns": getattr(result, "num_turns", None),
        "cost_usd": getattr(result, "total_cost_usd", None),
        "usage": usage,
        "wall_s": wall,
        "tool_calls": len(ctx.calls),
        "tool_errors": sum(1 for c in ctx.calls if not c["ok"]),
        "skill_calls": skill_calls,
        "renders": ctx.renders,
        "erc_calls": sum(1 for c in ctx.calls if c["tool"] == "run_erc"),
        "by_tool": {n: sum(1 for c in ctx.calls if c["tool"] == n) for n in sorted({c["tool"] for c in ctx.calls})},
        "errors": [f"{c['tool']}: {c['error']}" for c in ctx.calls if not c["ok"]],
        "item_changes_total": ctx.item_changes,
        "item_changes_per_call": [c["item_changes"] for c in ctx.calls if c["tool"] in tools.MUTATING],
        "score": sc,
        "success": sc["golden_ok"] and sc["erc_errors"] == 0,
        "calls": ctx.calls,
    }
    (out / "result.json").write_text(json.dumps(summary, indent=1, default=str))
    transcript.append(
        f"\n---\n**result:** {json.dumps({k_: summary[k_] for k_ in ('success', 'cost_usd', 'tool_calls', 'tool_errors', 'stop')})}\n"
    )
    if result is not None and result.result:
        transcript.append(f"\n**final answer:**\n\n{result.result}\n")
    (out / "transcript.md").write_text("\n".join(transcript))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=sorted(scenarios.PROMPTS))
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--model", default=None)
    ap.add_argument("--budget", type=float, default=4.0)
    ap.add_argument("--max-turns", type=int, default=120)
    ap.add_argument("--tools", type=int, default=2, choices=[1, 2], help="tool set + skill version")
    ap.add_argument("--tag", default="", help="run-directory prefix, e.g. 'v2-' or 'opus-'")
    a = ap.parse_args()
    s = asyncio.run(run(a.scenario, a.run, a.model, a.budget, a.max_turns, a.tools, a.tag))
    print(
        json.dumps(
            {
                k: s[k]
                for k in (
                    "scenario",
                    "run",
                    "model",
                    "success",
                    "cost_usd",
                    "tool_calls",
                    "tool_errors",
                    "wall_s",
                    "stop",
                    "item_changes_total",
                )
            }
        )
    )
    print(json.dumps(s["score"]["golden"]), "erc_errors", s["score"]["erc_errors"])


if __name__ == "__main__":
    main()
