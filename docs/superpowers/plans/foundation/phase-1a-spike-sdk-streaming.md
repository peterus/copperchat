# Phase 1A: Spike — Python Agent SDK streaming input

**Goal:** Prove (or disprove) with real runs against the Claude API that the
Python `claude-agent-sdk` supports CopperChat's agent runtime design (spec §5):
lazy pull of queued input at turn boundaries, `interrupt()` mid-turn with the
session continuing, `resume` + `fork_session` at burst start, `tools=[]` with an
in-process SDK MCP server, `RateLimitEvent` and `ResultMessage` usage shapes,
`can_use_tool` for MCP tools, and an `agents=` sub-agent with its own tools
while the main agent has no built-ins. Since the owner decision of 2026-10-16
(part-researcher as a real SDK sub-agent behind a guarded `Agent` tool) the
spike also gates Phase 8 with a **sub-agent go/no-go** (Task 1A.7 h4/h5:
`can_use_tool` sees `Agent` with `subagent_type`, fires for the sub-agent's own
calls, sub-agent messages carry `parent_tool_use_id`, interrupt works during a
sub-agent). Record results and both decisions in
`docs/spikes/1a-sdk-streaming.md`.

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X19).

**Depends on:** —

**Parallel with:** 1B, 1C

**Read first:**
- `docs/superpowers/plans/2026-10-15-foundation-plan.md` — Global Constraints,
  "Go/no-go gates" (1A), C7 (Agent runtime + protocol).
- `docs/superpowers/specs/2026-10-15-foundation-design.md` — §3 (Agent, Auth),
  §5 (steps 2, 7, 8, 9), §8 (part-researcher tools/guard), §12.
- Kontor reference (read-only): `~/CloudCLI/kontor/server/runEngine/inputStream.ts`
  and `~/CloudCLI/kontor/docs/superpowers/specs/2026-06-23-message-queueing-streaming-input-design.md`
  ("Input-Generator — das Herzstück": the pull timing of the generator *is* the
  turn boundary in the TS SDK).
- SDK source (unpacked wheel 0.2.168, read-only): `/tmp/sdk-dl/x/claude_agent_sdk/`
  — `client.py` (`connect`, `interrupt`, `receive_messages`),
  `_internal/query.py` (`stream_input`, `wait_for_result_and_end_input`),
  `types.py` (`ClaudeAgentOptions`, `ResultMessage`, `RateLimitEvent`,
  `RateLimitInfo`, `AgentDefinition`, `CanUseTool`). If the wheel is gone:
  `uv run --with claude-agent-sdk==0.2.168 python -c "import claude_agent_sdk,sys;print(claude_agent_sdk.__file__)"`.

**PR title:** `foundation phase 1A: spike Python Agent SDK streaming input`

**Files:**
- Create: `spikes/1a-sdk-streaming/pyproject.toml`
- Create: `spikes/1a-sdk-streaming/.gitignore`
- Create: `spikes/1a-sdk-streaming/README.md` (how to run; 10 lines)
- Create: `spikes/1a-sdk-streaming/harness.py`
- Create: `spikes/1a-sdk-streaming/q_a_lazy_pull.py`
- Create: `spikes/1a-sdk-streaming/q_b_interrupt.py`
- Create: `spikes/1a-sdk-streaming/q_c_resume_fork.py`
- Create: `spikes/1a-sdk-streaming/q_d_tools_empty_mcp.py`
- Create: `spikes/1a-sdk-streaming/q_g_can_use_tool.py`
- Create: `spikes/1a-sdk-streaming/q_ef_usage_ratelimit.py`
- Create: `spikes/1a-sdk-streaming/q_h_subagent.py`
- Create: `spikes/1a-sdk-streaming/run_all.py`
- Create: `docs/spikes/1a-sdk-streaming.md`

**Acceptance criteria:**
- Every question (a)–(h) has its own script section with explicit
  `rec.check(...)` assertions that print `PASS`/`FAIL` per check with evidence,
  and writes `spikes/1a-sdk-streaming/results/<question>.json`.
- Every script prints exactly one line `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set`
  and exits 0 when neither `CLAUDE_CODE_OAUTH_TOKEN` nor `ANTHROPIC_API_KEY` is
  in the environment. No script offers, triggers or documents any login flow
  (no `claude login`, no `/login`, no browser OAuth).
- All runs are isolated from the owner's personal Claude setup:
  `setting_sources=[]` and `CLAUDE_CONFIG_DIR` pointing at
  `spikes/1a-sdk-streaming/.claude-config/` (gitignored).
- `docs/spikes/1a-sdk-streaming.md` follows the fixed template (Question(s),
  Setup, Results, Decision, Follow-ups for later phases) and its Decision
  section names the Phase 5 consequence using the index gate wording
  ("1A go" / "1A partial: gated generator" / "1A no-go → Phase 5 Task 5.4b").
- The doc states that CI does not run spikes and that the spike needs a token.

**Verification commands:**
```bash
cd spikes/1a-sdk-streaming
uv sync
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python run_all.py   # all SKIP, exit 0
CLAUDE_CODE_OAUTH_TOKEN=... uv run python run_all.py                           # real run, writes results/
uv run ruff check . && uv run ruff format --check .
```

**CI expectations:** No CI job runs spikes (they need a model token and cost
money). The PR has no new required checks; existing checks (if any at that
point) stay green. The PR description pastes `results/summary.md`.

> **Spike TDD adaptation.** For spikes the "failing test" is the experiment
> script with explicit assertions; "run to verify it fails" is the run without a
> token (must print `SKIP`, proving the skip path) or the first real run before
> the harness piece it needs exists; "implementation" is the harness code the
> script needs plus the real run; "verify" is the real run whose PASS/FAIL lines
> are the *result* (a FAIL is a valid finding, not a bug to fix). Each task
> ends by copying its evidence into the spike doc and committing.

---

### Task 1A.1: Spike project, harness, skip path

**Files:**
- Create: `spikes/1a-sdk-streaming/pyproject.toml`
- Create: `spikes/1a-sdk-streaming/.gitignore`
- Create: `spikes/1a-sdk-streaming/README.md`
- Create: `spikes/1a-sdk-streaming/harness.py`
- Test: `spikes/1a-sdk-streaming/q_a_lazy_pull.py` (first version: only the skip check)

**Interfaces:**
- Consumes: `claude_agent_sdk` 0.2.168 (`ClaudeAgentOptions`).
- Produces (spike-local only, not used by later phases):
  `require_token() -> None`, `base_options(**overrides) -> ClaudeAgentOptions`,
  `user_msg(text: str) -> dict`, `Recorder(question: str)` with
  `check(name, ok, evidence) / note(name, evidence) / finish() -> int`,
  `log(msg) -> None`, `collect(client, until_results, quiet_s, hard_s) -> Collected`.

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_a_lazy_pull.py` (first version, replaced in Task 1A.2):

```python
"""(a) Does the SDK pull streaming input lazily at turn boundaries?"""

import asyncio

from harness import Recorder, require_token


async def main() -> int:
    require_token()
    rec = Recorder("a_lazy_pull")
    rec.check("harness_loaded", True, "harness import works")
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1a-sdk-streaming && python3 q_a_lazy_pull.py
```
Expected: `ModuleNotFoundError: No module named 'harness'` (no project yet).

- [ ] **Step 3: Write minimal implementation**

`spikes/1a-sdk-streaming/pyproject.toml`:

```toml
[project]
name = "spike-1a-sdk-streaming"
version = "0.0.0"
description = "Throwaway spike: Python claude-agent-sdk streaming input (CopperChat phase 1A)"
requires-python = ">=3.13"
license = "GPL-3.0-only"
dependencies = ["claude-agent-sdk==0.2.168"]

[dependency-groups]
dev = ["ruff>=0.7"]

[tool.uv]
package = false

