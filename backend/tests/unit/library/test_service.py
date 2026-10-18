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


class FootprintSession(FakeSession):
    """Two footprint libraries; `Bad` fails to load."""

    def __init__(self, failing: Exception | None) -> None:
        super().__init__()
        self.project = type("P", (), {"pcb_file": "x.kicad_pcb"})()
        self.failing = failing
        self.loads: list[str] = []

    async def call(self, fn: Any) -> Any:
        func = getattr(fn, "func", None)
        if func is service_mod._footprint_names:
            nickname = fn.args[0]
            self.loads.append(nickname)
            if nickname == "Bad_SMD" and self.failing is not None:
                raise self.failing
            return ["R_0805_2012Metric"]
        if func is service_mod._fetch_footprints:
            from copperchat.library.packages import FootprintDetail

            nickname, names = fn.args
            return [
                FootprintDetail(f"{nickname}:{n}", "Resistor SMD 0805", 2, 2, "0805") for n in names
            ]
        return await super().call(fn)


@pytest.mark.parametrize("failing", ["api", "doc"])
async def test_failing_footprint_library_is_skipped(failing: str) -> None:
    from kipy.errors import ApiError

    from copperchat.errors import DocumentNotOpen

    error = ApiError("bad table row") if failing == "api" else DocumentNotOpen("no board")
    session = FootprintSession(error)
    svc = _svc(session)
    svc._fp_scopes = {"Bad_SMD": "global", "Good_SMD": "global"}
    rows, failed = await svc.search_footprints_detailed("smd 0805")
    assert [r["lib_id"] for r in rows] == ["Good_SMD:R_0805_2012Metric"]
    assert set(failed) == {"Bad_SMD"}
    # remembered: an unscoped search does not retry the broken library
    rows2, failed2 = await svc.search_footprints_detailed("smd 0805")
    assert rows2 == rows and set(failed2) == {"Bad_SMD"}
    assert session.loads.count("Bad_SMD") == 1
    # an explicit library request retries and raises
    with pytest.raises(StructuredToolError) as err:
        await svc.search_footprints("0805", library="Bad_SMD")
    assert err.value.code == "library_error"
    assert session.loads.count("Bad_SMD") == 2


async def test_failed_library_recovers_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from kipy.errors import ApiError

    session = FootprintSession(ApiError("busy"))
    svc = _svc(session)
    svc._fp_scopes = {"Bad_SMD": "global", "Good_SMD": "global"}
    _, failed = await svc.search_footprints_detailed("smd 0805")
    assert set(failed) == {"Bad_SMD"}
    monkeypatch.setattr(service_mod, "FAILED_RETRY_S", -1.0)  # entry expired
    _, failed = await svc.search_footprints_detailed("smd 0805")
    assert set(failed) == {"Bad_SMD"} and session.loads.count("Bad_SMD") == 2
    session.failing = None  # type: ignore[assignment]
    monkeypatch.setattr(service_mod, "FAILED_RETRY_S", 300.0)
    rows = await svc.search_footprints("0805", library="Bad_SMD")  # explicit retry succeeds
    assert [r["lib_id"] for r in rows] == ["Bad_SMD:R_0805_2012Metric"]
    _, failed = await svc.search_footprints_detailed("smd 0805")
    assert failed == {}
