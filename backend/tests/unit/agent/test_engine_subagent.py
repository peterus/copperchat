import asyncio

from copperchat.agent.events import SubagentText, ToolResultEvt, ToolUse
from copperchat.agent.fake import Delegate, Emit, Text, Tool, Wait
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.tools import ImageBlock

PID = "a1b2c3d4e5f6"
CID = "c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"


async def _assistant_rows(db):  # type: ignore[no-untyped-def]
    rows, _ = await MessageRepo(db).page(PID, CID, limit=100)
    return [r for r in rows if r.role == "assistant"]


async def test_subagent_rows_are_complete_and_tagged(make_engine, db, until):
    agent_input = {"subagent_type": "part-researcher", "description": "TPS62133", "prompt": "q"}
    engine, _ = await make_engine(
        lambda item: [
            Text("Let me research. "),
            Emit(ToolUse(id="a1", name="Agent", input=agent_input)),
            Emit(SubagentText(parent_tool_use_id="a1", text="Searching the web")),
            Emit(
                ToolUse(
                    id="w1",
                    name="Write",
                    parent_tool_use_id="a1",
                    input={"file_path": "docs/parts/TPS62133/notes.md", "content": "x" * 5000},
                )
            ),
            Emit(ToolResultEvt(id="w1", content="y" * 9000, parent_tool_use_id="a1")),
            Emit(ToolResultEvt(id="a1", content="TPS62133 summary")),
            Text("Done."),
        ]
    )
    await engine.submit_ex(CID, "c", "research it", [])
    await until(lambda: not engine.is_busy and engine.current_run_id is None)
    assistant = await _assistant_rows(db)
    assert [(r.kind, r.metadata.get("parentToolUseId")) for r in assistant] == [
        ("text", None),
        ("tool_use", None),
        ("text", "a1"),
        ("tool_use", "a1"),
        ("tool_result", "a1"),
        ("tool_result", None),
        ("text", None),
    ]
    assert all(r.status == "done" for r in assistant)
    assert assistant[1].metadata["name"] == "Agent"
    assert assistant[1].metadata["input"] == agent_input
    assert assistant[3].metadata["name"] == "Write"
    assert len(assistant[3].metadata["input"]["content"]) == 2000
    assert assistant[3].metadata["input"]["file_path"] == "docs/parts/TPS62133/notes.md"
    assert len(assistant[4].content) == 4000
    assert assistant[5].content == "TPS62133 summary"  # the Agent call's own result
    assert assistant[0].content == "Let me research. " and assistant[6].content == "Done."
    assert len({r.run_id for r in assistant}) == 1


async def test_subagent_text_never_touches_the_open_main_segment(make_engine, db, until, drain):
    engine, _ = await make_engine(
        lambda item: [
            Text("main "),
            Emit(SubagentText(parent_tool_use_id="a1", text="sub")),
            Text("still main"),
        ]
    )
    sub = engine.broadcaster.subscribe(PID)
    await engine.submit_ex(CID, "c", "go", [])
    await until(lambda: not engine.is_busy)
    assistant = await _assistant_rows(db)
    assert [(r.kind, r.content, r.metadata.get("parentToolUseId")) for r in assistant] == [
        ("text", "main still main", None),
        ("text", "sub", "a1"),
    ]
    frames = await drain(sub)
    sub_row = next(
        f for f in frames if f.type == "message" and f.message.metadata.get("parentToolUseId")
    )
    assert sub_row.message.status == "done"
    deltas = [f for f in frames if f.type == "message-delta" and f.content_delta]
    assert all(f.message_id == assistant[0].id for f in deltas)


async def test_subagent_tool_result_images_are_dropped(make_engine, db, until, project):
    img = ImageBlock(mime="image/png", data=b"PNG")
    engine, _ = await make_engine(
        lambda item: [
            Emit(ToolResultEvt(id="w1", content="page", images=(img,), parent_tool_use_id="a1"))
        ]
    )
    await engine.submit_ex(CID, "c", "go", [])
    await until(lambda: not engine.is_busy)
    assistant = await _assistant_rows(db)
    assert [(r.kind, r.metadata) for r in assistant] == [
        (
            "tool_result",
            {"parentToolUseId": "a1", "toolUseId": "w1", "isError": False, "images": []},
        )
    ]
    assert not (project.dir / ".kicad-agent" / "images").exists()


async def test_fake_delegate_step_scripts_a_subagent(make_engine, db, until):
    engine, _ = await make_engine(
        lambda item: [
            Delegate(
                description="TPS62133",
                steps=(Text("Searching"), Tool("WebSearch", {"query": "TPS62133"})),
                summary="TPS62133: 3 A buck",
            ),
            Text("Here it is."),
        ]
    )
    await engine.submit_ex(CID, "c", "research", [])
    await until(lambda: not engine.is_busy)
    assistant = await _assistant_rows(db)
    agent_call = assistant[0]
    parent = agent_call.metadata["toolUseId"]
    assert agent_call.metadata["name"] == "Agent"
    assert agent_call.metadata["input"]["subagent_type"] == "part-researcher"
    assert [(r.kind, r.content, r.metadata.get("parentToolUseId")) for r in assistant] == [
        ("tool_use", "Agent", None),
        ("text", "Searching", parent),
        ("tool_use", "WebSearch", parent),
        ("tool_result", "WebSearch ok", parent),
        ("tool_result", "TPS62133: 3 A buck", None),
        ("text", "Here it is.", None),
    ]
    assert assistant[3].metadata["toolUseId"] == assistant[2].metadata["toolUseId"]


async def test_async_agent_launched_result_is_persisted_and_turn_ends(make_engine, db, until):
    # X20 (3): a backgrounded delegation answers the Agent call with "Async agent launched";
    # it is an ordinary main-agent tool_result.
    engine, agent = await make_engine(
        lambda item: (
            [
                Emit(ToolUse(id="a1", name="Agent", input={"subagent_type": "part-researcher"})),
                Emit(ToolResultEvt(id="a1", content="Async agent launched successfully.")),
                Text("Research started."),
            ]
            if item.text == "research"
            else [Text("two")]
        )
    )
    m1, _ = await engine.submit_ex(CID, "c", "research", [])
    await engine.submit_ex(CID, "d", "next", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assistant = await _assistant_rows(db)
    launched = [r for r in assistant if r.kind == "tool_result" and r.content.startswith("Async")]
    assert len(launched) == 1 and "parentToolUseId" not in launched[0].metadata
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "completed"


async def test_interrupt_during_subagent_ends_turn_and_next_runs(make_engine, db, until):
    never = asyncio.Event()
    engine, agent = await make_engine(
        lambda item: (
            [
                Delegate(description="X", steps=(Text("Searching"), Wait(never), Text("late"))),
                Text("never"),
            ]
            if item.text == "m1"
            else [Text("two")]
        )
    )
    m1, _ = await engine.submit_ex(CID, "a", "m1", [])
    await until(lambda: engine.current_run_id is not None)
    await until(lambda: len(agent.prompts) == 1)
    await asyncio.sleep(0.02)
    await engine.interrupt(engine.current_run_id)
    await until(lambda: not engine.is_busy)
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "interrupted"
    contents = [r.content for r in await _assistant_rows(db)]
    assert "Searching" in contents and "late" not in contents and "never" not in contents
    await engine.submit_ex(CID, "b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert "two" in [r.content for r in await _assistant_rows(db)]