[tool.ruff]
line-length = 100
target-version = "py313"
```

`spikes/1a-sdk-streaming/.gitignore`:

```gitignore
.venv/
.claude-config/
results/
work/
__pycache__/
```

`spikes/1a-sdk-streaming/README.md`:

```markdown
# Spike 1A — Python Agent SDK streaming input

Throwaway code. Results: `docs/spikes/1a-sdk-streaming.md`. CI never runs this.

    uv sync
    export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`; never commit it
    uv run python run_all.py              # or a single q_*.py

Without a token every script prints `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set` and exits 0.
Runs are isolated: settings sources are disabled and the CLI config dir is `./.claude-config/`.
Optional: `SPIKE_MODEL` (default `haiku`) to choose the model alias.
```

`spikes/1a-sdk-streaming/harness.py`:

```python
"""Shared helpers for spike 1A. Throwaway code."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
WORKDIR = HERE / "work"
CONFIG_DIR = HERE / ".claude-config"
T0 = time.monotonic()


def now() -> float:
    """Seconds since harness import, 3 decimals."""
    return round(time.monotonic() - T0, 3)


def log(msg: str) -> None:
    print(f"[{now():8.3f}] {msg}", flush=True)


def require_token() -> None:
    """Exit 0 with a SKIP line when no model credential is present. Never logs in."""
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        print("SKIP: CLAUDE_CODE_OAUTH_TOKEN not set")
        raise SystemExit(0)


def base_options(**overrides: Any) -> ClaudeAgentOptions:
    """Isolated options: no filesystem settings, own config dir, no built-in tools."""
    CONFIG_DIR.mkdir(exist_ok=True)
    WORKDIR.mkdir(exist_ok=True)
    kwargs: dict[str, Any] = {
        "model": os.environ.get("SPIKE_MODEL", "haiku"),
        "tools": [],
        "setting_sources": [],
        "system_prompt": "You are a terse test assistant. Follow instructions exactly.",
        "cwd": str(WORKDIR),
        "env": {"CLAUDE_CONFIG_DIR": str(CONFIG_DIR)},
        "max_turns": 8,
    }
    kwargs.update(overrides)
    return ClaudeAgentOptions(**kwargs)


def user_msg(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


def text_of(msg: AssistantMessage) -> str:
    return "".join(b.text for b in msg.content if isinstance(b, TextBlock))


@dataclass
class Collected:
    messages: list[tuple[float, Any]] = field(default_factory=list)
    results: list[tuple[float, ResultMessage]] = field(default_factory=list)
    texts_per_turn: list[str] = field(default_factory=list)


async def collect(
    client: ClaudeSDKClient, until_results: int, quiet_s: float = 30.0, hard_s: float = 240.0
) -> Collected:
    """Read messages until `until_results` ResultMessages arrived, or nothing arrived for
    `quiet_s` seconds, or `hard_s` passed. Groups assistant text per turn."""
    out = Collected()
    current: list[str] = []
    it = client.receive_messages().__aiter__()
    deadline = time.monotonic() + hard_s
    while len(out.results) < until_results and time.monotonic() < deadline:
        try:
            msg = await asyncio.wait_for(anext(it), timeout=quiet_s)
        except (TimeoutError, StopAsyncIteration):
            break
        t = now()
        out.messages.append((t, msg))
        if isinstance(msg, AssistantMessage):
            current.append(text_of(msg))
        if isinstance(msg, ResultMessage):
            out.results.append((t, msg))
            out.texts_per_turn.append("".join(current))
            current = []
            log(f"ResultMessage #{len(out.results)} subtype={msg.subtype} "
                f"terminal_reason={getattr(msg, 'terminal_reason', None)}")
    return out


def jsonable(obj: Any) -> Any:
    """Best-effort conversion of SDK dataclasses for evidence files."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


class Recorder:
    """Collects PASS/FAIL checks and free-form notes for one question."""

    def __init__(self, question: str) -> None:
        self.question = question
        self.rows: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, evidence: Any) -> bool:
        status = "PASS" if ok else "FAIL"
        print(f"{status}: {self.question}/{name} :: {json.dumps(jsonable(evidence))[:400]}")
        self.rows.append({"name": name, "status": status, "evidence": jsonable(evidence)})
        return ok

    def note(self, name: str, evidence: Any) -> None:
        print(f"NOTE: {self.question}/{name} :: {json.dumps(jsonable(evidence))[:400]}")
        self.rows.append({"name": name, "status": "NOTE", "evidence": jsonable(evidence)})

    def finish(self) -> int:
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"{self.question}.json").write_text(
            json.dumps({"question": self.question, "rows": self.rows}, indent=2)
        )
        failed = [r for r in self.rows if r["status"] == "FAIL"]
        print(f"== {self.question}: {len(self.rows) - len(failed)} ok/notes, {len(failed)} FAIL")
        return 1 if failed else 0


def sdk_versions() -> dict[str, str]:
    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    return {
        "claude_agent_sdk": claude_agent_sdk.__version__,
        "bundled_cli": __cli_version__,
        "python": sys.version.split()[0],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd spikes/1a-sdk-streaming && uv sync
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_a_lazy_pull.py; echo "exit=$?"
CLAUDE_CODE_OAUTH_TOKEN=dummy uv run python q_a_lazy_pull.py; echo "exit=$?"
uv run ruff check . && uv run ruff format --check .
```
Expected: first run prints `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set` and `exit=0`;
second prints `PASS: a_lazy_pull/harness_loaded` and `exit=0` (no API call
yet); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/pyproject.toml spikes/1a-sdk-streaming/uv.lock \
  spikes/1a-sdk-streaming/.gitignore spikes/1a-sdk-streaming/README.md \
  spikes/1a-sdk-streaming/harness.py spikes/1a-sdk-streaming/q_a_lazy_pull.py
git commit -m "chore: add spike 1A harness with token skip path

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A.2: (a) Lazy pull at turn boundaries

Reading `_internal/query.py::stream_input` shows the Python SDK iterates the
prompt iterable in a plain `async for` and writes each item to the CLI's stdin
immediately — i.e. the *SDK* does not wait for a `ResultMessage` before pulling
the next item. Whether input is consumed one turn at a time is therefore
decided by (1) the CLI's handling of queued stdin messages and (2) whether our
generator blocks. This task measures three variants:

- **a1 pre-queued:** three items available at t0; record the pull timestamp of
  each item vs. the `ResultMessage` timestamps. "Lazy" means
  `pull[i+1] >= result[i]`.
- **a2 mid-turn push:** item 1 triggers a 6 s MCP tool; item 2 is pushed while
  the tool runs. Record whether item 2 gets its own turn (own `ResultMessage`)
  or is absorbed into turn 1 (turn 1 text mentions `TOKEN-B`, or only one
  result arrives).
- **a3 gated generator:** the generator yields item `i+1` only after it has
  seen `ResultMessage` `i` (an `asyncio.Event` set by the reader). This is the
  CopperChat-controlled turn boundary that works regardless of a1/a2.

**Files:**
- Modify: `spikes/1a-sdk-streaming/q_a_lazy_pull.py` (replace whole file)

**Interfaces:**
- Consumes: `harness.*` (Task 1A.1), `ClaudeSDKClient.connect(prompt: AsyncIterable[dict])`,
  `create_sdk_mcp_server`, `tool`.
