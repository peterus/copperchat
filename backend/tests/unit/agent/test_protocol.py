# backend/tests/unit/agent/test_protocol.py
import json
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from copperchat.agent.protocol import (
    ClientFrame,
    MessageDeltaFrame,
    PongFrame,
    ServerFrame,
    client_frame_adapter,
    dump_frame,
    server_frame_adapter,
)

FIXTURES = Path(__file__).resolve().parents[4] / "protocol-fixtures"
CID = "0123456789abcdef0123456789abcdef"
ADAPTERS = {"c2s": client_frame_adapter, "s2c": server_frame_adapter}


def _types(union: object) -> set[str]:
    members = typing.get_args(typing.get_args(union)[0])
    return {typing.get_args(m.model_fields["type"].annotation)[0] for m in members}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixture_frames(path: Path) -> None:
    spec = json.loads(path.read_text())
    assert spec["type"] == path.stem
    assert spec["valid"], "every fixture needs at least one valid frame"
    for direction in spec["directions"]:
        adapter = ADAPTERS[direction]
        for frame in spec["valid"]:
            parsed = adapter.validate_python(frame)
            assert parsed.type == spec["type"]
            assert json.loads(dump_frame(parsed)) == frame, "fixtures must be canonical"
        for frame in spec["invalid"]:
            with pytest.raises(ValidationError):
                adapter.validate_python(frame)


def test_every_frame_type_has_a_fixture() -> None:
    present = {p.stem for p in FIXTURES.glob("*.json")}
    assert _types(ClientFrame) | _types(ServerFrame) == present


def test_delta_omits_absent_patches_and_pong_keeps_null_project() -> None:
    delta = MessageDeltaFrame(
        project_id="a1b2c3d4e5f6", conversation_id=CID, message_id=3, content_delta="x"
    )
    assert json.loads(dump_frame(delta)) == {
        "type": "message-delta",
        "projectId": "a1b2c3d4e5f6",
        "conversationId": CID,
        "messageId": 3,
        "contentDelta": "x",
    }
    assert json.loads(dump_frame(PongFrame(project_id=None))) == {"type": "pong", "projectId": None}


def test_delta_requires_a_patch() -> None:
    with pytest.raises(ValidationError):
        MessageDeltaFrame(project_id="a1b2c3d4e5f6", conversation_id=CID, message_id=3)


@pytest.mark.parametrize("ftype", ["send", "subscribe", "interrupt"])
def test_conversation_frames_require_conversation_id(ftype: str) -> None:
    spec = json.loads((FIXTURES / f"{ftype}.json").read_text())
    missing = [f for f in spec["invalid"] if "conversationId" not in f]
    assert missing, f"{ftype} needs an invalid case without conversationId"
