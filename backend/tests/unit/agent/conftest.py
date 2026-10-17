import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from pydantic import SecretStr

from copperchat.agent.broadcaster import Broadcaster, Subscription
from copperchat.agent.engine import RunEngine
from copperchat.agent.fake import FakeAgent
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project

PID = "a1b2c3d4e5f6"


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "demo"
    d.mkdir()
    return Project(
        id=PID,
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=d,
        pro_file=d / "demo.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("t"),
    )


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


@pytest_asyncio.fixture
async def make_engine(
    project: Project, settings: Settings, db: Database
) -> AsyncIterator[Callable[..., Any]]:
    engines: list[RunEngine] = []

    async def make(
        script_for: Any = None,
        *,
        ignore_interrupt: bool = False,
        specs: Any = (),
        with_session: bool = False,
        start: bool = True,
        backend: Any = None,
        **kw: Any,
    ) -> tuple[RunEngine, Any]:
        box: dict[str, Any] = {}
        session = SimpleNamespace(project=project, closed=False) if with_session else None

        def factory(ctx_factory: Callable[[], Any]) -> Any:
            box["agent"] = backend or FakeAgent(
                script_for, specs=specs, ctx_factory=ctx_factory, ignore_interrupt=ignore_interrupt
            )
            return box["agent"]

        engine = RunEngine(
            project=project,
            settings=settings,
            db=db,
            broadcaster=Broadcaster(),
            sessions=cast(Any, SimpleNamespace(current=lambda: session)),
            backend_factory=factory,
            **kw,
        )
        if start:
            await engine.start()
        engines.append(engine)
        return engine, box["agent"]

    yield make
    for e in engines:
        await e.close()


@pytest.fixture
def until() -> Callable[..., Any]:
    async def _until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not pred():
            if loop.time() > deadline:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.005)

    return _until


@pytest.fixture
def drain() -> Callable[..., Any]:
    async def _drain(sub: Subscription, quiet: float = 0.05) -> list[Any]:
        frames: list[Any] = []
        while True:
            try:
                frames.append(await asyncio.wait_for(sub.__anext__(), quiet))
            except (TimeoutError, StopAsyncIteration):
                return frames

    return _drain