- Produces: `results/a_lazy_pull.json` rows `a1_*`, `a2_*`, `a3_*`.

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_a_lazy_pull.py`:

```python
"""(a) Does the SDK pull streaming input lazily at turn boundaries?"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from harness import (
    Recorder,
    base_options,
    collect,
    log,
    now,
    require_token,
    sdk_versions,
    text_of,
    user_msg,
)


async def variant_prequeued(rec: Recorder) -> None:
    pulls: list[float] = []

    async def gen():
        for i in range(3):
            pulls.append(now())
            log(f"a1 generator: yielding item {i}")
            yield user_msg(f"Reply with exactly TOKEN-{i} and nothing else.")

    client = ClaudeSDKClient(base_options())
    await client.connect(gen())
    got = await collect(client, until_results=3)
    await client.disconnect()
    result_ts = [t for t, _ in got.results]
    lazy = len(result_ts) >= 2 and all(pulls[i + 1] >= result_ts[i] for i in range(2))
    rec.check("a1_pull_after_previous_result", lazy, {"pulls": pulls, "results": result_ts})
    rec.check(
        "a1_one_result_per_item",
        len(got.results) == 3,
        {"n_results": len(got.results), "texts": got.texts_per_turn},
    )
    rec.check(
        "a1_turn_i_answers_item_i",
        [f"TOKEN-{i}" in t for i, t in enumerate(got.texts_per_turn)] == [True, True, True],
        got.texts_per_turn,
    )


def slow_tool_server(started: asyncio.Event):
    @tool("slow_echo", "Waits 6 seconds, then echoes the given word.", {"word": str})
    async def slow_echo(args: dict[str, Any]) -> dict[str, Any]:
        log("a2 slow_echo started")
        started.set()
        await asyncio.sleep(6)
        log("a2 slow_echo finished")
        return {"content": [{"type": "text", "text": f"echo:{args['word']}"}]}

    return create_sdk_mcp_server("spike", tools=[slow_echo])


async def variant_midturn(rec: Recorder) -> None:
    started = asyncio.Event()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    pulls: list[float] = []

    async def gen():
        while (item := await queue.get()) is not None:
            pulls.append(now())
            log(f"a2 generator: yielded {item['message']['content'][:40]!r}")
            yield item

    opts = base_options(
        mcp_servers={"spike": slow_tool_server(started)},
        allowed_tools=["mcp__spike__slow_echo"],
    )
    client = ClaudeSDKClient(opts)
    await queue.put(user_msg("Call slow_echo with word=alpha, then reply with its output only."))
    await client.connect(gen())
    reader = asyncio.create_task(collect(client, until_results=2, quiet_s=40))
    await asyncio.wait_for(started.wait(), 120)
    push_t = now()
    await queue.put(user_msg("Reply with exactly TOKEN-B and nothing else."))
    got = await reader
    await queue.put(None)
    await client.disconnect()
    texts = got.texts_per_turn
    rec.note("a2_timeline", {"push_t": push_t, "pulls": pulls,
                             "results": [t for t, _ in got.results], "texts": texts})
    rec.check("a2_second_item_gets_own_turn", len(got.results) == 2, len(got.results))
    rec.check(
        "a2_turn1_not_absorbing_item2",
        bool(texts) and "TOKEN-B" not in texts[0] and "echo:alpha" in texts[0],
        texts,
    )


async def variant_gated(rec: Recorder) -> None:
    turn_done = asyncio.Event()
    pulls: list[float] = []

    async def gen():
        for i in range(3):
            if i > 0:
                await turn_done.wait()
                turn_done.clear()
            pulls.append(now())
            yield user_msg(f"Reply with exactly GATED-{i} and nothing else.")

    client = ClaudeSDKClient(base_options())
    await client.connect(gen())
    results: list[float] = []
    texts: list[str] = []
    buf: list[str] = []
    async with asyncio.timeout(240):
        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                buf.append(text_of(msg))
            if isinstance(msg, ResultMessage):
                results.append(now())
                texts.append("".join(buf))
                buf = []
                turn_done.set()
                if len(results) == 3:
                    break
    await client.disconnect()
    rec.check("a3_gated_pull_after_result",
              all(pulls[i + 1] >= results[i] for i in range(2)),
              {"pulls": pulls, "results": results})
    rec.check("a3_gated_one_turn_per_item",
              [f"GATED-{i}" in t for i, t in enumerate(texts)] == [True, True, True], texts)


async def main() -> int:
    require_token()
    rec = Recorder("a_lazy_pull")
    rec.note("versions", sdk_versions())
    for variant in (variant_prequeued, variant_midturn, variant_gated):
        try:
            await variant(rec)
        except Exception as exc:  # a crash is a finding, keep going
            rec.check(f"{variant.__name__}_no_exception", False, repr(exc))
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1a-sdk-streaming
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_a_lazy_pull.py
```
Expected: `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set` (skip path intact). Then
run with the token once; before Step 3 nothing is missing, so if this run
crashes with an `ImportError`/`TypeError` from the SDK, fix the call to match
the installed SDK and note the change in the doc.

- [ ] **Step 3: Write minimal implementation**

No harness change needed. Real run:

```bash
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" uv run python q_a_lazy_pull.py | tee results/a_lazy_pull.log
```

- [ ] **Step 4: Run tests to verify they pass**

The run completes (exit 0 or 1 — a FAIL is a finding) and
`results/a_lazy_pull.json` contains rows `a1_pull_after_previous_result`,
`a1_one_result_per_item`, `a1_turn_i_answers_item_i`, `a2_timeline`,
`a2_second_item_gets_own_turn`, `a2_turn1_not_absorbing_item2`,
`a3_gated_pull_after_result`, `a3_gated_one_turn_per_item`:

```bash
python3 -c "import json;r=json.load(open('results/a_lazy_pull.json'));print([x['name']+':'+x['status'] for x in r['rows']])"
```
Copy the rows plus the log lines with timestamps into the doc's Results → (a).

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/q_a_lazy_pull.py docs/spikes/1a-sdk-streaming.md
git commit -m "test: spike 1A (a) streaming input pull timing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
(Create `docs/spikes/1a-sdk-streaming.md` from the template in Task 1A.8 the
first time you commit evidence; fill only section (a) now.)

---

### Task 1A.3: (b) interrupt() mid-turn, then continue with the next queued item

**Files:**
- Create: `spikes/1a-sdk-streaming/q_b_interrupt.py`

**Interfaces:**
- Consumes: `ClaudeSDKClient.interrupt()`, `include_partial_messages=True`
  (`StreamEvent`), `ResultMessage.subtype / terminal_reason / is_error`.
- Produces: `results/b_interrupt.json`.

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_b_interrupt.py`:

```python
"""(b) interrupt() mid-turn (text streaming and inside an MCP tool), then continue."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    create_sdk_mcp_server,
    tool,
)

from harness import Recorder, base_options, log, now, require_token, text_of, user_msg


async def run_case(rec: Recorder, case: str, first_prompt: str, opts_extra: dict[str, Any],
                   trigger: asyncio.Event | None) -> None:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    turn_done = asyncio.Event()

    async def gen():
        first = True
        while True:
            if not first:
                await turn_done.wait()
                turn_done.clear()
            first = False
            item = await queue.get()
            if item is None:
                return
            yield item

    client = ClaudeSDKClient(base_options(include_partial_messages=True, **opts_extra))
    await queue.put(user_msg(first_prompt))
    await client.connect(gen())
    results: list[ResultMessage] = []
    texts: list[str] = []
    buf: list[str] = []
    interrupt_t: float | None = None
    first_delta = asyncio.Event()

    async def interrupter() -> None:
        nonlocal interrupt_t
        await (trigger.wait() if trigger else first_delta.wait())
        await asyncio.sleep(1.0)
        interrupt_t = now()
        log(f"{case}: calling interrupt()")
        await client.interrupt()
        await queue.put(user_msg("Reply with exactly AFTER-INTERRUPT and nothing else."))

    task = asyncio.create_task(interrupter())
    result_ts: list[float] = []
    async with asyncio.timeout(240):
        async for msg in client.receive_messages():
            if isinstance(msg, StreamEvent):
                first_delta.set()
            if isinstance(msg, AssistantMessage):
                buf.append(text_of(msg))
            if isinstance(msg, ResultMessage):
                results.append(msg)
                result_ts.append(now())
                texts.append("".join(buf))
                buf = []
                turn_done.set()
                if len(results) == 2:
                    break
    await queue.put(None)
    await task
    await client.disconnect()
    r1 = results[0]
    rec.note(f"{case}_first_result", {
        "subtype": r1.subtype, "is_error": r1.is_error, "stop_reason": r1.stop_reason,
        "terminal_reason": getattr(r1, "terminal_reason", None),
        "latency_interrupt_to_result_s": round(result_ts[0] - (interrupt_t or 0), 3),
        "partial_text_len": len(texts[0]),
    })
    rec.check(f"{case}_interrupt_produces_result", interrupt_t is not None and result_ts[0] >= interrupt_t,
              {"interrupt_t": interrupt_t, "result_t": result_ts[0]})
    rec.check(f"{case}_next_item_runs_after_interrupt", len(results) == 2 and "AFTER-INTERRUPT" in texts[1],
              texts)
    rec.check(f"{case}_same_session_after_interrupt",
              len(results) == 2 and results[0].session_id == results[1].session_id,
              [r.session_id for r in results])


