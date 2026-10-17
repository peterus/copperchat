"""``research_part``: runs the part-researcher as a separate SDK query (owner decision 8).

One :class:`ResearchRunner` belongs to one main SDK client (burst). The main agent's
guard reports every allowed ``research_part`` call with its ``tool_use_id``
(:meth:`ResearchRunner.note_call`); the MCP handler then calls :meth:`ResearchRunner.run`
with the same input. The research query's messages are forwarded through ``emit`` as
AgentEvents tagged ``parent_tool_use_id`` = that id, so they become sub-agent rows under
the "Part research: <MPN>" card. The query's usage is collected for the main turn
(:meth:`take_usage`); :meth:`cancel_all` (interrupt) stops running queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
)

from copperchat.agent.events import AgentEvent
from copperchat.agent.sdk_messages import Totals, result_totals, subagent_events, usage_totals
from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.parts import InvalidMpn, mpn_dirname
from copperchat.projects import Project
from copperchat.research.agent import MAX_TURNS, PARTS_SERVER, RESEARCHER_TOOLS, researcher_prompt
from copperchat.research.datasheets import parts_mcp_server
from copperchat.research.net import Resolver
from copperchat.research.paths import parts_root, resolve_inside, uploads_image_root

log = logging.getLogger(__name__)

QueryFn = Callable[[str, ClaudeAgentOptions], AsyncIterator[Any]]
DEFAULT_TIMEOUT_S = 900.0
MAX_QUERY_CHARS = 4000
MAX_ATTACHMENTS = 10
MAX_BUFFER_BYTES = 64 * 1024 * 1024
_part_locks: dict[tuple[str, str], asyncio.Lock] = {}


def researcher_options(
    project: Project,
    settings: Settings,
    notes_skill: str | None = None,
    *,
    resolver: Resolver | None = None,
    write_folder: str | None = None,
) -> ClaudeAgentOptions:
    """Isolated options: researcher built-ins + the ``parts`` server, every call through the
    researcher guard (nothing in ``allowed_tools``), no user/project settings loaded."""
    from copperchat.agent.guards import (  # guards import research.*
        guard_as_pre_tool_use_hook,
        make_researcher_guard,
    )

    env = {
        "CLAUDE_CONFIG_DIR": str(settings.data_dir / "claude"),
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    }
    if settings.claude_code_oauth_token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token.get_secret_value()
    if settings.anthropic_api_key is not None:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key.get_secret_value()
    guard = make_researcher_guard(project, resolver=resolver, write_folder=write_folder)
    return ClaudeAgentOptions(
        system_prompt=researcher_prompt(notes_skill),
        tools=list(RESEARCHER_TOOLS),
        allowed_tools=[],
        permission_mode="default",
        can_use_tool=guard,
        # Also as a hook: the CLI skips can_use_tool for Read/Glob inside cwd.
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[guard_as_pre_tool_use_hook(guard)])]
        },
        mcp_servers={PARTS_SERVER: parts_mcp_server(project, resolver=resolver)},
        setting_sources=[],
        cwd=str(project.dir),
        env=env,
        max_turns=MAX_TURNS,
        # Reading a datasheet PDF returns it as one base64 document block; the SDK's
        # 1 MiB default line buffer is far too small (live eval, 2026-10-17).
        max_buffer_size=MAX_BUFFER_BYTES,
    )


async def sdk_query(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
    """One research turn on its own client; disconnect (kills the CLI) on any exit."""
    client = ClaudeSDKClient(options)
    await client.connect()
    try:
        await client.query(prompt)
        async for msg in client.receive_response():
            yield msg
    finally:
        await client.disconnect()


def _call_key(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


class ResearchRunner:
    def __init__(
        self,
        project: Project,
        settings: Settings,
        *,
        emit: Callable[[AgentEvent], None],
        notes_skill: str | None = None,
        query_fn: QueryFn | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        resolver: Resolver | None = None,
    ) -> None:
        self._project = project
        self._settings = settings
        self._emit = emit
        self._notes_skill = notes_skill
        self._query_fn = query_fn or sdk_query
        self._timeout_s = timeout_s
        self._resolver = resolver
        self._pending: defaultdict[str, deque[str]] = defaultdict(deque)
        self._tasks: set[asyncio.Task[str]] = set()
        self._usage: Totals | None = None

    # ---- main-agent side
    def note_call(self, tool_input: dict[str, Any], tool_use_id: str | None) -> None:
        """The guard allowed a research_part call with this input (FIFO per input)."""
        if tool_use_id:
            self._pending[_call_key(tool_input)].append(tool_use_id)

    def end_turn(self) -> None:
        """Forget recorded calls that never reached :meth:`run` (failed before it)."""
        self._pending.clear()

    def take_usage(self) -> Totals | None:
        usage, self._usage = self._usage, None
        return usage

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    # ---- research_part handler
    def _prompt(self, args: dict[str, Any]) -> tuple[str, str]:
        try:
            folder = mpn_dirname(str(args.get("mpn") or ""))
        except InvalidMpn as exc:
            raise StructuredToolError(
                "Invalid part number", str(exc), "Pass the bare manufacturer part number as mpn"
            ) from exc
        query = str(args.get("query") or "").strip()
        if not query:
            raise StructuredToolError("No question", "query is empty", "Say what you need to know")
        if len(query) > MAX_QUERY_CHARS:
            raise StructuredToolError(
                "Question too long", f"more than {MAX_QUERY_CHARS} characters", "Shorten it"
            )
        raw_paths = args.get("attachment_paths") or []
        if not isinstance(raw_paths, list) or len(raw_paths) > MAX_ATTACHMENTS:
            raise StructuredToolError(
                "Attachment paths rejected",
                f"attachment_paths must be a list of at most {MAX_ATTACHMENTS} paths",
                "Pass the paths exactly as shown in the attachment note",
            )
        readable = [parts_root(self._project), uploads_image_root(self._project)]
        paths: list[str] = []
        for p in raw_paths:
            real = resolve_inside(str(p), self._project.dir, readable)
            if real is None or not real.is_file():
                raise StructuredToolError(
                    "Attachment path rejected",
                    f"{p!r} is not a file under docs/parts/ or .kicad-agent/uploads/",
                    "Pass the path exactly as shown in the attachment note",
                )
            paths.append(real.relative_to(self._project.dir.resolve()).as_posix())
        prompt = (
            f"Part: {args.get('mpn')}\nNotes folder: docs/parts/{folder}/\n\nQuestion from the "
            f"design agent:\n{query}"
        )
        if paths:
            prompt += "\n\nAttached files (read these first):\n" + "\n".join(
                f"- {p}" for p in paths
            )
        return folder, prompt

    async def run(self, args: dict[str, Any]) -> str:
        queue = self._pending.get(_call_key(args))
        parent = queue.popleft() if queue else None
        if parent is None:
            log.warning("research_part call without a recorded tool_use_id; steps not forwarded")
        folder, prompt = self._prompt(args)
        task = asyncio.create_task(self._collect(prompt, parent, folder))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if task.cancelled() and me is not None and me.cancelling() == 0:
                # Only the research was cancelled (cancel_all): report it to the model.
                raise StructuredToolError(
                    "Part research was cancelled",
                    "the user stopped the turn",
                    "Do not retry unless the user asks again",
                ) from None
            raise
        finally:
            if not task.done():
                task.cancel()

    def _add_usage(self, totals: Totals) -> None:
        self._usage = totals if self._usage is None else self._usage + totals

    async def _collect(self, prompt: str, parent: str | None, folder: str) -> str:
        # One query per part folder at a time (two never write the same notes.md);
        # different parts research in parallel.
        # casefold: on a case-insensitive filesystem LM358 and lm358 are one folder.
        lock = _part_locks.setdefault((self._project.id, folder.casefold()), asyncio.Lock())
        options = researcher_options(
            self._project,
            self._settings,
            self._notes_skill,
            resolver=self._resolver,
            write_folder=folder,
        )
        final: ResultMessage | None = None
        # Fallback when no ResultMessage arrives (interrupt, timeout, crash): tokens of the
        # assistant messages seen so far, one entry per API message (blocks repeat it).
        partial: dict[str, Totals] = {}
        try:
            async with asyncio.timeout(self._timeout_s), lock:
                async for msg in self._query_fn(prompt, options):
                    if isinstance(msg, ResultMessage):
                        final = msg
                        self._add_usage(result_totals(msg))
                        continue
                    if isinstance(msg, AssistantMessage) and msg.usage:
                        key = msg.message_id or f"#{len(partial)}"
                        partial[key] = usage_totals(msg.usage)
                    if parent is not None:
                        for ev in subagent_events(msg, parent):
                            self._emit(ev)
        except TimeoutError as exc:
            raise StructuredToolError(
                "Part research timed out",
                f"exceeded {int(self._timeout_s)} s",
                "Ask a narrower question",
            ) from exc
        except StructuredToolError:
            raise
        except Exception as exc:  # CLI/transport failure of the research query
            log.exception("part research query failed")
            raise StructuredToolError(
                "Part research failed",
                f"{type(exc).__name__}: {exc}"[:500],
                "Retry once; if it fails again, ask the user for the datasheet",
            ) from exc
        finally:
            if final is None:
                for totals in partial.values():
                    self._add_usage(totals)
        if final is None or final.is_error or not final.result:
            raise StructuredToolError(
                "Part research failed",
                str(final.result or final.subtype) if final is not None else "no result",
                "Retry later or ask the user for a datasheet",
            )
        return final.result
