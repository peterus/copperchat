"""LibraryService state machine and error mapping with a fake session (no KiCad)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession, LibrariesLoading
from copperchat.library import service as service_mod
from copperchat.library.index import SymbolRow
from copperchat.library.service import LibraryService


class FakeSession:
    read_only = False

    def __init__(self) -> None:
        self.fail_with: Exception | None = None
        self.release = asyncio.Event()
        self.release.set()
        self.calls = 0

    async def ensure_symbol_libraries(self) -> None:
        await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with

    async def call(self, fn: Any) -> Any:
        self.calls += 1
        name = getattr(fn, "__name__", "") or getattr(getattr(fn, "func", None), "__name__", "")
        if name == "run":  # _scopes(...)
            return {"Device": "global"}
        if name == "_symbol_ids":
            return [("Device", "R"), ("Device", "C")]
        if name == "_fetch_rows":
            return [SymbolRow("Device:R", "Resistor", "R res resistor", 1, 2)]
        raise AssertionError(f"unexpected call {fn!r}")


def _svc(session: FakeSession) -> LibraryService:
    return LibraryService(cast(KicadSession, session))


async def test_warm_up_ready_and_omitted_ids_indexed() -> None:
    session = FakeSession()  # the service holds the session weakly
    svc = _svc(session)
    assert not svc.started
    assert await svc.wait_ready(5)
    assert svc.started and svc.state == "ready"
    ids = [r["lib_id"] for r in await svc.search_symbols("resistor")]
    assert ids == ["Device:R"]
    assert [r["lib_id"] for r in await svc.search_symbols("C")] == ["Device:C"]


async def test_loading_then_wait_timeout() -> None:
    session = FakeSession()
    session.release.clear()
    svc = _svc(session)
    assert await svc.wait_ready(0.05) is False
    assert svc.state == "loading"
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("resistor")
    assert err.value.code == "libraries_loading"
    session.release.set()
    assert await svc.wait_ready(5)


async def test_failed_warm_up_is_retried() -> None:
    session = FakeSession()
    session.fail_with = LibrariesLoading("still loading after 120s")
    svc = _svc(session)
    assert await svc.wait_ready(5) is False
    assert svc.state == "failed" and "120s" in (svc.error or "")
    session.fail_with = None
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("resistor")  # restarts the warm-up
    assert err.value.code == "libraries_loading" and "failed" in err.value.why
    assert await svc.wait_ready(5)


async def test_invalid_arguments() -> None:
    session = FakeSession()
    svc = _svc(session)
    assert await svc.wait_ready(5)
    for kwargs in ({"query": " "}, {"query": "x" * 201}, {"query": "r", "limit": "5"}):
        with pytest.raises(StructuredToolError) as err:
            await svc.search_symbols(**kwargs)  # type: ignore[arg-type]
        assert err.value.code == "invalid_argument"
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("r", library="Devic")
    assert err.value.code == "not_found"


async def test_kicad_errors_are_structured() -> None:
    from kipy.errors import ApiError

    with pytest.raises(StructuredToolError) as err, service_mod._kicad_errors("Could not read X"):
        raise ApiError("boom")
    assert err.value.code == "library_error" and "boom" in err.value.why
    with pytest.raises(StructuredToolError) as err, service_mod._kicad_errors("Could not read X"):
        raise LibrariesLoading("slow")
    assert err.value.code == "libraries_loading"