async def main() -> int:
    require_token()
    rec = Recorder("b_interrupt")
    await run_case(rec, "b1_text", "Count from 1 to 400, one number per line, no other text.", {}, None)

    tool_started = asyncio.Event()
    cancelled = {"value": False}

    @tool("slow_wait", "Waits 30 seconds and returns 'done'.", {})
    async def slow_wait(args: dict[str, Any]) -> dict[str, Any]:
        tool_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        return {"content": [{"type": "text", "text": "done"}]}

    server = create_sdk_mcp_server("spike", tools=[slow_wait])
    await run_case(rec, "b2_in_tool", "Call slow_wait, then say its result.",
                   {"mcp_servers": {"spike": server}, "allowed_tools": ["mcp__spike__slow_wait"]},
                   tool_started)
    rec.note("b2_tool_coroutine_cancelled", cancelled["value"])
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_b_interrupt.py
```
Expected: `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set`.

- [ ] **Step 3: Write minimal implementation**

Real run: `uv run python q_b_interrupt.py | tee results/b_interrupt.log`.

- [ ] **Step 4: Run tests to verify they pass**

`results/b_interrupt.json` has `b1_*` and `b2_*` rows plus
`b2_tool_coroutine_cancelled`. Record in the doc: the first result's
`subtype`/`terminal_reason` after interrupt (Phase 5 maps it to
`run-finished{reason:"interrupted"}`), interrupt→result latency, whether the
session id survives, whether the MCP tool coroutine is cancelled.

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/q_b_interrupt.py docs/spikes/1a-sdk-streaming.md
git commit -m "test: spike 1A (b) interrupt mid-turn and continue

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A.4: (c) resume + fork_session at burst start

**Files:**
- Create: `spikes/1a-sdk-streaming/q_c_resume_fork.py`

**Interfaces:**
- Consumes: `ClaudeAgentOptions(resume=..., fork_session=True)`,
  `ResultMessage.session_id`.
- Produces: `results/c_resume_fork.json` incl. where session files live under
  `CLAUDE_CONFIG_DIR` (Phase 5/9 map this into `/data`).

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_c_resume_fork.py`:

```python
"""(c) resume + fork_session at burst start: memory carries over, ids fork, branches isolate."""

from __future__ import annotations

import asyncio
import secrets

from claude_agent_sdk import ClaudeSDKClient

from harness import CONFIG_DIR, Recorder, base_options, collect, require_token, user_msg


async def burst(prompt: str, **opts) -> tuple[str, str]:
    """One burst = one connect with one queued item; returns (session_id, text)."""

    async def gen():
        yield user_msg(prompt)

    client = ClaudeSDKClient(base_options(**opts))
    await client.connect(gen())
    got = await collect(client, until_results=1)
    await client.disconnect()
    _, result = got.results[0]
    return result.session_id, got.texts_per_turn[0]


async def main() -> int:
    require_token()
    rec = Recorder("c_resume_fork")
    word1 = f"PAPAYA{secrets.token_hex(3).upper()}"
    word2 = f"KIWI{secrets.token_hex(3).upper()}"

    s1, _ = await burst(f"Remember the codeword {word1}. Reply OK.")
    s2, t2 = await burst(f"What is the codeword? Also remember second word {word2}. "
                         "Reply with the codeword only.", resume=s1, fork_session=True)
    rec.check("c1_resume_remembers", word1 in t2, t2)
    rec.check("c2_fork_new_session_id", s2 != s1, {"s1": s1, "s2": s2})

    s3, t3 = await burst("Did I tell you a second word? Reply with it, or NONE.",
                         resume=s1, fork_session=True)
    rec.check("c3_fork_branches_isolated", word2 not in t3, t3)
    rec.check("c3_new_id_again", s3 not in (s1, s2), [s1, s2, s3])

    s4, t4 = await burst("Reply with the second word only.", resume=s2, fork_session=True)
    rec.check("c4_resume_from_fork", word2 in t4, t4)

    files = sorted(str(p.relative_to(CONFIG_DIR)) for p in CONFIG_DIR.rglob("*.jsonl"))
    rec.note("c5_session_store_files", files)
    rec.check("c5_sessions_in_config_dir", any(s1 in f for f in files), files)
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

`env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_c_resume_fork.py`
→ `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set`.

- [ ] **Step 3: Write minimal implementation**

Real run: `uv run python q_c_resume_fork.py | tee results/c_resume_fork.log`.

- [ ] **Step 4: Run tests to verify they pass**

`results/c_resume_fork.json` contains `c1`…`c5`. Record in the doc the session
file path pattern (e.g. `projects/<cwd-slug>/<session_id>.jsonl`) — Phase 5
stores `CLAUDE_CONFIG_DIR=/data/claude` and must know that the slug depends on
`cwd`.

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/q_c_resume_fork.py docs/spikes/1a-sdk-streaming.md
git commit -m "test: spike 1A (c) resume and fork_session

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A.5: (d) tools=[] with an in-process SDK MCP server, and (g) can_use_tool for MCP tools

Both questions share the same option set (no built-ins, one SDK MCP server), so
they live in one task with two scripts.

**Files:**
- Create: `spikes/1a-sdk-streaming/q_d_tools_empty_mcp.py`
- Create: `spikes/1a-sdk-streaming/q_g_can_use_tool.py`

**Interfaces:**
- Consumes: `create_sdk_mcp_server(name, tools=[...])`, `@tool(name, desc, schema)`,
  `SystemMessage` (subtype `init`, `data["tools"]`), `ToolUseBlock`,
  `ToolResultBlock`, `CanUseTool = Callable[[str, dict, ToolPermissionContext], Awaitable[PermissionResultAllow | PermissionResultDeny]]`.
- Produces: `results/d_tools_empty_mcp.json`, `results/g_can_use_tool.json`.

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_d_tools_empty_mcp.py`:

