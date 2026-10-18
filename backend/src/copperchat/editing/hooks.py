"""Editing run hook (E-C5): turn bookkeeping. E8a adds the turn-end ERC delta, E8b the
plan pause/turn handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.agent.engine import RunEngine
    from copperchat.editing.service import EditingService


class EditingHooks(RunHooks):
    def __init__(self, svc: EditingService, project: Project, engine: RunEngine) -> None:
        self.svc = svc
        self.project = project
        self.engine = engine

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        self.svc.drop_turn(self.project.id, turn.run_id)
