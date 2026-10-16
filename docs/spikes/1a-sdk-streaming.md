# Spike 1A — Python Agent SDK streaming input

Status: done (2026-10-16). Code: `spikes/1a-sdk-streaming/`. CI does not run
spikes; re-running needs `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`)
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
- claude-agent-sdk 0.2.168, bundled CLI 2.1.301, Python 3.14.3 (uv's pick for
  `requires-python >=3.13`), model alias `haiku` (resolved to
  `claude-haiku-5-5`; main agent and sub-agents both)
- Isolation: setting_sources=[], CLAUDE_CONFIG_DIR=spikes/1a-sdk-streaming/.claude-config
- Commands: `uv sync && uv run python run_all.py` (the real run was done
  script by script with the same scripts, because one shell call is capped at
  10 min; `results/summary.md` was written with `run_all.py`'s summary code)

### Changes to the plan's scripts (made during the run)
The plan's code was used as written for (a)–(g). For (h) the real runs
exposed two script artifacts and two behaviours the plan did not foresee;
`q_h_subagent.py` was changed accordingly (the evidence from the earlier runs
is quoted below; four runs in total):

1. **Delegation tool name.** With `tools=["Agent"]` the init tool list reports
   `"Task"`, while the model's `ToolUseBlock` is named `"Agent"`. The plan's
   fallback switched `delegate` to `"Task"`, so h4's guard compared against
   the wrong name. Calls are now matched by `{"Agent", "Task"}`; `tools=`
   keeps `"Agent"`.
2. **h4 helper tools.** The plan gave the h4 helper `["mcp__spike__get_magic",
   "WebFetch"]`. A built-in that is not in the session `tools` makes the
   helper refuse to start ("tool list didn't resolve", see h3), so in run 1
   no sub-agent ran at all and every h4 row was empty. The h4 helper now gets
   only the in-session MCP tool (as h5 already did).
3. **Background sub-agents.** In runs 1–2 the model sometimes launched the
   sub-agent asynchronously (tool result `"Async agent launched
   successfully…"`, task events `task_started` / `background_tasks_changed`).
   The main turn then ends with a ResultMessage before the sub-agent has
   finished ("I'll reply with the value once its result comes back"), which
   failed h2 in run 2 and scrambled h5 there. All h runs now set
   `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` in the CLI env (see (h) foreground
   probe).
4. **Guard mechanism.** `can_use_tool` is never invoked for `Agent` (runs 1–4
   and a separate diagnostic run, also with `allowed_tools=[]`). The plan
   names a `PreToolUse` hook as the alternative ("record which one works"),
   so h4 now runs twice: as planned (`can_use_tool` only, rows without suffix)
   and with a `PreToolUse` hook (matcher `"Agent"`) guarding delegations plus
   `can_use_tool` guarding every other call (rows with suffix `_hook`).
5. **Added rows.** `h3b_*` (researcher built-ins in the session `tools`, main
   agent denied by the session guard — the precondition of the "built-ins in
   session" gate outcome); h3 counts a built-in use only when its parent is a
   `helper` delegation (run 3 had a false PASS from the model's fallback to
   `general-purpose`); h5's usage note compares exact sums.

## Results

Full table: `results/summary.md` (gitignored; pasted into the PR body). Rows
quoted below are from the final run of each script.

### (a) lazy pull

| Check | Status |
|---|---|
| a1_pull_after_previous_result | FAIL |
| a1_one_result_per_item | FAIL |
| a1_turn_i_answers_item_i | FAIL |
| a2_second_item_gets_own_turn | FAIL |
| a2_turn1_not_absorbing_item2 | FAIL |
| a3_gated_pull_after_result | PASS |
| a3_gated_one_turn_per_item | PASS |

```
[   2.116] a1 generator: yielding item 0
[   2.116] a1 generator: yielding item 1
[   2.116] a1 generator: yielding item 2
[   3.872] ResultMessage #1 subtype=success terminal_reason=completed
[   4.897] ResultMessage #2 subtype=success terminal_reason=completed
```
- a1: the SDK pulls all three pre-queued items at once (2.116 s). The CLI
  answered with **two** turns for three items: texts `["TOKEN-0", "TOKEN-2"]`
  — items 1 and 2 were merged into one turn and only the last was answered.
- a2: an item pushed while the turn's MCP tool runs (7.894 s) is **absorbed
  into the running turn**: one ResultMessage (16.862 s), text `"TOKEN-B"`
  only; the `echo:alpha` answer was dropped.
- a3: gated generator (yield `i+1` only after ResultMessage `i`): pulls
  `[57.51, 59.829, 61.212]`, results `[59.829, 61.212, 62.748]`, texts
  `GATED-0/1/2` — one clean turn per item.

### (b) interrupt

All 6 checks PASS (`b1_text_*`, `b2_in_tool_*`).

| Case | first ResultMessage after interrupt() | latency | session id kept |
|---|---|---|---|
| b1 text streaming | `subtype="error_during_execution"`, `is_error=True`, `stop_reason=None`, `terminal_reason="aborted_streaming"` | 0.017 s | yes |
| b2 inside MCP tool | `subtype="error_during_execution"`, `is_error=True`, `stop_reason="tool_use"`, `terminal_reason="aborted_tools"` | 0.019 s | yes |

The next queued item runs normally on the same client (`AFTER-INTERRUPT`).
The in-process MCP tool coroutine **is cancelled** (`CancelledError` seen,
`b2_tool_coroutine_cancelled = true`).

### (c) resume + fork_session

All 6 checks PASS: resume remembers, `fork_session=True` yields a new session
id each time, forked branches are isolated, resuming a fork works. Session
files: `<CLAUDE_CONFIG_DIR>/projects/<cwd slug>/<session_id>.jsonl`, the slug
being the absolute `cwd` with `/` replaced by `-`, e.g.
`projects/-home-…-spikes-1a-sdk-streaming-work/ac62d148-….jsonl`.

### (d) tools=[] + SDK MCP

All 4 checks PASS. Init `tools` for `tools=[]` + one SDK MCP server:
`["mcp__spike__get_magic"]` (exactly). Asked to run `ls /`, the model replied
that it has no shell tool and used only the MCP tool.

### (e) RateLimitEvent

`e1_rate_limit_event_observed` PASS (3 events in a 2-turn run — they arrive in
normal operation, not only on status changes), `e2_status_field` PASS.
Literal shape (`jsonable(RateLimitEvent)`):
```json
{"rate_limit_info": {"status": "allowed", "resets_at": 1792181400,
  "rate_limit_type": "five_hour", "utilization": null,
  "overage_status": "rejected", "overage_resets_at": null,
  "overage_disabled_reason": "out_of_credits",
  "raw": {"status": "allowed", "resetsAt": 1792181400, "rateLimitType": "five_hour",
          "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
          "isUsingOverage": false,
          "unifiedWindows": {"five_hour": {"utilization": 0.16, "resetsAt": 1792181400},
                             "seven_day": {"utilization": 0.15, "resetsAt": 1792522800}}}},
 "uuid": "…", "session_id": "…"}
```
`resets_at` is `int` Unix seconds. `RateLimitInfo.utilization` was `null`;
per-window utilization exists only in `raw.unifiedWindows`.

### (f) ResultMessage usage

`f1_two_results`, `f2_usage_keys_turn0/1` PASS. `ResultMessage.usage` keys:
`input_tokens, output_tokens, cache_read_input_tokens,
cache_creation_input_tokens, cache_creation{ephemeral_1h_input_tokens,
ephemeral_5m_input_tokens}, output_tokens_details{thinking_tokens},
server_tool_use{web_search_requests, web_fetch_requests}, service_tier,
inference_geo, iterations[], speed, fallback_credit`.
- `usage` is **per turn** (turn 0: input 2 / cache_creation 669 / output 5;
  turn 1: input 2 / cache_creation 710 / output 5).
- `total_cost_usd` is **cumulative across turns of one client session**:
  turn 0 `0.0002619`, turn 1 `0.0004066`.
- `model_usage` is **cumulative per client** as well: turn 0
  `{inputTokens 1176, cacheCreationInputTokens 669, costUSD 0.0002619}`,
  turn 1 `{inputTokens 1178, cacheCreationInputTokens 1379 (= 669 + 710),
  costUSD 0.0004066}` — each turn adds its own `usage` (input +2, cache
  creation +710); its `costUSD` equals the cumulative `total_cost_usd`. About
  1174 `inputTokens` appear once per client, outside any turn's `usage`
  (fixed per-client overhead), which also accounts for h5's 1239.
- `StreamEvent` `message_start` / `message_delta` carry the same usage dicts
  per API message.

### (g) can_use_tool for MCP tools

All 4 checks PASS. `can_use_tool` is called for in-process MCP tools with the
full input (`("mcp__spike__deny_me", {"path": "/etc/passwd"})`); a
`PermissionResultDeny(message=…)` reaches the model as a `ToolResultBlock`
with `is_error=True` and the message text. **Tools listed in `allowed_tools`
bypass `can_use_tool`** (g4: with `allowed_tools=["mcp__spike__get_magic"]`
the callback saw only `deny_me`).

### (h) sub-agent, incl. gate h4/h5

Final run (run 4):

| Check | Status |
|---|---|
| h1_helper_reachable_with_tools_empty | FAIL |
| h2_main_has_only_delegate_and_mcp | PASS |
| h2_helper_used_mcp_tool | PASS |
| h2_answer_contains_magic | PASS |
| h3_main_lacks_webfetch_write | PASS |
| h3_helper_used_webfetch | FAIL |
| h3_guard_saw_helper_write | FAIL |
| h3_inside_write_ok | FAIL |
| h3_outside_write_denied | PASS (vacuous: nothing was written) |
| h3b_main_builtin_call_denied | PASS |
| h3b_helper_used_webfetch | PASS |
| h3b_guard_saw_helper_write_with_agent_id | PASS |
| h3b_inside_write_ok | PASS |
| h3b_outside_write_denied | PASS |
| h4a_guard_sees_agent_with_subagent_type | FAIL |
| h4a_general_purpose_denied_by_guard | FAIL |
| h4b_guard_fires_for_subagent_tools | PASS |
| h4b_agent_id_distinguishes_main_and_sub | FAIL |
| h4c_subagent_messages_carry_parent_id | PASS |
| h4c_no_unparented_deltas_during_subagent | PASS |
| h4a_guard_sees_agent_with_subagent_type_hook | PASS |
| h4a_general_purpose_denied_by_guard_hook | PASS |
| h4b_guard_fires_for_subagent_tools_hook | PASS |
| h4b_agent_id_distinguishes_main_and_sub_hook | PASS |
| h4c_subagent_messages_carry_parent_id_hook | PASS |
| h4c_no_unparented_deltas_during_subagent_hook | PASS |
| h5_interrupt_ends_turn_during_subagent | PASS |
| h5_no_subagent_messages_after_result | PASS |
| h5_next_turn_works | PASS |

Evidence:
- **h1:** with `tools=[]` there is no delegation tool; init `agents` still
  lists `["claude", "Explore", "general-purpose", "helper", "Plan",
  "statusline-setup"]`. The model called the MCP tool itself.
- **h2 / delegation name:** `tools=["Agent"]` → init tools `["Task",
  "mcp__spike__get_magic"]`; the model's call is
  `ToolUseBlock(name="Agent", input={"description", "prompt",
  "subagent_type": "helper"[, "run_in_background"]})`. Main has no other
  built-in.
- **h3 (all four runs):** a helper whose `tools` include built-ins that are not
  in the session `tools` **does not start**: "the helper sub-agent refused to
  start because its tools (WebFetch, Write) didn't resolve". Sub-agents only
  get built-ins that are in the session `tools`.
- **h3b:** session `tools=["Agent", "WebFetch", "Write"]`, `allowed_tools=[]`,
  `can_use_tool` session guard. Guard calls (run 4):
  `WebFetch agent_id=None → deny` (main agent's own attempt),
  `WebFetch agent_id="a9107a…" → allow`, `Write agent_id="a9107a…"
  file_path=…/docs/parts/TEST2/notes.md → allow`, `Write … outside2.md → deny`.
  `notes.md` exists, `outside2.md` does not. Init tools: `["Task",
  "WebFetch", "Write", "mcp__spike__get_magic"]`.
- **h4 (`can_use_tool` only):** the callback saw only the sub-agent's
  `mcp__spike__get_magic` calls (`agent_id` set, e.g. `"abce23b1aa546be98"`)
  and **never an `Agent` call** — so it cannot see `subagent_type` and cannot
  deny `general-purpose`. In runs 2 and 4 the unguarded `general-purpose`
  delegation actually ran (run 2: it reported having no fetch tool; run 4: two
  distinct `agent_id`s call `mcp__spike__get_magic`, i.e. the built-in
  `general-purpose` agent inherited the session's MCP tool). Confirmed
  separately with `tools=["Agent","Skill"]` and `["Task","Skill"]`,
  `allowed_tools=[]`. Consequence: with built-ins in the session, any
  sub-agent that gets past the delegation guard also gets the session's
  built-ins and MCP tools, and a `can_use_tool` guard keyed only on
  `agent_id is not None` allows them — the delegation hook is the **sole**
  `subagent_type` barrier unless the per-call guard also checks the
  sub-agent's type (hook input carries `agent_type` inside sub-agents per the
  SDK's `_SubagentContextMixin`; not measured here).
- **h4 (`_hook`: PreToolUse hook on `Agent` + `can_use_tool` for the rest):**
  calls `[{"name": "Agent", "subagent_type": "general-purpose", "agent_id":
  null} → deny, {"name": "Agent", "subagent_type": "helper", "agent_id":
  null} → allow, {"name": "mcp__spike__get_magic", "agent_id":
  "aa488f60285311a98"} → allow]` (run 4); answer contains the magic value. The hook input
  carries `tool_name`, `tool_input` and `agent_id` (absent for the main
  agent). A hook that returns `{}` defers to `can_use_tool`; **without** a
  `can_use_tool` callback (run 2's hook-only variant) the sub-agent's MCP call
  was refused for lack of a permission grant.
- **h4c:** all sub-agent `AssistantMessage` / `UserMessage` carry
  `parent_tool_use_id` = the `Agent` `ToolUseBlock.id`. With
  `include_partial_messages=True` and `forward_subagent_text=True`, **0
  sub-agent `StreamEvent`s** arrived (sub-agent text comes as complete
  messages, not deltas), and 0 unparented deltas arrived while a delegation
  was open.
- **h5:** interrupt on the first sub-agent message → ResultMessage
  `subtype="error_during_execution", is_error=True` (main `num_turns=3`); no
  sub-agent message after it; the next queued item answers `AFTER` on the same
  session.
- **h5 usage** (hook run, run 4): `ResultMessage.usage` = `{input 6, output
  499, cache_read 19101, cache_creation 680}`; sum of main-agent
  AssistantMessage usage (per `message_id`) = `{6, 21, 19101, 680}`; sum of
  sub-agent usage = `{4, 32, 2413, 1048}`. The script's flags
  `result_equals_main_only` and `result_equals_main_plus_sub` are both
  `false`: `usage`'s input/cache figures equal the main-agent sum, but its
  output (499) matches neither sum (21 main, 32 sub; per-block
  AssistantMessage usage seems to hold a streaming snapshot — unverified).
  `model_usage["claude-haiku-5-5"]` = `{inputTokens 1239, outputTokens 563,
  cacheReadInputTokens 21514 (= 19101 + 2413), cacheCreationInputTokens 1728
  (= 680 + 1048), costUSD 0.00088754}`: its cache figures equal main + sub
  exactly, but `inputTokens` 1239 ≫ 10 (main + sub) — `model_usage` is a
  **superset** that includes the sub-agent plus calls not visible as
  messages — consistent with the ~1174-token per-client overhead seen in
  (f), since `model_usage` is cumulative per client. Reading: `usage` ≈ main
  agent only for the turn; `model_usage` / `total_cost_usd` are cumulative
  per client and include the sub-agent and that overhead. Summing sub-agent
  `AssistantMessage.usage` is unreliable (output tokens are streaming
  snapshots).
- **Foreground probe** (ad-hoc, `results/h_foreground_probe.log`, 2 runs per
  config, hook on `Agent` + `can_use_tool`): baseline, `AgentDefinition
  (background=False)`, hook `updatedInput {"run_in_background": false}`, and
  env `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` all stayed in the foreground in
  these 8 runs, including the baseline — the async launch is intermittent
  and the probe could not reproduce it, so it **does not prove** any switch
  works. The model passed `run_in_background: false` in baseline and
  `background=False` runs and omitted it in the env-switch and `hook_update`
  runs (the hook records the input before its own update). The async launches
  of runs 1–2 happened when the model omitted the parameter without the env
  switch. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` is therefore a
  **mitigation, not a proven fix**; run 3 and 4 (with it) stayed foreground.
  The probe was an ad-hoc script, not committed.
- **Skill `allowed-tools` bypass:** not measured (informational; Phase 8
  strips the key anyway).
- **Known check weaknesses (review round 1, not re-run):** h5's interrupt
  rows do not assert the first result's `subtype` or that it arrived after
  the interrupt (the run-4 evidence shows `error_during_execution` with
  `is_error=True`, quoted above); h3b's `helper_used_webfetch` accepts any
  sub-agent with a helper parent, but `h3b_guard_calls` attribute the calls
  to one `agent_id`; `work/` is not cleaned inside the script (each final run
  was preceded by `rm -rf work`); h2's tool-list row also passes when the
  delegation tool is absent (the init list shows `"Task"`, so it was present).

## Decision

**1A partial: gated generator** — a1 and a2 FAIL (the CLI merges queued
stdin items into the running or next turn), a3 PASS, (b) PASS (session usable
after interrupt). Phase 5 keeps one long-lived `ClaudeSDKClient` per burst,
and the push-stream generator yields the next item only after the engine has
observed the previous `ResultMessage` (turn gate, `PushStream.release_next()`).
Queue semantics for the client are unchanged. Never push into the generator
mid-turn (a2: the item is absorbed into the running turn).

Sub-agent gate, applied mechanically: **sub-agent no-go** — h4a (both rows)
and h4b `agent_id_distinguishes_main_and_sub` FAIL, because `can_use_tool` is
never invoked for `Agent`; owner decision item 3(a) requires exactly that.
Under this rule Phase 8 uses fallback Task 8.6b.

**Proposed alternative — pending owner confirmation:** "go (built-ins in
session)" with the `Agent` delegation guard implemented as a **`PreToolUse`
hook** (matcher `"Agent"`) and `can_use_tool` for every other call. With
that mechanism every gate row passes: h4a/h4b/h4c `_hook` PASS, h5 interrupt
rows PASS, h3b PASS (session guard denies the researcher's built-ins for the
main agent and path-guards the researcher); h3 `helper_used_webfetch` FAIL
selects the "built-ins in session" variant. The plan's own hook clause
(Task 1A.7 Step 4) is worded for the helper's `Write`, not for `Agent`, so
extending it to the delegation guard is a decision for the owner, not for this
spike. Until the owner confirms, X20 items (1)–(2) are marked pending; Phase 5
is unaffected (it only needs the 1A decision above and X20 (3)–(8)).

Consequences per question for Phases 5/8:
- (b) map `ResultMessage.subtype == "error_during_execution"` with
  `terminal_reason in {"aborted_streaming", "aborted_tools"}` after our own
  `interrupt()` to `run-finished{reason: "interrupted"}`; MCP tool handlers
  must tolerate `CancelledError`.
- (f) per-run cost = delta of `total_cost_usd` between consecutive
  ResultMessages of one client; tokens from `model_usage` (sums over models)
  so sub-agent tokens are included.
- (g)/(h) never list a guarded tool in `allowed_tools`; `Agent` is guarded by
  the hook, everything else by `can_use_tool` (both, always).

## Follow-ups for later phases
- **Streaming input (Phase 5):** `await client.connect(prompt=<async gen>)`,
  `client.receive_messages()`, `await client.interrupt()`. The generator must
  be gated on ResultMessage (a3). Interrupt → ResultMessage within ~20 ms,
  `subtype="error_during_execution"`, `is_error=True`, `terminal_reason`
  `"aborted_streaming"` (text) or `"aborted_tools"` (in a tool); same
  `session_id` afterwards.
- **Session store (Phase 5/9):** `<CLAUDE_CONFIG_DIR>/projects/<cwd with "/"
  → "-">/<session_id>.jsonl`; the slug depends on `cwd`, so `cwd` must stay
  stable per project (Phase 5 uses the project dir) for `resume` to find the
  file. `resume=<id>, fork_session=True` → new id per burst, history kept.
- **Init tool list (Phase 5 adapter test):** `tools=[]` + CopperChat MCP →
  exactly the `mcp__<server>__*` names. With `tools=["Agent", …]` the init
  list reports **`"Task"`** for the delegation tool, while `ToolUseBlock.name`
  and the hook `tool_name` are **`"Agent"`** → `DELEGATION_TOOL = "Agent"`,
  but adapter tests that assert the init list must expect `"Task"`.
- **RateLimitInfo (Phase 5):** `status` (`"allowed"` observed),
  `resets_at: int` Unix seconds, `rate_limit_type` (`"five_hour"`),
  `utilization` may be `None` (use `raw["unifiedWindows"][…]["utilization"]`),
  `overage_*`. Events arrive in normal operation (several per run).
- **Usage (Phase 5, resolves Task 5.11b's usage branch):** `ResultMessage.usage`
  is per turn and covers (approximately) the **main agent only** — the
  "does not include sub-agent tokens" case. Do **not** sum sub-agent
  `AssistantMessage.usage` (unreliable, see h5); keep `_add_subagent_usage` a
  no-op. Instead compute the turn's `Usage` (tokens and cost, incl.
  sub-agent) as the delta of `model_usage` summed over models, and of
  `total_cost_usd`, against the previous `ResultMessage` of the same client
  (first turn of a client: the absolute values, which include the
  per-client overhead); `ResultMessage.model_usage[<model>]`
  (`inputTokens, outputTokens, cacheReadInputTokens,
  cacheCreationInputTokens, costUSD`) and `total_cost_usd` include
  sub-agents; `total_cost_usd` is cumulative per client → per-run delta.
- **can_use_tool (Phases 5/8):** called for SDK MCP tools and for built-ins
  (main and sub-agent) with full input; **not called for `Agent`**; entries
  in `allowed_tools` bypass it. Sub-agent calls: `ToolPermissionContext.agent_id`
  is set (e.g. `"a9107a28545a8ad33"`), main-agent calls: `None`.
- **Delegation guard (Phase 8 Task 8.6, replaces "Agent via can_use_tool";
  pending owner confirmation, see Decision):**
  `hooks={"PreToolUse": [HookMatcher(matcher="Agent", hooks=[guard])]}`; the
  hook gets `input["tool_name"] == "Agent"`, `input["tool_input"]
  ["subagent_type"]`, `input.get("agent_id")`; deny with
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": …}}`, allow by
  returning `{}` (falls through to `can_use_tool`, which must be set). Deny
  every `subagent_type` except `part-researcher` — the built-in agents
  `general-purpose`, `Explore`, `Plan`, `claude`, `statusline-setup` are
  always offered (init `agents`).
- **Researcher built-ins (Phase 8):** `BUILTINS_IN_SESSION = True` — the
  researcher's built-ins (WebFetch, WebSearch, Write, …) must be in the
  session `tools`, otherwise the sub-agent refuses to start. The session
  guard (`can_use_tool`) denies them when `agent_id is None` and path-guards
  the researcher's writes (h3b). The main agent sees those tools' schemas but
  cannot execute one.
- **Foreground sub-agents (Phases 5/8):** set
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` in `ClaudeAgentOptions.env` as a
  mitigation (not proven, see foreground probe); without it the model
  sometimes backgrounded the sub-agent, ending the turn before the research
  was done (runs 1–2). Phase 5 must also handle a backgrounded delegation
  defensively (tool result "Async agent launched", `task_started` system
  messages).
- **Per-call guard must check the sub-agent type (Phase 8):** built-in agents
  that slip past the delegation guard inherit the session's built-ins and MCP
  tools (runs 2, 4); `can_use_tool` allowing any `agent_id is not None` is not
  enough on its own.
- **Sub-agent stream (Phases 5/6A):** sub-agent `AssistantMessage` /
  `UserMessage` carry `parent_tool_use_id` = the `Agent` call's
  `ToolUseBlock.id`; no sub-agent `StreamEvent`s arrive (complete messages
  only, also with `forward_subagent_text=True`), and no unparented deltas
  leak while a delegation is open. Interrupt during a sub-agent ends the turn
  cleanly; no sub-agent message arrives after the ResultMessage.