```python
"""(d) tools=[] disables all built-ins while an in-process SDK MCP tool still works."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    SystemMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from harness import Recorder, base_options, collect, require_token, user_msg

MAGIC = "MAGIC-7731"


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": MAGIC}]}


async def main() -> int:
    require_token()
    rec = Recorder("d_tools_empty_mcp")
    server = create_sdk_mcp_server("spike", tools=[get_magic])

    async def gen():
        yield user_msg("First run the shell command `ls /` with any tool you have. "
                       "Then call get_magic and reply with its value.")

    client = ClaudeSDKClient(base_options(
        tools=[], mcp_servers={"spike": server}, allowed_tools=["mcp__spike__get_magic"]))
    await client.connect(gen())
    got = await collect(client, until_results=1)
    await client.disconnect()

    init = next(m for _, m in got.messages if isinstance(m, SystemMessage) and m.subtype == "init")
    tools = init.data.get("tools", [])
    rec.note("d_init_tools", tools)
    rec.check("d1_no_builtin_tools", set(tools) == {"mcp__spike__get_magic"}, tools)
    used = [b.name for _, m in got.messages if isinstance(m, AssistantMessage)
            for b in m.content if isinstance(b, ToolUseBlock)]
    rec.check("d2_no_builtin_tool_called", all(n.startswith("mcp__") for n in used), used)
    rec.check("d3_mcp_tool_called", "mcp__spike__get_magic" in used, used)
    rec.check("d4_mcp_result_in_answer", MAGIC in got.texts_per_turn[0], got.texts_per_turn)
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

`spikes/1a-sdk-streaming/q_g_can_use_tool.py`:

```python
"""(g) Is can_use_tool invoked for in-process MCP tools, and how do allow rules shadow it?"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    ToolResultBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from harness import Recorder, base_options, collect, require_token, user_msg


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "MAGIC-7731"}]}


@tool("deny_me", "Writes a file at the given path.", {"path": str})
async def deny_me(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "written"}]}


async def run(allowed: list[str]) -> tuple[list[tuple[str, dict]], list[Any]]:
    calls: list[tuple[str, dict]] = []

    async def can_use_tool(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        calls.append((name, inp))
        if name.endswith("deny_me"):
            return PermissionResultDeny(message="path outside docs/parts/ is not allowed")
        return PermissionResultAllow()

    async def gen():
        yield user_msg("Call get_magic. Then call deny_me with path=/etc/passwd. "
                       "Then report both outcomes in one line.")

    server = create_sdk_mcp_server("spike", tools=[get_magic, deny_me])
    client = ClaudeSDKClient(base_options(
        mcp_servers={"spike": server}, allowed_tools=allowed, can_use_tool=can_use_tool))
    await client.connect(gen())
    got = await collect(client, until_results=1)
    await client.disconnect()
    results = [b for _, m in got.messages if isinstance(m, UserMessage) and isinstance(m.content, list)
               for b in m.content if isinstance(b, ToolResultBlock)]
    return calls, results


async def main() -> int:
    require_token()
    rec = Recorder("g_can_use_tool")
    calls, results = await run(allowed=[])
    names = [n for n, _ in calls]
    rec.check("g1_called_for_mcp_tool", "mcp__spike__get_magic" in names, calls)
    rec.check("g2_called_with_input", any(n.endswith("deny_me") and i.get("path") == "/etc/passwd"
                                          for n, i in calls), calls)
    rec.check("g3_denial_reaches_model_as_error",
              any(r.is_error and "docs/parts" in str(r.content) for r in results),
              [(r.is_error, str(r.content)[:200]) for r in results])
    calls2, _ = await run(allowed=["mcp__spike__get_magic"])
    rec.check("g4_allowed_tools_shadow_callback",
              "mcp__spike__get_magic" not in [n for n, _ in calls2], calls2)
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_d_tools_empty_mcp.py
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_g_can_use_tool.py
```
Expected: both print `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set`.

- [ ] **Step 3: Write minimal implementation**

Real runs:
```bash
uv run python q_d_tools_empty_mcp.py | tee results/d_tools_empty_mcp.log
uv run python q_g_can_use_tool.py | tee results/g_can_use_tool.log
```

- [ ] **Step 4: Run tests to verify they pass**

Both JSON files exist with rows `d1`–`d4`, `g1`–`g4`. Record the exact init
`tools` list (Phase 5 asserts the same list in its adapter test) and whether
`allowed_tools` entries bypass `can_use_tool` (Phase 8's path guard must then
*not* list guarded tools in `allowed_tools`).

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/q_d_tools_empty_mcp.py spikes/1a-sdk-streaming/q_g_can_use_tool.py docs/spikes/1a-sdk-streaming.md
git commit -m "test: spike 1A (d) tools=[] with SDK MCP and (g) can_use_tool

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A.6: (e) RateLimitEvent shape and (f) ResultMessage usage

**Files:**
- Create: `spikes/1a-sdk-streaming/q_ef_usage_ratelimit.py`

**Interfaces:**
- Consumes: `RateLimitEvent(rate_limit_info: RateLimitInfo, uuid, session_id)`,
  `RateLimitInfo(status, resets_at, rate_limit_type, utilization, overage_*, raw)`,
  `ResultMessage(usage, total_cost_usd, model_usage, num_turns, duration_ms, duration_api_ms)`,
  `StreamEvent.event` (for per-message usage deltas).
- Produces: `results/ef_usage_ratelimit.json` with the literal dict shapes
  Phase 5 maps into the C7 `usage` frame
  (`inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens, costUsd, rateLimit{status, resetsAt}`).

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_ef_usage_ratelimit.py`:

```python
"""(e) RateLimitEvent shape; (f) usage data on ResultMessage (two turns, cumulative or not)."""

from __future__ import annotations

import asyncio
from collections import Counter

from claude_agent_sdk import ClaudeSDKClient, RateLimitEvent, ResultMessage, StreamEvent

from harness import Recorder, base_options, jsonable, require_token, user_msg

USAGE_KEYS = {"input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens"}


async def main() -> int:
    require_token()
    rec = Recorder("ef_usage_ratelimit")
    turn_done = asyncio.Event()

    async def gen():
        yield user_msg("Reply with the word ONE.")
        await turn_done.wait()  # gated: second prompt only after the first ResultMessage
        yield user_msg("Reply with the word TWO.")

    client = ClaudeSDKClient(base_options(include_partial_messages=True))
    await client.connect(gen())
    msgs: list = []
    async with asyncio.timeout(240):
        async for msg in client.receive_messages():
            msgs.append(msg)
            if isinstance(msg, ResultMessage):
                turn_done.set()
                if sum(isinstance(m, ResultMessage) for m in msgs) == 2:
                    break
    await client.disconnect()

    rec.note("ef_message_kinds", dict(Counter(type(m).__name__ for m in msgs)))
    rl = [m for m in msgs if isinstance(m, RateLimitEvent)]
    rec.note("e_rate_limit_events", [jsonable(e) for e in rl])
    rec.check("e1_rate_limit_event_observed", bool(rl), len(rl))
    if rl:
        info = rl[0].rate_limit_info
        rec.check("e2_status_field", info.status in ("allowed", "allowed_warning", "rejected"),
                  info.status)
        rec.note("e3_resets_at_type", type(info.resets_at).__name__)
        rec.note("e4_raw", info.raw)
    results = [m for m in msgs if isinstance(m, ResultMessage)]
    rec.check("f1_two_results", len(results) == 2, len(results))
    for i, r in enumerate(results):
        rec.note(f"f_result_{i}", {
            "usage": r.usage, "total_cost_usd": r.total_cost_usd,
            "model_usage": jsonable(r.model_usage), "num_turns": r.num_turns,
            "duration_ms": r.duration_ms, "duration_api_ms": r.duration_api_ms,
        })
        rec.check(f"f2_usage_keys_turn{i}", USAGE_KEYS <= set((r.usage or {}).keys()),
                  sorted((r.usage or {}).keys()))
    if len(results) == 2:
        c0, c1 = results[0].total_cost_usd or 0, results[1].total_cost_usd or 0
        rec.note("f3_cost_cumulative_across_turns", {"turn0": c0, "turn1": c1, "cumulative": c1 > c0})
    deltas = [m.event for m in msgs if isinstance(m, StreamEvent)
              and m.event.get("type") in ("message_start", "message_delta")]
    rec.note("f4_stream_usage_events", [d.get("usage") or d.get("message", {}).get("usage")
                                        for d in deltas][:6])
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

`env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_ef_usage_ratelimit.py`
→ `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set`.

- [ ] **Step 3: Write minimal implementation**

Real run: `uv run python q_ef_usage_ratelimit.py | tee results/ef_usage_ratelimit.log`.
If `e1_rate_limit_event_observed` is FAIL (the CLI only emits on status
changes), record "not observed in normal operation" and copy the dataclass
shape from `types.py` (`RateLimitEvent`/`RateLimitInfo`, quoted above) as the
documented contract; `resets_at` is a Unix timestamp (int seconds).

- [ ] **Step 4: Run tests to verify they pass**

`results/ef_usage_ratelimit.json` exists with `e*` and `f*` rows. Record in the
doc whether `total_cost_usd`/`usage` are per-turn or cumulative across turns of
one streaming session (Phase 5 must compute per-run deltas if cumulative).

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/q_ef_usage_ratelimit.py docs/spikes/1a-sdk-streaming.md
git commit -m "test: spike 1A (e) rate limit event and (f) usage shape

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A.7: (h) Sub-agent with its own tools while main has no built-ins

`AgentDefinition` sub-agents are invoked through the built-in `Agent` tool
(older CLIs: `Task`). With `tools=[]` that tool is gone, so this task measures
three configurations:

- **h1:** main `tools=[]` + `agents={"helper": …}` → is the helper reachable at all?
- **h2:** main `tools=["Agent"]` (fallback name `"Task"` if `Agent` is not in
  the init tool list) + helper with `tools=["mcp__spike__get_magic"]` → works?
  Main still has no Bash/Read/Write/WebFetch?
- **h3:** helper with `tools=["WebFetch", "Write"]` while main has only
  `["Agent"]` → can the helper use built-ins the main agent does not have, and
  does `can_use_tool` see the helper's `Write` calls (Phase 8 path guard)?
- **h4 (owner gate, 2026-10-16):** main `tools=["Agent", "Skill"]`, `Agent`
  **not** in `allowed_tools`, a recording `can_use_tool` that allows `Agent`
  only for `subagent_type == "helper"` → (a) the guard is invoked for the
  `Agent` call and sees `subagent_type` in the input; a `general-purpose`
  delegation is denied by the guard; (b) the guard also fires for the
  sub-agent's own tool calls, and `ToolPermissionContext.agent_id` (or the
  equivalent field) is `None` for main-agent calls and set for sub-agent calls;
  (c) every sub-agent message (`AssistantMessage`, `UserMessage`, and
  `StreamEvent` with `include_partial_messages=True`) carries
  `parent_tool_use_id` equal to the id of the `Agent` `ToolUseBlock`.
- **h5:** interrupt while the sub-agent is working → the turn ends with a
  `ResultMessage` (interrupted), no sub-agent message arrives after it, and the
  next turn on the same client works; plus usage: does the turn's
  `ResultMessage.usage` / `total_cost_usd` include the sub-agent's tokens
  (compare with the sum of the sub-agent `AssistantMessage.usage` values, if
  exposed)?

**Files:**
- Create: `spikes/1a-sdk-streaming/q_h_subagent.py`

**Interfaces:**
- Consumes: `AgentDefinition(description, prompt, tools, model)`,
  `AssistantMessage.parent_tool_use_id` (sub-agent messages).
- Produces: `results/h_subagent.json` incl. the exact delegation tool name.

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/q_h_subagent.py`:

```python
"""(h) agents= sub-agent with its own tools while the main agent has no built-in tools."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from harness import WORKDIR, Recorder, base_options, collect, require_token, user_msg

