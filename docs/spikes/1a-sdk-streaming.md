# Spike 1A — Python Agent SDK streaming input

Status: **pending real runs** (harness and scripts done 2026-10-16; no model
token available yet). Code: `spikes/1a-sdk-streaming/`. CI does not run
spikes; running them needs `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`)
or `ANTHROPIC_API_KEY` in the environment — without it every script prints
`SKIP` and exits 0. CopperChat ships no login flow; neither does this spike.

## Question(s)
(a) lazy pull at turn boundaries · (b) interrupt mid-turn then continue ·
(c) resume + fork_session at burst start · (d) tools=[] + SDK MCP tool ·
(e) RateLimitEvent shape · (f) ResultMessage usage · (g) can_use_tool for MCP
tools · (h) sub-agent with own tools while main has none, incl. the owner's
sub-agent gate (h4: Agent guard by subagent_type, guard on sub-agent calls,
parent_tool_use_id; h5: interrupt + usage during a sub-agent).

## Setup
- claude-agent-sdk 0.2.168, bundled CLI 2.1.301, Python 3.13, model alias
  `haiku` (override with `SPIKE_MODEL`)
- Isolation: setting_sources=[], CLAUDE_CONFIG_DIR=spikes/1a-sdk-streaming/.claude-config
- Commands: `uv sync && uv run python run_all.py`

Type check against the installed SDK (no API call): every dataclass field the
scripts use exists in 0.2.168 — `ToolPermissionContext.agent_id`,
`StreamEvent.parent_tool_use_id`, `AssistantMessage.parent_tool_use_id` /
`.usage`, `ResultMessage.terminal_reason`, `RateLimitInfo.resets_at` / `.raw`,
`ClaudeAgentOptions.tools` / `forward_subagent_text` / `fork_session` /
`agents` / `can_use_tool` / `setting_sources`.

## Results
Not yet run. Each subsection is filled from `results/summary.md` and the
per-question logs after the real run.

### (a) lazy pull
Pending.

### (b) interrupt
Pending.

### (c) resume + fork_session
Pending.

### (d) tools=[] + SDK MCP
Pending.

### (e) RateLimitEvent
Pending.

### (f) ResultMessage usage
Pending.

### (g) can_use_tool for MCP tools
Pending.

### (h) sub-agent, incl. gate h4/h5
Pending.

## Decision
Pending — no decision is recorded until the real runs exist. The rules
(Task 1A.8 and Task 1A.7 Step 4) are applied mechanically to the PASS/FAIL
rows once available.

## Follow-ups for later phases
Pending the real run.
