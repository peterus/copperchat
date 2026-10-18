"""Library service against a real api-server with the stock libraries (phase E3)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable

import pytest

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession, KicadSessionManager
from copperchat.library.service import LibraryService, library_for
from copperchat.projects import discover_projects
from tests.kicad.edit.helpers import item_ids
from tests.support.kicad_project import copy_fixture, real_settings

pytestmark = pytest.mark.kicad


async def _ready(session: KicadSession) -> LibraryService:
    svc = library_for(session)
    assert await svc.wait_ready(120), svc.error
    return svc


@pytest.fixture
async def boardless_session(make_settings: Callable[..., Settings]) -> AsyncIterator[KicadSession]:
    """The `blank` fixture copied without its .kicad_pcb (owner decision 3)."""
    settings = real_settings(make_settings)
    dest = copy_fixture("blank", settings.projects_root)
    (dest / "blank.kicad_pcb").unlink()
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "blank")
    assert project.pcb_file is None
    manager = KicadSessionManager(settings)
    try:
        yield await manager.open(project)
    finally:
        await manager.close_all()


# -- Task 1: index and warm-up ---------------------------------------------------------


async def test_warm_up_and_search(blank_session: KicadSession) -> None:
    svc = await _ready(blank_session)
    assert svc.state == "ready"
    for query, expected, top in (
        ("resistor", "Device:R", 5),
        ("LM358", "Amplifier_Operational:LM358", 5),
        ("ECC83", "ecc83-pp:ECC83", 5),
    ):
        t = time.monotonic()
        rows = await svc.search_symbols(query, limit=top)
        assert time.monotonic() - t < 0.3, query
        assert expected in [r["lib_id"] for r in rows], (query, rows)
    ecc = next(r for r in await svc.search_symbols("ECC83") if r["lib_id"] == "ecc83-pp:ECC83")
    assert ecc["scope"] == "project"
    rows = await svc.search_symbols("resistor", library="Device", limit=50)
    assert rows and all(r["lib_id"].startswith("Device:") for r in rows)
    r = next(r for r in rows if r["lib_id"] == "Device:R")
    assert r["pin_count"] == 2 and r["unit_count"] == 1 and "R_*" in r["footprint_filters"]
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("resistor", library="Devic")
    assert err.value.code == "not_found" and "Device" in err.value.suggestion
    assert library_for(blank_session) is svc


async def test_search_while_loading_errors(blank_session: KicadSession) -> None:
    svc = LibraryService(blank_session)
    await svc.start()
    assert svc.state == "loading"
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("resistor")
    assert err.value.code == "libraries_loading"
    assert err.value.to_dict()["code"] == "libraries_loading"
    assert await svc.wait_ready(120)
    assert await svc.search_symbols("resistor")


# -- Task 2: symbol detail --------------------------------------------------------------


async def test_derived_symbol_pins_via_probe(blank_session: KicadSession) -> None:
    svc = await _ready(blank_session)
    lib_id = "MCU_ST_STM32F1:STM32F103C8Tx"
    before = next(r for r in await svc.search_symbols("STM32F103C8Tx") if r["lib_id"] == lib_id)
    assert before["pin_count"] is None  # derived symbol: no pins in the library listing
    detail = await svc.get_symbol(lib_id)
    assert len({p.number for p in detail.pins}) == 48 and detail.pins_known
    assert detail.footprint == "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    assert detail.body_size_mm[0] > 10 and detail.body_size_mm[1] > 10
    assert detail.pin_directions_by_rotation is None
    after = next(r for r in await svc.search_symbols("STM32F103C8Tx") if r["lib_id"] == lib_id)
    assert after["pin_count"] == 48
    assert await svc.get_symbol(lib_id) is detail  # cached


async def test_probe_leaves_document_unchanged(blank_session: KicadSession) -> None:
    s = blank_session
    svc = await _ready(s)
    before = await s.call(item_ids)
    await svc.get_symbol("Amplifier_Operational:LM358")  # derived (extends)
    assert await s.call(item_ids) == before
    assert await s.call(lambda h: h.schematic().is_document_modified()) is False
    assert s.modified is False and s.jobs_stale is False


async def test_pin_directions_by_rotation_for_small_parts(blank_session: KicadSession) -> None:
    svc = await _ready(blank_session)
    led = await svc.get_symbol("Device:LED")
    dirs = led.pin_directions_by_rotation
    assert dirs is not None and set(dirs) == {"0", "90", "180", "270"}
    names = {p.number: p.name for p in led.pins}
    assert names == {"1": "K", "2": "A"}
    assert dirs["0"] == {"1": "left", "2": "right"}
    assert dirs["180"] == {"1": "right", "2": "left"}
    assert led.reference_prefix == "D"
    assert 7.62 <= led.body_size_mm[0] < 10 and 2 < led.body_size_mm[1] < 6
    assert (await svc.get_symbol("74xx:74LS00")).pin_directions_by_rotation is None


async def test_multi_unit_and_body_styles(blank_session: KicadSession) -> None:
    svc = await _ready(blank_session)
    d = await svc.get_symbol("74xx:74LS00")
    assert d.unit_count == 5 and d.body_styles == 2
    keys = [(p.unit, p.number) for p in d.pins]
    assert len(keys) == len(set(keys)) == 14
    assert {p.number for p in d.pins if p.unit == 5} == {"7", "14"}


async def test_unknown_symbol_not_found(blank_session: KicadSession) -> None:
    svc = await _ready(blank_session)
    with pytest.raises(StructuredToolError) as err:
        await svc.get_symbol("Amplifier_Operational:LM358X")
    assert err.value.code == "not_found"
    assert "Amplifier_Operational:LM358" in err.value.suggestion


async def test_read_only_derived_symbol_without_probe(blank_session: KicadSession) -> None:
    svc = await _ready(blank_session)
    blank_session.read_only = True  # a probe would be a (dropped) write
    try:
        d = await svc.get_symbol("MCU_ST_STM32F1:STM32F103C8Tx")
    finally:
        blank_session.read_only = False
    assert d.pins_known is False and d.pins == [] and d.to_dict()["pin_count"] is None


# -- Task 3: footprints lazily ------------------------------------------------------------


def _loaded(nickname: str):  # type: ignore[no-untyped-def]
    def run(h: KicadHandle) -> bool:
        from kipy.proto.common.types import LibraryLoadStatus, LibraryType

        statuses = h.kicad.get_library_statuses(LibraryType.LT_FOOTPRINT)
        return any(
            s.nickname == nickname and s.status == LibraryLoadStatus.LLS_LOADED for s in statuses
        )

    return run


async def test_footprint_lazy_load(ecc83_session: KicadSession) -> None:
    svc = library_for(ecc83_session)
    assert svc.footprints_available()
    assert not await ecc83_session.call(_loaded("Resistor_SMD"))
    fp = await svc.footprint("Resistor_SMD:R_0805_2012Metric")
    assert fp is not None
    assert (fp.pad_count, fp.smd_pads, fp.package) == (2, 2, "0805")
    assert await ecc83_session.call(_loaded("Resistor_SMD"))
    assert not await ecc83_session.call(_loaded("Capacitor_SMD"))
    assert svc.loaded_footprint_libraries() == ["Resistor_SMD"]
    assert await svc.footprint("Resistor_SMD:R_9999_Nope") is None
    assert await svc.footprint("NoSuchLib:X") is None


async def test_search_footprints_in_library(ecc83_session: KicadSession) -> None:
    svc = library_for(ecc83_session)
    rows = await svc.search_footprints("0805", library="Resistor_SMD")
    assert "Resistor_SMD:R_0805_2012Metric" in [r["lib_id"] for r in rows]
    assert all(r["lib_id"].startswith("Resistor_SMD:") for r in rows)
    with pytest.raises(StructuredToolError) as err:
        await svc.search_footprints("0805", library="Resistor_SMDX")
    assert err.value.code == "not_found"


async def test_search_without_library_suggests_libraries(ecc83_session: KicadSession) -> None:
    svc = library_for(ecc83_session)
    suggested = await svc.suggest_footprint_libraries("0805 resistor")
    assert "Resistor_SMD" in suggested
    rows = await svc.search_footprints("0805 resistor", limit=5)
    assert "Resistor_SMD:R_0805_2012Metric" in [r["lib_id"] for r in rows]
    loaded = svc.loaded_footprint_libraries()
    assert "Resistor_SMD" in loaded and len(loaded) <= 3
    assert not await ecc83_session.call(_loaded("Capacitor_SMD"))


async def test_no_board_footprints_unavailable(boardless_session: KicadSession) -> None:
    svc = library_for(boardless_session)
    assert not svc.footprints_available()
    for call in (
        svc.search_footprints("0805"),
        svc.footprint("Resistor_SMD:R_0805_2012Metric"),
        svc.footprint_libraries(),
    ):
        with pytest.raises(StructuredToolError) as err:
            await call
        assert err.value.code == "footprints_unavailable"
    # symbol search still works without a board
    svc2 = await _ready(boardless_session)
    assert await svc2.search_symbols("resistor")