MAGIC = "MAGIC-7731"


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": MAGIC}]}


async def run(main_tools: list[str], helper_tools: list[str], prompt: str, guard=None):
    server = create_sdk_mcp_server("spike", tools=[get_magic])
    helper = AgentDefinition(
        description="Helper that fetches data. Use it for any data lookup.",
        prompt="You are a helper. Use your tools to answer, reply tersely.",
        tools=helper_tools, model="haiku")

    async def gen():
        yield user_msg(prompt)

    opts = base_options(tools=main_tools, mcp_servers={"spike": server},
                        agents={"helper": helper},
                        allowed_tools=["mcp__spike__get_magic", *main_tools],
                        can_use_tool=guard)
    client = ClaudeSDKClient(opts)
    await client.connect(gen())
    got = await collect(client, until_results=1, hard_s=300)
    await client.disconnect()
    init = next(m for _, m in got.messages if isinstance(m, SystemMessage) and m.subtype == "init")
    uses = [(m.parent_tool_use_id, b.name, b.input) for _, m in got.messages
            if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, ToolUseBlock)]
    return init.data.get("tools", []), init.data.get("agents"), uses, got.texts_per_turn[0]


async def main() -> int:
    require_token()
    rec = Recorder("h_subagent")
    ask = "Use the helper sub-agent to get the magic value via get_magic. Reply with the value."

    tools1, agents1, uses1, text1 = await run([], ["mcp__spike__get_magic"], ask)
    rec.note("h1_init", {"tools": tools1, "agents": agents1, "uses": uses1})
    rec.check("h1_helper_reachable_with_tools_empty",
              any(u[1] in ("Agent", "Task") for u in uses1), uses1)

    delegate = "Agent"
    tools2, agents2, uses2, text2 = await run([delegate], ["mcp__spike__get_magic"], ask)
    if delegate not in tools2:
        delegate = "Task"
        tools2, agents2, uses2, text2 = await run([delegate], ["mcp__spike__get_magic"], ask)
    rec.note("h2_delegation_tool_name", delegate)
    rec.check("h2_main_has_only_delegate_and_mcp",
              set(tools2) <= {delegate, "mcp__spike__get_magic"}, tools2)
    rec.check("h2_helper_used_mcp_tool",
              any(p is not None and n == "mcp__spike__get_magic" for p, n, _ in uses2), uses2)
    rec.check("h2_answer_contains_magic", MAGIC in text2, text2)

    seen: list[tuple[str, dict]] = []

    async def guard(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        seen.append((name, inp))
        if name == "Write" and "docs/parts/" not in str(inp.get("file_path", "")):
            return PermissionResultDeny(message="writes only under docs/parts/")
        return PermissionResultAllow()

    ask3 = ("Use the helper sub-agent: it must fetch https://example.com, then write the page "
            f"title into {WORKDIR}/docs/parts/TEST/notes.md and also try to write it to "
            f"{WORKDIR}/outside.md. Reply with the title.")
    tools3, _, uses3, text3 = await run([delegate], ["WebFetch", "Write"], ask3, guard=guard)
    rec.check("h3_main_lacks_webfetch_write", not {"WebFetch", "Write"} & set(tools3), tools3)
    rec.check("h3_helper_used_webfetch",
              any(p is not None and n == "WebFetch" for p, n, _ in uses3), uses3)
    rec.check("h3_guard_saw_helper_write", any(n == "Write" for n, _ in seen), seen)
    rec.check("h3_inside_write_ok", (WORKDIR / "docs/parts/TEST/notes.md").exists(), str(WORKDIR))
    rec.check("h3_outside_write_denied", not (WORKDIR / "outside.md").exists(), str(WORKDIR))
    rec.note("h3_answer", text3)

    # h4: owner gate — Agent guarded by subagent_type, guard sees sub-agent calls, parent ids
    calls: list[dict[str, Any]] = []

    async def gate_guard(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        calls.append({"name": name, "subagent_type": inp.get("subagent_type"),
                      "agent_id": getattr(ctx, "agent_id", "<no field>")})
        if name == delegate and inp.get("subagent_type") != "helper":
            return PermissionResultDeny(message="only the helper sub-agent may be used")
        return PermissionResultAllow()

    server = create_sdk_mcp_server("spike", tools=[get_magic])
    helper = AgentDefinition(description="Helper that fetches data. Use it for any data lookup.",
                             prompt="You are a helper. Use your tools to answer, reply tersely.",
                             tools=["mcp__spike__get_magic", "WebFetch"], model="haiku")

    async def gen4():
        yield user_msg("First try the general-purpose sub-agent to fetch https://example.com. "
                       "Then use the helper sub-agent to get the magic value via get_magic. "
                       "Reply with the value.")

    opts4 = base_options(tools=[delegate, "Skill"], mcp_servers={"spike": server},
                         agents={"helper": helper}, allowed_tools=[],  # nothing shadows the guard
                         can_use_tool=gate_guard, include_partial_messages=True,
                         forward_subagent_text=True)
    client4 = ClaudeSDKClient(opts4)
    await client4.connect(gen4())
    got4 = await collect(client4, until_results=1, hard_s=300)
    await client4.disconnect()
    agent_ids = {b.id for _, m in got4.messages if isinstance(m, AssistantMessage)
                 for b in m.content if isinstance(b, ToolUseBlock) and b.name == delegate}
    sub_msgs = [m for _, m in got4.messages if getattr(m, "parent_tool_use_id", None)]
    rec.note("h4_calls", calls)
    rec.check("h4a_guard_sees_agent_with_subagent_type",
              any(c["name"] == delegate and c["subagent_type"] == "helper" for c in calls), calls)
    rec.check("h4a_general_purpose_denied_by_guard",
              any(c["name"] == delegate and c["subagent_type"] == "general-purpose" for c in calls)
              and not any(getattr(m, "parent_tool_use_id", None) and "example.com" in str(m)
                          for m in sub_msgs), calls)
    rec.check("h4b_guard_fires_for_subagent_tools",
              any(c["name"] == "mcp__spike__get_magic" for c in calls), calls)
    rec.check("h4b_agent_id_distinguishes_main_and_sub",
              all(c["agent_id"] is None for c in calls if c["name"] == delegate)
              and all(c["agent_id"] not in (None, "<no field>")
                      for c in calls if c["name"] == "mcp__spike__get_magic"), calls)
    rec.check("h4c_subagent_messages_carry_parent_id",
              bool(sub_msgs) and all(m.parent_tool_use_id in agent_ids for m in sub_msgs),
              [(type(m).__name__, m.parent_tool_use_id) for m in sub_msgs][:20])
    rec.note("h4c_stream_events_with_parent",
             sum(1 for m in sub_msgs if type(m).__name__ == "StreamEvent"))
    # Unparented text deltas while an Agent call is open would leak sub-agent text into the
    # main agent's streaming segment (Phase 5); record how many arrive in that window.
    open_calls: set[str] = set()
    leaked = 0
    for _, m in got4.messages:
        if isinstance(m, AssistantMessage) and not m.parent_tool_use_id:
            open_calls |= {b.id for b in m.content if isinstance(b, ToolUseBlock) and b.name == delegate}
        if type(m).__name__ == "UserMessage" and not getattr(m, "parent_tool_use_id", None) \
                and not isinstance(m.content, str):
            open_calls -= {getattr(b, "tool_use_id", None) for b in m.content}
        if type(m).__name__ == "StreamEvent" and open_calls and not m.parent_tool_use_id \
                and m.event.get("type") == "content_block_delta":
            leaked += 1
    rec.check("h4c_no_unparented_deltas_during_subagent", leaked == 0, leaked)

    # h5: interrupt during the sub-agent, then a second turn; usage incl. sub-agent tokens.
    # The sub-agent uses a slow in-session MCP tool (not a built-in), so h5 does not depend on
    # whether built-ins are inherited (h3).
    @tool("slow_lookup", "Looks up a value slowly (20 s).", {"key": str})
    async def slow_lookup(args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(20)
        return {"content": [{"type": "text", "text": f"value-of-{args.get('key')}"}]}

    slow_server = create_sdk_mcp_server("spike", tools=[slow_lookup])
    slow_helper = AgentDefinition(description="Slow lookup helper. Use it for every lookup.",
                                  prompt="Look up each key with slow_lookup, one by one, then summarise.",
                                  tools=["mcp__spike__slow_lookup"], model="haiku")
    second = asyncio.Event()

    async def allow_all(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        return PermissionResultAllow()

    async def gen5():
        yield user_msg("Use the lookup sub-agent to look up the keys alpha, beta and gamma.")
        await second.wait()
        yield user_msg("Reply with exactly: AFTER")

    opts5 = base_options(tools=[delegate], mcp_servers={"spike": slow_server},
                         agents={"lookup": slow_helper}, allowed_tools=[], can_use_tool=allow_all,
                         forward_subagent_text=True)
    client5 = ClaudeSDKClient(opts5)
    await client5.connect(gen5())
    interrupted_at: float | None = None
    got5_msgs: list[tuple[float, Any]] = []
    try:
        async with asyncio.timeout(300):
            async for msg in client5.receive_messages():
                now = asyncio.get_running_loop().time()
                got5_msgs.append((now, msg))
                if interrupted_at is None and getattr(msg, "parent_tool_use_id", None):
                    interrupted_at = now
                    await client5.interrupt()
                if type(msg).__name__ == "ResultMessage":
                    if not second.is_set():
                        second.set()
                    else:
                        break
    except TimeoutError:
        rec.note("h5_timeout", len(got5_msgs))
    await client5.disconnect()
    results = [(t, m) for t, m in got5_msgs if type(m).__name__ == "ResultMessage"]
    first_result_t = results[0][0] if results else float("inf")
    rec.check("h5_interrupt_ends_turn_during_subagent",
              interrupted_at is not None and bool(results), [str(m)[:200] for _, m in results])
    rec.check("h5_no_subagent_messages_after_result",
              not any(t > first_result_t and getattr(m, "parent_tool_use_id", None)
                      for t, m in got5_msgs if t < (results[1][0] if len(results) > 1 else 1e18)),
              len(got5_msgs))
    rec.check("h5_next_turn_works", len(results) > 1 and "AFTER" in str(results[1][1]), len(results))
    sub_usage = [getattr(m, "usage", None) for _, m in got4.messages
                 if isinstance(m, AssistantMessage) and m.parent_tool_use_id]
    final4 = next((m for _, m in got4.messages if type(m).__name__ == "ResultMessage"), None)
    rec.note("h5_usage_compare", {"result_usage": getattr(final4, "usage", None),
                                  "result_cost": getattr(final4, "total_cost_usd", None),
                                  "subagent_assistant_usage": sub_usage})
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run test to verify it fails**

`env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python q_h_subagent.py`
→ `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set`.

- [ ] **Step 3: Write minimal implementation**

Real run: `rm -rf work && uv run python q_h_subagent.py | tee results/h_subagent.log`.

- [ ] **Step 4: Run tests to verify they pass**

`results/h_subagent.json` exists with `h1`–`h5` rows. Record: the delegation
tool name; that main's tool list is then exactly `{<delegate>, mcp__…}`;
whether the helper's built-ins work although the main base set excludes them;
whether `can_use_tool` receives the helper's `Write` (if not, Phase 8 must use
a `PreToolUse` hook instead — record which one works); the exact name of the
field that identifies sub-agent calls in `ToolPermissionContext` (`agent_id`
or other); whether sub-agent `StreamEvent`s carry `parent_tool_use_id`; and
whether sub-agent text arrives with `forward_subagent_text=True` (it is off
by default in the SDK; Phase 8 sets it); whether a skill's `allowed-tools`
front matter would bypass `can_use_tool` (informational — Phase 8 strips that
key anyway); whether the turn's `ResultMessage.usage` / `total_cost_usd` already include
the sub-agent's tokens (h5 note — if not, Phase 5's mapper sums the sub-agent
`AssistantMessage.usage` values into the turn's `Usage`).

**Sub-agent gate (owner decision 2026-10-16, binding for Phase 8):** the
decision section of the spike doc states one of:

- **sub-agent go** — h4a (both rows), h4b (both rows), h4c (all rows) and the h5
  interrupt rows PASS **and** h3 `helper_used_webfetch` PASS: Phase 8 Task 8.6
  as written (main `tools=["Agent", "Skill"]`, researcher built-ins not in the
  session `tools`, `BUILTINS_IN_SESSION = False`).
- **sub-agent go (built-ins in session)** — as above, but h3
  `helper_used_webfetch` FAIL (sub-agents only get built-ins that are in the
  session `tools`): Phase 8 Task 8.6 with `BUILTINS_IN_SESSION = True` — the
  researcher's built-ins are added to the session `tools`, and the session
  guard denies every one of them for main-agent calls (`agent_id` is `None`).
  The main agent then sees those tools' schemas but can never execute one; the
  coordinator mentions this in its report to the owner.
- **sub-agent no-go** — any of h4a, h4b (both rows: the guard must fire for
  sub-agent calls **and** tell them apart from main-agent calls, because the
  `download_datasheet` MCP tool is visible to the main agent too), h4c or the
  h5 interrupt rows FAIL: Phase 8 uses the documented fallback Task 8.6b
  (separate one-shot SDK query behind a `research_part` MCP tool).

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/q_h_subagent.py docs/spikes/1a-sdk-streaming.md
git commit -m "test: spike 1A (h) sub-agent tools with main tools disabled

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A.8: run_all, summary, spike result doc and decision

**Files:**
- Create: `spikes/1a-sdk-streaming/run_all.py`
- Create/complete: `docs/spikes/1a-sdk-streaming.md`

**Interfaces:**
- Consumes: all `q_*.py`, `results/*.json`.
- Produces: `results/summary.md` (Markdown table: question, check, status,
  short evidence) and the final spike doc. The doc's "Follow-ups" are binding
  input for Phase 5 and Phase 8.

- [ ] **Step 1: Write the failing test**

`spikes/1a-sdk-streaming/run_all.py`:

```python
"""Run every spike 1A question, then write results/summary.md."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = ["q_a_lazy_pull.py", "q_b_interrupt.py", "q_c_resume_fork.py",
           "q_d_tools_empty_mcp.py", "q_ef_usage_ratelimit.py", "q_g_can_use_tool.py",
           "q_h_subagent.py"]


def main() -> int:
    worst = 0
    skipped = False
    for script in SCRIPTS:
        print(f"### {script}", flush=True)
        proc = subprocess.run([sys.executable, script], cwd=HERE, text=True,
                              capture_output=True, timeout=900)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr[-4000:])
        skipped |= proc.stdout.startswith("SKIP:")
        worst = max(worst, proc.returncode)
    if skipped:
        return 0
    lines = ["| Question | Check | Status | Evidence |", "|---|---|---|---|"]
    for f in sorted((HERE / "results").glob("*.json")):
        data = json.loads(f.read_text())
        for row in data["rows"]:
            ev = json.dumps(row["evidence"])[:160].replace("|", "\\|")
            lines.append(f"| {data['question']} | {row['name']} | {row['status']} | `{ev}` |")
    (HERE / "results" / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY uv run python run_all.py; echo "exit=$?"
```
Expected: seven `SKIP: CLAUDE_CODE_OAUTH_TOKEN not set` lines, `exit=0`, no
`results/summary.md` written.

- [ ] **Step 3: Write minimal implementation**

Full real run: `rm -rf results work .claude-config && uv run python run_all.py`.
Then write `docs/spikes/1a-sdk-streaming.md` with exactly this structure
(fill every cell from `results/summary.md` and the logs; quote timestamps and
literal dict shapes):

```markdown
# Spike 1A — Python Agent SDK streaming input

Status: done (YYYY-MM-DD). Code: `spikes/1a-sdk-streaming/`. CI does not run
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
- claude-agent-sdk <version>, bundled CLI <version>, Python <version>, model alias <SPIKE_MODEL>
- Isolation: setting_sources=[], CLAUDE_CONFIG_DIR=spikes/1a-sdk-streaming/.claude-config
- Commands: `uv sync && uv run python run_all.py`

## Results
### (a) … (h)
One subsection per question: PASS/FAIL per check (table from results/summary.md),
plus evidence (timestamps, init tool list, ResultMessage fields, literal shapes).

## Decision
One of:
- **1A go** — a1 PASS (SDK/CLI consume one item per turn, pull after result):
  Phase 5 uses one long-lived `ClaudeSDKClient` per burst fed by the push
  stream exactly as in Kontor.
- **1A partial: gated generator** — a1 or a2 FAIL but a3 PASS: Phase 5 keeps
  one long-lived client per burst, and the push-stream generator yields the
  next item only after the engine has observed the previous `ResultMessage`
  (turn gate). Queue semantics for the client are unchanged.
- **1A no-go** — a3 FAIL (or b FAIL: session unusable after interrupt):
  Phase 5 uses Task 5.4b "one `query()` per turn + `resume`".
Also state per (b)–(h) any consequence for Phases 5/8 (e.g. delegation tool
name must be in `tools`, guard via hook instead of can_use_tool), and exactly
one sub-agent gate line: **sub-agent go**, **sub-agent go (built-ins in
session)** or **sub-agent no-go** (rules in Task 1A.7 Step 4).

## Follow-ups for later phases
Exact names/calls: ClaudeSDKClient.connect(prompt=<async gen>), interrupt(),
ResultMessage.subtype/terminal_reason after interrupt, session file layout under
CLAUDE_CONFIG_DIR, init tools list for tools=[] (+ delegate), RateLimitInfo
fields (resets_at unit), usage keys and per-turn vs cumulative cost,
can_use_tool vs allowed_tools shadowing, delegation tool name,
ToolPermissionContext field for sub-agent calls, parent_tool_use_id on
sub-agent StreamEvents, whether ResultMessage usage includes sub-agent tokens.
```

Decision rules are mechanical: apply them to the PASS/FAIL rows; do not soften.

- [ ] **Step 4: Run tests to verify they pass**

```bash
test -f spikes/1a-sdk-streaming/results/summary.md
grep -E '^## (Question\(s\)|Setup|Results|Decision|Follow-ups for later phases)$' docs/spikes/1a-sdk-streaming.md | wc -l   # 5
grep -E '1A (go|partial: gated generator|no-go)' docs/spikes/1a-sdk-streaming.md
grep -E 'sub-agent (go|go \(built-ins in session\)|no-go)' docs/spikes/1a-sdk-streaming.md
grep -niE 'claude login|/login' -r spikes/1a-sdk-streaming docs/spikes/1a-sdk-streaming.md || echo "no login flow"
cd spikes/1a-sdk-streaming && uv run ruff check . && uv run ruff format --check .
```
Expected: 5 headings, one decision line, one sub-agent gate line, `no login flow`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add spikes/1a-sdk-streaming/run_all.py docs/spikes/1a-sdk-streaming.md
git commit -m "docs: record spike 1A results and decision

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Then open the PR (`gh pr create --title "foundation phase 1A: spike Python Agent SDK streaming input"`),
paste `results/summary.md` and the Decision section into the body, and report
the decision to the coordinator — it gates Phase 5.

---

## Phase self-review

- Spec coverage: §5 step 2 (lazy pull, "to verify early") → Task 1A.2; §5 step 7
  (interrupt) → 1A.3; §5 step 8 (resume + fork at burst start) → 1A.4; §5 step 9
  and C7 `usage`/`rate-limited` frames → 1A.6; §3/§4.1 "built-in tools disabled
  for the main agent" → 1A.5 (d); §8 `can_use_tool` path guard and
  part-researcher web/write → 1A.5 (g), 1A.7 (h3); owner decision 2026-10-16
  (part-researcher as real SDK sub-agent behind a guarded `Agent` tool) →
  1A.7 (h4, h5) and the sub-agent gate; §11 "first plan steps" → whole
  phase; §12 no login flow → Task 1A.1 skip path + 1A.8 grep.
- Index gate 1A is applied mechanically in Task 1A.8, with one addition: the
  "partial: gated generator" outcome. Source reading (`stream_input` iterates
  the prompt iterable eagerly) makes a1 likely to FAIL while a3 should PASS;
  the gated generator keeps Phase 5's single-client design without switching to
  Task 5.4b. The coordinator should note this outcome in the index gate list.
- Deviation: spike scripts accept `ANTHROPIC_API_KEY` as well as
  `CLAUDE_CODE_OAUTH_TOKEN` (both allowed by Global Constraints); the SKIP line
  names `CLAUDE_CODE_OAUTH_TOKEN` as required by the assignment.
- Review Focus: no Review Focus line is owned by this phase.
