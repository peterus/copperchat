"""Design docs under docs/design/ (sessions phase C3): path guard, service, doc tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.design_docs.service import DesignDocs

__all__ = ["install_design_docs"]


def install_design_docs(app: FastAPI) -> DesignDocs:
    """Lifespan: after the agent runtime exists. ``app.state.docs`` serves the REST routes
    and (from C5) the tool contexts; ``doc-updated`` frames go through the runtime's
    broadcaster."""
    from copperchat.design_docs.service import DesignDocs

    runtime = app.state.agent_runtime
    docs = DesignDocs(app.state.db, app.state.settings, runtime.broadcaster)
    app.state.docs = docs
    return docs
