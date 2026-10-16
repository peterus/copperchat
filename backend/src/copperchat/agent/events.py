"""Internal events between an agent backend (SDK adapter or FakeAgent) and the RunEngine."""

from __future__ import annotations

from dataclasses import dataclass

from copperchat.tools import ImageBlock


@dataclass(frozen=True)
class ToolResultEvt:
    id: str
    content: str
    images: tuple[ImageBlock, ...] = ()
    is_error: bool = False
