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
            log(
                f"ResultMessage #{len(out.results)} subtype={msg.subtype} "
                f"terminal_reason={getattr(msg, 'terminal_reason', None)}"
            )
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
