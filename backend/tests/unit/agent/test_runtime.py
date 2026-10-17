import asyncio
from typing import Any

from claude_agent_sdk import ResultMessage

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import RunHooks
from copperchat.agent.fake import FakeAgent
from copperchat.agent.runtime import AgentRuntime, default_backend_factory
from copperchat.agent.sdk_backend import SdkAgentBackend, SdkStreamingBackend
from copperchat.config import Settings
from copperchat.tools import registry

PID = "a1b2c3d4e5f6"


class _Sessions:
    def current(self) -> None:
        return None


def test_backend_selection(settings: Settings) -> None:
    fake = settings.model_copy(update={"dev_fake_agent": True})
    assert isinstance(default_backend_factory(fake, [])(lambda: None), FakeAgent)  # type: ignore[arg-type,return-value]
    real = default_backend_factory(settings, [])(lambda: None)  # type: ignore[arg-type,return-value]
    assert isinstance(real, SdkAgentBackend)
    assert SdkAgentBackend is SdkStreamingBackend  # spike 1A: gated generator (no 5.4b)


async def test_default_backend_factory_gets_registry_specs(settings: Settings, db: Any) -> None:
    rt = AgentRuntime(settings=settings, db=db, broadcaster=Broadcaster(), sessions=_Sessions())  # type: ignore[arg-type]
    backend = rt.backend_factory(lambda: None)  # type: ignore[arg-type,return-value]
    assert isinstance(backend, SdkStreamingBackend)
    names = {s.name for s in backend._specs}
    assert names == {s.name for s in registry.all_specs()} and names


async def test_ensure_creates_one_engine_per_project_with_hooks(
    settings: Settings, db: Any, project: Any
) -> None:
    made: list[Any] = []
    started: list[str] = []

    class Hooks(RunHooks):
        async def on_engine_start(self) -> None:
            started.append("yes")

    def hooks_factory(p: Any, engine: Any) -> RunHooks:
        made.append((p.id, engine))
        return Hooks()

    rt = AgentRuntime(
        settings=settings,
        db=db,
        broadcaster=Broadcaster(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        backend_factory=lambda ctx: FakeAgent(ctx_factory=ctx),
        hooks_factory=hooks_factory,
    )
    a, b = await asyncio.gather(rt.ensure(project), rt.ensure(project))
    assert a is b and rt.get(PID) is a and isinstance(a.hooks, Hooks)
    assert len(made) == 1 and started == ["yes"], "hooks are set before engine.start()"
    assert a.broadcaster is rt.broadcaster
    await rt.close_all()
    assert rt.get(PID) is None


async def test_usage_frame_from_sdk_result_message(
    settings: Settings, db: Any, project: Any, until: Any, drain: Any
) -> None:
    class Client:
        def __init__(self, options: Any) -> None:
            self.out: asyncio.Queue[Any] = asyncio.Queue()

        async def connect(self, prompt: Any) -> None:
            async def pump() -> None:
                async for _ in prompt:
                    await self.out.put(
                        ResultMessage(
                            subtype="success",
                            duration_ms=1,
                            duration_api_ms=1,
                            is_error=False,
                            num_turns=1,
                            session_id="s1",
                            total_cost_usd=0.25,
                            usage={
                                "input_tokens": 10,
                                "output_tokens": 3,
                                "cache_read_input_tokens": 7,
                                "cache_creation_input_tokens": 1,
                            },
                        )
                    )
                await self.out.put(None)

            self.task = asyncio.create_task(pump())

        async def receive_messages(self) -> Any:
            while (m := await self.out.get()) is not None:
                yield m

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    rt = AgentRuntime(
        settings=settings,
        db=db,
        broadcaster=Broadcaster(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        backend_factory=lambda ctx: SdkStreamingBackend(
            settings=settings, specs=[], ctx_factory=ctx, client_factory=Client
        ),
    )
    engine = await rt.ensure(project)
    sub = engine.broadcaster.subscribe(PID)
    await engine.submit_ex("c1", "hi", [])
    await until(lambda: not engine.is_busy)
    frames = await drain(sub)
    [usage] = [f for f in frames if f.type == "usage"]
    assert (usage.usage.input_tokens, usage.usage.output_tokens) == (10, 3)
    assert usage.usage.cache_read_tokens == 7 and usage.usage.cost_usd == 0.25
    await rt.close_all()
