"""sch_set_title_block through the real tool adapter: the title block is written by KiCad
(file on disk, kicad-cli export and ERC still work), partial updates, all_sheets, journal."""

from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools import project
from copperchat.kicad_tools.edit.title_block import read_values
from tests.kicad.edit.test_shift_sheet import _ok
from tests.support.edit import RUN_ID, call_tool, edit_ctx
from tests.support.kicad_env import KICAD_CLI

pytestmark = pytest.mark.kicad

ROOT = "complex_hierarchy.kicad_sch"
CHILD = "ampli_ht.kicad_sch"


async def _values(s: KicadSession, sheet: str | None = None) -> dict[str, str]:
    from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api

    return await s.call(lambda h: read_values(sheet_api(h, resolve_sheet(h, sheet))))


def _node(s: KicadSession, name: str) -> str:
    """The `(title_block ...)` text of a saved sheet file (read only, to see what KiCad wrote)."""
    text = (Path(s.project.dir) / name).read_text()
    m = re.search(r"\(title_block\n.*?\n\t\)", text, re.S)
    assert m, f"no title_block in {name}"
    return m.group(0)


async def test_sets_fields_keeps_the_rest_and_writes_the_file(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    before = await _values(s)
    out = await _ok(ctx, "sch_set_title_block", title='Amp "v2" \\ (test)', rev="C",
                    company="OE5XRX", comments={"1": "Peter", "9": "last"})  # fmt: skip
    assert out["logical_changes"] == 1 and out["sheets_changed"] == 1
    [row] = out["title_blocks"]
    assert row["sheet"]["path"] == "/" and row["sheet"]["file"] == ROOT
    assert row["changed"] == ["title", "rev", "company", "comment1", "comment9"]
    assert row["before"]["title"] == before["title"] and "company" not in row["before"]
    assert row["after"] == {
        "title": 'Amp "v2" \\ (test)', "date": before["date"], "rev": "C",
        "company": "OE5XRX", "comment1": "Peter", "comment9": "last",
    }  # fmt: skip
    now = await _values(s)
    assert now["date"] == before["date"]  # untouched
    assert now["title"] == 'Amp "v2" \\ (test)' and now["comment9"] == "last"
    node = _node(s, ROOT)  # saved by the adapter, escaped by KiCad
    assert '(title "Amp \\"v2\\" \\\\ (test)")' in node
    assert '(rev "C")' in node and '(company "OE5XRX")' in node
    assert '(comment 1 "Peter")' in node and '(comment 9 "last")' in node
    assert _node(s, CHILD).count("(title ") == 1  # the child's own block is a different one


async def test_empty_string_clears_and_a_missing_block_is_created(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    cleared = await _ok(ctx, "sch_set_title_block", title="", date="", rev="", company="")
    assert cleared["title_blocks"][0]["after"] == {}
    assert "(title_block" not in (Path(s.project.dir) / ROOT).read_text()  # KiCad drops it
    await _ok(ctx, "sch_set_title_block", title="Fresh")
    text = (Path(s.project.dir) / ROOT).read_text()
    assert text.index("(paper") < text.index("(title_block") < text.index("(lib_symbols")
    assert '(title "Fresh")' in _node(s, ROOT)


async def test_date_today(hier_session: KicadSession, edit_db: Database) -> None:
    out = await _ok(edit_ctx(hier_session, edit_db), "sch_set_title_block", date="today")
    iso = date.today().isoformat()
    assert out["title_blocks"][0]["after"]["date"] == iso
    assert (await _values(hier_session))["date"] == iso


async def test_a_sheet_file_is_shared_by_its_instances(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    out = await _ok(ctx, "sch_set_title_block", sheet="/ampli_ht_vertical/", rev="Z")
    assert out["title_blocks"][0]["sheet"]["file"] == CHILD
    assert any("used by 2 sheet instances" in n for n in out["notes"])
    assert (await _values(s, "/ampli_ht_horizontal/"))["rev"] == "Z"
    assert (await _values(s))["rev"] != "Z"  # the root has its own
    assert '(rev "Z")' in _node(s, CHILD)


async def test_all_sheets_writes_each_file_once(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    out = await _ok(ctx, "sch_set_title_block", all_sheets=True, company="ACME", date="2026-11-04")
    assert [r["sheet"]["file"] for r in out["title_blocks"]] == [ROOT, CHILD]  # not 3 rows
    assert out["sheets_changed"] == 2 and out["logical_changes"] == 1
    for sheet in (None, "/ampli_ht_vertical/", "/ampli_ht_horizontal/"):
        v = await _values(s, sheet)
        assert (v["company"], v["date"]) == ("ACME", "2026-11-04")
    for name in (ROOT, CHILD):
        assert '(company "ACME")' in _node(s, name)
    # the child keeps its own title, only the given fields changed
    assert (await _values(s, "/ampli_ht_vertical/"))["rev"] == "1"


async def test_nothing_to_change_writes_nothing(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    current = await _values(s)
    out = await _ok(ctx, "sch_set_title_block", title=current["title"], company="")
    assert out["sheets_changed"] == 0 and out["logical_changes"] == 0
    assert any("nothing to change" in n for n in out["notes"])
    assert await ctx.editing.changes.for_run(s.project.id, RUN_ID) == []  # type: ignore[union-attr]


async def test_refusals_change_nothing(hier_session: KicadSession, edit_db: Database) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    before = await _values(s)
    for args in (
        {"title": "a\nb"},
        {"comments": {"10": "x"}},
        {"title": "x" * 300},
        {"sheet": "/nope/", "title": "x"},
        {"all_sheets": True, "sheet": "/", "title": "x"},
        {},
    ):
        out, err = await call_tool(ctx, "sch_set_title_block", **args)
        assert err, args
        want = "not_found" if args.get("sheet") == "/nope/" else "invalid_argument"
        assert out["error"]["code"] == want, out
    assert await _values(s) == before


async def test_journal_row_and_budget(hier_session: KicadSession, edit_db: Database) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    out = await _ok(ctx, "sch_set_title_block", all_sheets=True, rev="Q")
    rows = await ctx.editing.changes.for_run(s.project.id, RUN_ID)  # type: ignore[union-attr]
    assert {(r.tool, r.op, r.kind, r.uuid) for r in rows} == {
        ("sch_set_title_block", "modify", "title_block", ROOT),
        ("sch_set_title_block", "modify", "title_block", CHILD),
    }
    assert all(r.after and r.after["rev"] == "Q" for r in rows)
    assert out["budget"] is not None and out["budget"]["used"] == 1


async def test_project_info_shows_the_title_blocks(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    await _ok(edit_ctx(s, edit_db), "sch_set_title_block", title="Seen", comments={"2": "two"})
    info = await project.project_info(edit_ctx(s), {})
    assert info.data is not None
    root = info.data["sheets"][0]["title_block"]
    assert root["title"] == "Seen" and root["comment2"] == "two"
    assert all(v for v in root.values())  # only the non-empty fields


async def test_kicad_cli_still_loads_and_shows_the_values(
    hier_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = hier_session
    await _ok(edit_ctx(s, edit_db), "sch_set_title_block", title="Exported Title XYZ",
              company="Quotes \"Q\" GmbH", comments={"3": "back\\slash"})  # fmt: skip
    sch = str(Path(s.project.dir) / ROOT)
    out = tmp_path / "svg"
    run = subprocess.run([KICAD_CLI, "sch", "export", "svg", "-o", str(out), sch],
                         capture_output=True, text=True, timeout=120)  # fmt: skip
    assert run.returncode == 0, run.stderr
    svg = (out / ROOT.replace(".kicad_sch", ".svg")).read_text()
    assert "Exported Title XYZ" in svg and "OE5XRX" not in svg
    assert "Quotes" in svg and "back" in svg
    erc = subprocess.run([KICAD_CLI, "sch", "erc", "-o", str(tmp_path / "erc.rpt"), sch],
                         capture_output=True, text=True, timeout=120)  # fmt: skip
    assert erc.returncode in (0, 5), erc.stderr  # 5: violations found, the file loaded


async def test_a_failing_write_restores_the_old_values(
    hier_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second sheet of an all_sheets call fails to take its values: both are undone."""
    from copperchat.kicad_tools.edit import specs_title_block as mod

    s = hier_session
    ctx = edit_ctx(s, edit_db)
    root0, child0 = await _values(s), await _values(s, "/ampli_ht_vertical/")
    real = mod.write_values
    calls: list[str] = []

    def flaky(sch: Any, values: dict[str, str]) -> None:
        calls.append(values["company"])
        if values["company"] == "BOOM" and len(calls) == 2:
            raise RuntimeError("KiCad said no")
        real(sch, values)

    monkeypatch.setattr(mod, "write_values", flaky)
    _out, err = await call_tool(ctx, "sch_set_title_block", all_sheets=True, company="BOOM")
    assert err
    monkeypatch.setattr(mod, "write_values", real)
    assert await _values(s) == root0 and await _values(s, "/ampli_ht_vertical/") == child0
    assert await ctx.editing.changes.for_run(s.project.id, RUN_ID) == []  # type: ignore[union-attr]
