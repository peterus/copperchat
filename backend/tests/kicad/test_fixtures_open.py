import pytest

from copperchat.kicad_session import KicadSession
from tests.support.kicad_project import FIXTURES

pytestmark = pytest.mark.kicad


def test_fixture_files_present() -> None:
    ecc = FIXTURES / "ecc83"
    for name in (
        "ecc83-pp.kicad_pro",
        "ecc83-pp.kicad_sch",
        "ecc83-pp.kicad_pcb",
        "ecc83-pp.kicad_sym",
        "sym-lib-table",
        "fp-lib-table",
    ):
        assert (ecc / name).is_file(), name
    hier = FIXTURES / "complex_hierarchy"
    for name in (
        "complex_hierarchy.kicad_pro",
        "complex_hierarchy.kicad_sch",
        "ampli_ht.kicad_sch",
    ):
        assert (hier / name).is_file(), name
    assert not list(FIXTURES.rglob("*.kicad_prl")) and not list(FIXTURES.rglob("*.lck"))


async def test_ecc83_opens_both_documents(ecc83_session: KicadSession) -> None:
    n_fp = await ecc83_session.call(lambda h: len(h.board().get_footprints()))
    n_sym = await ecc83_session.call(lambda h: len(h.schematic().get_symbols()))
    assert n_fp > 0 and n_sym > 0


async def test_server_uses_seeded_library_tables(ecc83_session: KicadSession) -> None:
    config = ecc83_session.settings.data_dir / "kicad-config" / "10.99"
    assert (config / "sym-lib-table").is_file() and (config / "fp-lib-table").is_file()
