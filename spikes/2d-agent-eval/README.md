# Spike 2D — agent tool-shape eval

Throwaway. Not run in CI. Findings: `docs/spikes/2d-agent-eval.md`.

Can the model build the three spec §12 circuits from an empty, KiCad-created
project with high-level editing tools only? Prototype tools + a draft skill, driven by
the real model through the Claude Agent SDK, set up like the backend's main agent
(`tools=["Skill"]`, MCP tools only, guard, skills as a local plugin, `setting_sources=[]`).

Runs inside the pinned base image
`ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff`
(override with `KICAD_IMAGE`); `./run.sh CMD…` mounts the repository at `/repo` because
the spike reuses `spikes/2a-sch-write` (`harness.py`, `geom.py`), `spikes/2b-libraries`
(`libdata.py`, `index.py`) and the built-in `kicad-schematic-style` skill.

1. `uv pip install --target .deps --python 3.13 --python-platform x86_64-manylinux_2_28 claude-agent-sdk==0.2.168 pillow`
   (the SDK wheel bundles the `claude` CLI; `.deps` is gitignored and on `PYTHONPATH`).
2. `./run.sh python scripted.py` — no model: builds the three circuits with fixed tool
   calls and scores them (validates the prototype; `results/scripted/`).
3. `./run.sh python eval.py --scenario s1|s2|s3 --run N --tools 1|2 --tag v2- [--model …]` —
   one model run (credentials: `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` from the
   environment, else read from `~/copperchat/.env`; passed to docker by name only).
   Writes `results/runs/<scenario>-<tag>r<N>/` (`result.json`, `transcript.md`,
   `render-*.png` = what the agent saw, `final.png`).
4. `python3 summarize.py` — `results/summary.md` / `summary.json` (host, no docker).
5. `./run.sh python replay.py results/runs/<run>` — replays a run's tool calls without the
   model (reproduces tool failures; `--debug N` explains a `sch_add_power` call).

| File | What |
|---|---|
| `tools.py` | prototype tools v1: `lib_search_symbols`, `lib_get_symbol`, `lib_search_footprints`, `sch_list_symbols`, `sch_get_pin`, `sch_get_net`, `sch_place_symbol`, `sch_set_fields`, `sch_connect`, `sch_add_label`, `sch_add_power`, `sch_no_connect`, `run_erc`, `render_view`; read-back verification against the live netlist, compensation on mismatch |
| `tools_v2.py` | v2 additions: `sch_find_space`, `sch_delete`, `sch_move` (unconnected symbols only), pin directions per rotation |
| `geo.py` | scene model from API data, obstacle boxes, orthogonal router, collision checks |
| `kit.py` | blank project made by KiCad (`CreateDocument` + save), library loading, symbol index (2B prototype) |
| `scenarios.py` | the three prompts, golden connectivity checkers (by role, not by reference), s1 rail check, layout checks, scoring |
| `checks.py` | requested values + 0805 check from recorded components (no KiCad imports; used by `summarize.py`) |
| `eval.py` | Agent SDK harness (MCP server, guard, plugin, transcript, metrics) |
| `skill/v1`, `skill/v2` | draft `kicad-schematic-editing` skill as used by the v1 / v2 runs |

Tool-set versions: **v1** = `tools.py` + `skill/v1`; **v2** = + `tools_v2.py`, misuse guards
(no loose `power:` symbols, no labels named like a rail, `to=<power net>` places a power
symbol) + `skill/v2`; **v2.1** = v2 + geometry fixes made after the v2 runs (one obstacle
box per field instead of a union box, the new part's own pin corridors kept free in
`sch_find_space`, upright power symbols preferred, multi-pin `sch_add_power`
all-or-nothing). The code in this directory is v2.1; the v1/v2 runs were recorded with
the coarser geometry, so `replay.py` of a v1/v2 run can now differ ("differs from run").

Nothing here hand-edits or generates KiCad files: all writes go through the IPC API, all
checks through the live netlist and `kicad-cli` (ERC, PNG export).
