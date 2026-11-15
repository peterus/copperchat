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


def test_registry_visible_for() -> None:
    r = VisibilityRegistry()
    assert r.visible_for("p1") is False
    r.set(1, "p1", True)
    r.set(2, "p1", False)
    assert r.visible_for("p1") is True
    r.set(1, "p1", False)
    assert r.visible_for("p1") is False
    r.set(3, "p1", True)
    r.drop(3)  # socket closed while visible
    assert r.visible_for("p1") is False


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_visible_claim_expires_without_fresh_client_frames() -> None:
    # A frozen page (iOS PWA in the background) never sends visible:false, and its socket
    # can stay open for hours: the claim only holds while the client keeps talking.
    clock = Clock()
    r = VisibilityRegistry(ttl_s=60, clock=clock)
    r.set(1, "p1", True)
    clock.now += 59
    assert r.visible_for("p1") is True
    clock.now += 2
    assert r.visible_for("p1") is False
    r.touch(1)  # any frame (e.g. the client's ping) proves the page is alive again
    assert r.visible_for("p1") is True
    r.touch(99)  # touching an unknown or hidden connection does not make it visible
    r.set(1, "p1", False)
    r.touch(1)
    assert r.visible_for("p1") is False


def test_visibility_is_per_project() -> None:
    r = VisibilityRegistry()
    r.set(1, "p1", True)
    assert r.visible_for("p1") is True
    assert r.visible_for("p2") is False  # a window on p1 does not hide pushes about p2
    r.set(1, "p2", True)  # the same window switched project
    assert r.visible_for("p1") is False
    assert r.visible_for("p2") is True
