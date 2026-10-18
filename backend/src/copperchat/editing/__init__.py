"""SP2 editing state, gate and change log (E-C5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copperchat.editing.service import EditingService

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.agent.engine import RunEngine, RunHooks
    from copperchat.projects import Project

__all__ = ["EditingService", "install_editing"]


def install_editing(app: FastAPI) -> EditingService:
    """Lifespan: after install_history, before resume_pending (engines keep the hooks
    and services they were created with). Sets `runtime.editing` (passed into every
    ToolContext) and chains EditingHooks after the history hooks."""
    from copperchat.agent.engine import ChainedHooks
    from copperchat.db.repos.editing_settings import EditingSettingsRepo
    from copperchat.editing.hooks import EditingHooks

    runtime = app.state.agent_runtime
    svc = EditingService(
        db=app.state.db,
        broadcaster=runtime.broadcaster,
        settings_repo=EditingSettingsRepo(app.state.db),
    )
    previous = runtime.hooks_factory

    def hooks_factory(project: Project, engine: RunEngine) -> RunHooks:
        return ChainedHooks([previous(project, engine), EditingHooks(svc, project, engine)])

    runtime.hooks_factory = hooks_factory
    runtime.editing = svc
    app.state.editing = svc
    return svc
