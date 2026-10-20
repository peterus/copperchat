"""Golden scenario: requirements → schematic handover (spec §14.3), end to end through
AgentRuntime with the scripted fake backend and the real doc tools. KiCad is stopped."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from copperchat.agent.events import QueuedItem
from copperchat.agent.fake import Text, Tool
from copperchat.agent.profiles import SCH_WRITES
from copperchat.sessions import CLOSE_TEXT

from .conftest import Env

REQ = "requirements.md"
SECRET = "S1-secret-chat-phrase"
FILLED = (
    "# Requirements\n\n## Function\n\nA USB-powered LED blinker.\n\n## Interfaces\n\nUSB-C.\n\n"
    "## Power\n\nTBD\n\n## Environment\n\nIndoor.\n\n## Mechanical\n\n30 x 20 mm.\n\n"
    "## Constraints\n\n2 layers.\n\n## Acceptance criteria\n\nLED blinks at 1 Hz.\n\n"
    "## Open points\n\n- enclosure colour\n"
)
PARTS = (
    "# Parts\n\n## Key parts\n\n| Role | MPN | Rationale | Status | Notes |\n"
    "|---|---|---|---|---|\n| MCU | ATTINY85 | small | candidate | |\n\n## Open points\n"
)


def _results(env: Env, rows: list[Any]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.kind == "tool_result":
            data = json.loads(r.content) if r.content.startswith("{") else {"text": r.content}
            out.append({**data, "is_error": bool(r.metadata.get("isError"))})
    return out


@pytest.mark.parametrize("approved", [True, False], ids=["approved", "gate"])
async def test_requirements_to_schematic_handover(env: Env, approved: bool) -> None:
    s1 = await env.create("requirements")
    old_version: dict[str, str | None] = {}

    def s1_script(item: QueuedItem) -> list[Any]:
        if "start" in item.text:  # step 1
            return [
                Tool("doc_read", {"path": REQ}),
                Tool(
                    "doc_write",
                    {"path": REQ, "body": FILLED, "base_version": None, "summary": "first draft"},
                ),
                Tool("sch_connect", {"from": "U1.1", "to": "R1.1"}),  # out of profile
                Text(f"Drafted the requirements. {SECRET}"),
            ]
        if "power" in item.text:
            return [
                Tool(
                    "doc_patch_section",
                    {
                        "path": REQ,
                        "heading": "Power",
                        "body": "5 V from USB-C, 100 mA.",
                        "base_version": env.version(REQ),
                        "summary": "power",
                    },
                ),
                *(
                    Tool(
                        "doc_append",
                        {
                            "path": "decisions.md",
                            "title": title,
                            "text": f"{title}: because.",
                            "summary": title,
                        },
                    )
                    for title in ("Use USB-C power", "Use an ATtiny85")
                ),
                Tool(
                    "doc_write",
                    {"path": "parts.md", "body": PARTS, "base_version": None, "summary": "parts"},
                ),
            ]
        if "mechanical" in item.text:  # step 3: the stale version, then re-read and patch
            return [
                Tool(
                    "doc_patch_section",
                    {
                        "path": REQ,
                        "heading": "Mechanical",
                        "body": "30 x 20 mm, two M2 holes.",
                        "base_version": old_version["req"],
                        "summary": "holes",
                    },
                ),
                Tool("doc_read", {"path": REQ}),
                Tool(
                    "doc_patch_section",
                    {
                        "path": REQ,
                        "heading": "Mechanical",
                        "body": "30 x 20 mm, two M2 holes.",
                        "base_version": env.version(REQ),
                        "summary": "holes",
                    },
                ),
            ]
        if CLOSE_TEXT in item.text:  # step 4
            return [
                Tool(
                    "doc_patch_section",
                    {
                        "path": REQ,
                        "heading": "Open points",
                        "body": "- confirm the LED colour",
                        "base_version": env.version(REQ),
                        "mode": "append",
                        "summary": "handover",
                    },
                ),
                Text("Requirements are complete. Next: draw the schematic."),
            ]
        return [Text("ok")]

    env.script = s1_script
    # 1. requirements session: template, write, patch, decisions, parts
    await env.send(s1, f"start the requirements ({SECRET})")
    first = _results(env, await env.rows(s1))
    assert first[0]["exists"] is False and "## Power" in first[0]["template"]
    assert first[1]["is_error"] is False
    assert first[2]["is_error"] is True and "not available" in first[2]["text"]  # refused
    await env.send(s1, "now the power")
    assert "5 V from USB-C" in (env.design / REQ).read_text()
    decisions = (env.design / "decisions.md").read_text()
    assert "Use USB-C power" in decisions and "Use an ATtiny85" in decisions
    assert "| candidate |" in (env.design / "parts.md").read_text()

    # 2. the user edits requirements.md and approves it
    old_version["req"] = env.version(REQ)
    edited = (
        (env.design / REQ).read_text().split("---\n", 2)[-1].replace("Indoor.", "Indoor, 0-40 °C.")
    )
    user = await env.docs.write(env.project, REQ, edited, old_version["req"], author="user")
    await env.docs.approve(env.project, REQ, user.version)
    assert (await env.docs.read(env.project, REQ)).approved

    # 3. a patch with the old version conflicts; re-read, patch: draft again; re-approve
    await env.send(s1, "add the mechanical holes")
    third = _results(env, await env.rows(s1))[-3:]
    assert third[0]["is_error"] is True and third[0]["error"]["code"] == "doc_conflict"
    assert third[2]["is_error"] is False
    doc = await env.docs.read(env.project, REQ)
    assert not doc.approved and "two M2 holes" in doc.body and "0-40 °C" in doc.body
    if approved:
        await env.docs.approve(env.project, REQ, doc.version or "")

    # 4. close: the handover patches Open points, the summary becomes a decision
    r = await env.client.post(f"/api/projects/{env.pid}/conversations/{s1}/close")
    assert r.status_code == 202
    await env.idle()
    assert (await env.conv(s1)).status == "closed"
    decisions = (env.design / "decisions.md").read_text()
    assert re.search(
        r"## \d{4}-\d\d-\d\d — Session 1 closed\n"
        r"Requirements are complete\. Next: draw the schematic\.\n\n"
        r"\*Session: Session 1 \(#1, requirements\)\*",
        decisions,
    )
    final = await env.docs.read(env.project, REQ)
    assert "confirm the LED colour" in final.body
    assert final.approved is False  # the handover changed it after the approval
    if approved:
        await env.docs.approve(env.project, REQ, final.version or "")

    # 5. a schematic session starts from the docs, not from session 1's chat
    s2 = await env.create("schematic")
    env.script = lambda item: [Text("reading the docs")]
    await env.send(s2, "draw it")
    s2_bursts = [b for b in env.agent.bursts if b.conversation_id == s2]
    prompt = s2_bursts[0].prompts[0]
    if approved:
        assert "- requirements.md — approved (" in prompt
        assert "Gate:" not in prompt
    else:
        assert "- requirements.md — draft" in prompt
        assert "Gate: requirements.md is draft (not approved by the user; 2 open points)" in prompt
    fence = re.search(
        r"<<<doc requirements\.md ([0-9a-f]{16})>>>\n(.*?)\n<<<end \1>>>", prompt, re.S
    )
    assert fence is not None and "two M2 holes" in fence.group(2)
    assert SECRET not in prompt and "Drafted the requirements" not in prompt
    assert '"Session 1 closed" (Session 1)' in prompt  # the decisions index row

    # 6. tool lists per burst
    s1_bursts = [b for b in env.agent.bursts if b.conversation_id == s1]
    assert s1_bursts and all(b.conversation_type == "requirements" for b in s1_bursts)
    for b in s1_bursts:
        assert not set(b.tool_names) & SCH_WRITES
        assert "doc_write" in b.tool_names
    assert set(s2_bursts[0].tool_names) >= SCH_WRITES
    assert "doc_write" not in s2_bursts[0].tool_names
