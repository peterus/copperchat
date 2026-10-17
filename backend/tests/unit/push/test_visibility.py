import pytest
from pydantic import ValidationError

from copperchat.agent.protocol import VisibilityFrame, client_frame_adapter
from copperchat.push.visibility import VisibilityRegistry


def test_visibility_frame_parses() -> None:
    f = client_frame_adapter.validate_python(
        {"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": True}
    )
    assert isinstance(f, VisibilityFrame) and f.visible is True
    with pytest.raises(ValidationError):
        client_frame_adapter.validate_python(
            {"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": "x"}
        )


def test_registry_any_visible() -> None:
    r = VisibilityRegistry()
    assert r.any_visible() is False
    r.set(1, "p1", True)
    r.set(2, "p2", False)
    assert r.any_visible() is True
    r.set(1, "p1", False)
    assert r.any_visible() is False
    r.set(3, "p1", True)
    r.drop(3)  # socket closed while visible
    assert r.any_visible() is False
