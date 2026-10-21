from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import KicadSession
from copperchat.repo_context import specs
from copperchat.tools import StructuredToolError, ToolContext
from tests.unit.repo_context.conftest import git, init_repo, make_project

TOOLS = {s.name: s for s in specs.TOOL_SPECS}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "projects" / "repo")


@pytest.fixture
def ctx(repo: Path, make_settings: Callable[..., Settings]) -> ToolContext:
    project = make_project(repo / "hw" / "main")
    return ToolContext(project=project, session=cast(KicadSession, None), settings=make_settings())


def write(repo: Path, rel: str, content: str | bytes) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


async def call(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    result = await TOOLS[name].handler(ctx, args)
    assert result.data is not None
    return result.data


def test_specs_shape() -> None:
    assert list(TOOLS) == ["repo_list", "repo_grep", "repo_read"]
    for spec in specs.TOOL_SPECS:
        assert spec.requires_kicad is False and spec.mutating is False
        assert spec.input_schema["type"] == "object"


# --- repo_list -----------------------------------------------------------------------------


async def test_list_pattern_scad(ctx: ToolContext, repo: Path) -> None:
    write(repo, "mech/case/enclosure.scad", "cube(10);\n")
    write(repo, "top.scad", "sphere(1);\n")
    write(repo, "README.md", "# hi\n")
    data = await call(ctx, "repo_list", pattern="**/*.scad")
    assert data["root"] == "repo"
    assert data["project_dir"] == "hw/main"
    assert [e["path"] for e in data["entries"]] == ["mech/case/enclosure.scad", "top.scad"]
    assert all(e["kind"] == "text" for e in data["entries"])
    assert data["entries"][1]["size"] == len("sphere(1);\n")
    assert "dirs" not in data
    assert data["total"] == 2 and data["truncated"] is False


async def test_list_pattern_relative_to_path(ctx: ToolContext, repo: Path) -> None:
    write(repo, "mech/a.scad", "x\n")
    write(repo, "mech/sub/b.scad", "x\n")
    write(repo, "other/c.scad", "x\n")
    data = await call(ctx, "repo_list", path="mech", pattern="*.scad")
    assert [e["path"] for e in data["entries"]] == ["mech/a.scad"]
    data = await call(ctx, "repo_list", path="mech", pattern="**/*.scad")
    assert [e["path"] for e in data["entries"]] == ["mech/a.scad", "mech/sub/b.scad"]


async def test_list_dirs_without_pattern(ctx: ToolContext, repo: Path) -> None:
    write(repo, "README.md", "# hi\n")
    write(repo, "mech/a.scad", "x\n")
    write(repo, "mech/sub/b.scad", "x\n")
    write(repo, ".env", "SECRET=1\n")
    data = await call(ctx, "repo_list")
    assert [e["path"] for e in data["entries"]] == ["README.md"]
    assert data["dirs"] == [{"path": "hw", "files": 1}, {"path": "mech", "files": 2}]
    data = await call(ctx, "repo_list", path="mech")
    assert [e["path"] for e in data["entries"]] == ["mech/a.scad"]
    assert data["dirs"] == [{"path": "mech/sub", "files": 1}]


async def test_list_kinds(ctx: ToolContext, repo: Path) -> None:
    write(repo, "img/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00")
    write(repo, "big.txt", "x" * (specs.FILE_MAX_BYTES + 1))
    write(repo, "notes.md", "hello\n")
    data = await call(ctx, "repo_list", pattern="**")
    kinds = {e["path"]: e["kind"] for e in data["entries"]}
    assert kinds == {
        "big.txt": "large",
        "hw/main/main.kicad_pro": "kicad",
        "img/logo.png": "binary",
        "notes.md": "text",
    }


async def test_list_kind_unknown_beyond_sniff_cap(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(specs, "LIST_SNIFF_MAX", 1)
    write(repo, "a.txt", "a\n")
    write(repo, "b.txt", "b\n")
    data = await call(ctx, "repo_list", pattern="*.txt")
    assert [e["kind"] for e in data["entries"]] == ["text", "unknown"]


async def test_list_max_entries_truncated(ctx: ToolContext, repo: Path) -> None:
    for i in range(5):
        write(repo, f"f{i}.txt", "x\n")
    data = await call(ctx, "repo_list", pattern="*.txt", max_entries=3)
    assert [e["path"] for e in data["entries"]] == ["f0.txt", "f1.txt", "f2.txt"]
    assert data["total"] == 5 and data["truncated"] is True


@pytest.mark.parametrize(
    "args",
    [
        {"max_entries": 0},
        {"max_entries": 501},
        {"max_entries": "10"},
        {"pattern": "/etc/*"},
        {"pattern": "../*"},
        {"path": "../"},
        {"path": ".git"},
        {"path": "missing"},
        {"pattern": "[z-a].txt"},
    ],
)
async def test_list_bad_input(ctx: ToolContext, args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError):
        await call(ctx, "repo_list", **args)


# --- repo_read -----------------------------------------------------------------------------


async def test_read_range(ctx: ToolContext, repo: Path) -> None:
    write(repo, "doc.txt", "".join(f"line {i}\n" for i in range(1, 11)))
    data = await call(ctx, "repo_read", path="doc.txt", offset=3, limit=2)
    assert data["root"] == "repo" and data["project_dir"] == "hw/main"
    assert data["path"] == "doc.txt"
    assert data["content"] == "line 3\nline 4\n"
    assert (data["start_line"], data["end_line"], data["total_lines"]) == (3, 4, 10)
    assert data["truncated"] is True and data["next_offset"] == 5


async def test_read_whole_file(ctx: ToolContext, repo: Path) -> None:
    write(repo, "doc.txt", "a\nb\n")
    data = await call(ctx, "repo_read", path="./doc.txt")
    assert data["content"] == "a\nb\n" and data["truncated"] is False
    assert "next_offset" not in data
    assert (data["start_line"], data["end_line"], data["total_lines"]) == (1, 2, 2)


async def test_read_default_limit(ctx: ToolContext, repo: Path) -> None:
    write(repo, "long.txt", "x\n" * 1500)
    data = await call(ctx, "repo_read", path="long.txt")
    assert data["end_line"] == specs.READ_MAX_LINES
    assert data["truncated"] is True and data["next_offset"] == specs.READ_MAX_LINES + 1


async def test_read_64k_cap(ctx: ToolContext, repo: Path) -> None:
    line = "y" * 99 + "\n"  # 100 bytes
    write(repo, "wide.txt", line * 900)  # 90 000 bytes
    data = await call(ctx, "repo_read", path="wide.txt")
    assert len(data["content"].encode()) <= specs.READ_MAX_BYTES
    assert data["content"].endswith("\n")
    assert data["end_line"] == specs.READ_MAX_BYTES // 100
    assert data["truncated"] is True and data["next_offset"] == data["end_line"] + 1


async def test_read_single_huge_line_cut(ctx: ToolContext, repo: Path) -> None:
    write(repo, "one.txt", "z" * (specs.READ_MAX_BYTES + 500) + "\nnext\n")
    data = await call(ctx, "repo_read", path="one.txt")
    assert len(data["content"].encode()) == specs.READ_MAX_BYTES
    assert data["end_line"] == 1 and data["truncated"] is True and data["next_offset"] == 2


async def test_read_offset_beyond_eof(ctx: ToolContext, repo: Path) -> None:
    write(repo, "doc.txt", "a\nb\n")
    with pytest.raises(StructuredToolError, match="beyond"):
        await call(ctx, "repo_read", path="doc.txt", offset=5)


async def test_read_empty_file(ctx: ToolContext, repo: Path) -> None:
    write(repo, "empty.txt", "")
    data = await call(ctx, "repo_read", path="empty.txt")
    assert data["content"] == "" and data["total_lines"] == 0 and data["truncated"] is False


async def test_read_decodes_with_replace(ctx: ToolContext, repo: Path) -> None:
    write(repo, "mixed.txt", b"ok\n" * 3000 + b"bad \xff byte\n")
    data = await call(ctx, "repo_read", path="mixed.txt", offset=3001)
    assert data["content"] == "bad � byte\n"


async def test_read_refuses_kicad_with_hint(ctx: ToolContext) -> None:
    with pytest.raises(StructuredToolError) as err:
        await call(ctx, "repo_read", path="hw/main/main.kicad_pro")
    assert "sch_" in err.value.suggestion


async def test_read_refuses_binary(ctx: ToolContext, repo: Path) -> None:
    write(repo, "blob.bin", b"\x00\x01\x02")
    with pytest.raises(StructuredToolError) as err:
        await call(ctx, "repo_read", path="blob.bin")
    assert "binary files cannot be read" in err.value.suggestion


async def test_read_refuses_over_1mib(ctx: ToolContext, repo: Path) -> None:
    write(repo, "big.txt", "x" * (specs.FILE_MAX_BYTES + 1))
    with pytest.raises(StructuredToolError, match="1 MiB"):
        await call(ctx, "repo_read", path="big.txt")


async def test_read_refuses_env(ctx: ToolContext, repo: Path) -> None:
    write(repo, ".env", "TOKEN=abc\n")
    with pytest.raises(StructuredToolError) as err:
        await call(ctx, "repo_read", path=".env")
    assert err.value.suggestion


async def test_read_refuses_symlink_to_env(ctx: ToolContext, repo: Path) -> None:
    write(repo, ".env", "TOKEN=abc\n")
    (repo / "harmless.txt").symlink_to(repo / ".env")
    with pytest.raises(StructuredToolError):
        await call(ctx, "repo_read", path="harmless.txt")


@pytest.mark.parametrize(
    "args",
    [
        {"path": "doc.txt", "offset": 0},
        {"path": "doc.txt", "limit": 0},
        {"path": "doc.txt", "limit": 1001},
        {"path": "doc.txt", "offset": True},
        {"path": "/etc/passwd"},
        {},
    ],
)
async def test_read_bad_input(ctx: ToolContext, repo: Path, args: dict[str, Any]) -> None:
    write(repo, "doc.txt", "a\n")
    with pytest.raises(StructuredToolError):
        await call(ctx, "repo_read", **args)


# --- repo_grep -----------------------------------------------------------------------------


async def test_grep_literal_case_insensitive(ctx: ToolContext, repo: Path) -> None:
    write(repo, "a.txt", "nothing\nUSB-C Connector here\n")
    write(repo, "b.md", "the usb-c port (a+b)\n")
    data = await call(ctx, "repo_grep", query="usb-c")
    assert data["root"] == "repo" and data["project_dir"] == "hw/main"
    assert data["matches"] == [
        {"path": "a.txt", "line": 2, "text": "USB-C Connector here"},
        {"path": "b.md", "line": 1, "text": "the usb-c port (a+b)"},
    ]
    assert data["stopped_by"] is None and data["truncated"] is False
    assert data["files_scanned"] == 2
    # Literal: regex metacharacters match themselves.
    data = await call(ctx, "repo_grep", query="(a+b)")
    assert [m["path"] for m in data["matches"]] == ["b.md"]
    data = await call(ctx, "repo_grep", query="usb-c", case_sensitive=True)
    assert [m["path"] for m in data["matches"]] == ["b.md"]


async def test_grep_regex(ctx: ToolContext, repo: Path) -> None:
    write(repo, "fw/main.c", "int x;\n#define LED_PIN 13\n#define BTN_PIN 2\n")
    data = await call(ctx, "repo_grep", query=r"^#define \w+_PIN \d+$", regex=True)
    assert [(m["path"], m["line"]) for m in data["matches"]] == [
        ("fw/main.c", 2),
        ("fw/main.c", 3),
    ]


async def test_grep_glob_filter(ctx: ToolContext, repo: Path) -> None:
    write(repo, "mech/case.scad", "pin header\n")
    write(repo, "docs/pins.md", "pin header\n")
    write(repo, "fw/src/pins.h", "pin header\n")
    data = await call(ctx, "repo_grep", query="pin", glob="*.scad")
    assert [m["path"] for m in data["matches"]] == ["mech/case.scad"]
    data = await call(ctx, "repo_grep", query="pin", glob="src/*.h", path="fw")
    assert [m["path"] for m in data["matches"]] == ["fw/src/pins.h"]
    data = await call(ctx, "repo_grep", query="pin", path="docs")
    assert [m["path"] for m in data["matches"]] == ["docs/pins.md"]


async def test_grep_skips_kicad_binary_blocked(ctx: ToolContext, repo: Path) -> None:
    write(repo, "hw/main/main.kicad_sch", "(kicad_sch needle)\n")
    write(repo, "blob.bin", b"needle\x00")
    write(repo, ".env", "needle\n")
    write(repo, "config/secrets.yaml", "needle\n")
    write(repo, "big.txt", "needle\n" + "x" * specs.FILE_MAX_BYTES)
    write(repo, "ok.txt", "needle\n")
    data = await call(ctx, "repo_grep", query="needle")
    assert [m["path"] for m in data["matches"]] == ["ok.txt"]


async def test_grep_max_results(ctx: ToolContext, repo: Path) -> None:
    write(repo, "many.txt", "hit\n" * 20)
    data = await call(ctx, "repo_grep", query="hit", max_results=5)
    assert len(data["matches"]) == 5
    assert data["stopped_by"] == "max_results" and data["truncated"] is True


async def test_grep_bytes_budget(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(specs, "GREP_BYTES", 100)
    write(repo, "a.txt", "hit\n" + "x" * 200 + "\nhit\n")
    data = await call(ctx, "repo_grep", query="hit")
    assert [m["line"] for m in data["matches"]] == [1]
    assert data["stopped_by"] == "bytes" and data["truncated"] is True


async def test_grep_text_cut_300(ctx: ToolContext, repo: Path) -> None:
    write(repo, "long.txt", "needle " + "w" * 1000 + "\n")
    data = await call(ctx, "repo_grep", query="needle")
    assert len(data["matches"][0]["text"]) == 300


async def test_grep_line_cut_2000_before_matching(ctx: ToolContext, repo: Path) -> None:
    write(repo, "long.txt", "w" * 2500 + "needle\n")
    data = await call(ctx, "repo_grep", query="needle")
    assert data["matches"] == []


async def test_grep_bad_regex_structured_error(ctx: ToolContext) -> None:
    with pytest.raises(StructuredToolError, match="regular expression"):
        await call(ctx, "repo_grep", query="(unclosed", regex=True)


@pytest.mark.parametrize(
    "args",
    [
        {"query": ""},
        {"query": "x" * 201},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": 101},
        {"query": "x", "regex": "yes"},
        {"query": "x", "glob": "../*"},
        {"query": "x", "path": ".git"},
    ],
)
async def test_grep_bad_input(ctx: ToolContext, args: dict[str, Any]) -> None:
    with pytest.raises(StructuredToolError):
        await call(ctx, "repo_grep", **args)


# Lines are cut to 2000 chars before matching, which drops the "b" of the 5001-char line
# (it then matches at once); the second line stays under the cut and backtracks forever.
EVIL = "a" * 5000 + "b\n" + "a" * 1999 + "b\n"


async def test_grep_redos_killed(ctx: ToolContext, repo: Path) -> None:
    assert specs.GREP_TIMEOUT_S >= 1.0
    write(repo, "evil.txt", EVIL)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.1)
            ticks += 1

    task = asyncio.create_task(ticker())
    start = time.monotonic()
    try:
        data = await call(ctx, "repo_grep", query="(a+)+$", regex=True)
    finally:
        task.cancel()
    elapsed = time.monotonic() - start
    assert elapsed < 8.0
    assert data["stopped_by"] == "time" and data["truncated"] is True
    assert [m["line"] for m in data["matches"]] == [1]  # streamed before the kill
    # The loop stayed responsive: roughly one tick per 100 ms while the worker ran.
    assert ticks >= int(specs.GREP_TIMEOUT_S * 10 * 0.7)


async def test_grep_cancel_kills_worker(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(repo, "evil.txt", EVIL)
    procs: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_exec(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(specs.asyncio, "create_subprocess_exec", spy)
    task = asyncio.create_task(call(ctx, "repo_grep", query="(a+)+$", regex=True))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs and procs[0].returncode is not None


async def test_grep_worker_crash_structured_error(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(repo, "a.txt", "x\n")
    monkeypatch.setattr(specs, "_WORKER_MODULE", "copperchat.repo_context.no_such_module")
    with pytest.raises(StructuredToolError, match="grep"):
        await call(ctx, "repo_grep", query="x")


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.scad", "a.scad", True),
        ("**/*.scad", "x/y/a.SCAD", True),
        ("*.scad", "x/a.scad", False),
        ("x/**/a.txt", "x/a.txt", True),
        ("x/**/a.txt", "x/y/z/a.txt", True),
        ("x/**", "x/y/z", True),
        ("x/**", "xy/z", False),
        ("**", "a/b/c", True),
        ("a?.txt", "ab.txt", True),
        ("a?.txt", "a/.txt", False),
        ("[ab].txt", "b.txt", True),
        ("[!ab].txt", "c.txt", True),
        ("[!ab].txt", "a.txt", False),
        ("[a-c]x", "Bx", True),
        ("[a-c]x", "dx", False),
        ("[]]x", "]x", True),
        ("[!]]x", "]x", False),
        ("[ab", "[ab", True),
        ("[ab", "a", False),
        ("a+(b).txt", "a+(b).txt", True),
        ("a***b", "axyb", True),
        ("x/a**b", "x/ab", True),
        ("x/a**b", "x/a/b", False),
        ("*", "a/b", False),
        ("**/", "a", False),
        ("a/**/b/**/c", "a/b/c", True),
        ("a/**/b/**/c", "a/x/b/y/z/c", True),
        ("a/**/b/**/c", "a/x/c", False),
        ("*.txt", ".hidden.txt", True),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert specs.glob_match(pattern, path) is expected


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("*?*?*?*?*?*?*?*%", "a" * 50),
        ("*a*a*a*a*a*a*a*b", "a" * 60),
        ("*a*a*a*b", "a" * 250),
        ("**/**/**/**/x", "/".join(["d"] * 300)),
        ("**/a*a*a*a*b/**/**/y", "/".join(["a" * 40] * 300)),
    ],
    ids=["star-qmark", "star-a-60", "star-a-250", "doublestar-deep", "mixed-deep"],
)
def test_glob_match_is_linear(pattern: str, path: str) -> None:
    start = time.perf_counter()
    assert specs.glob_match(pattern, path) is False
    assert time.perf_counter() - start < 0.05


async def test_list_many_wildcards_allowed(ctx: ToolContext, repo: Path) -> None:
    write(repo, "abcdefghij.txt", "x\n")
    data = await call(ctx, "repo_list", pattern="*a*b*c*d*e*f*g*h*i*j*")
    assert [e["path"] for e in data["entries"]] == ["abcdefghij.txt"]


async def test_grep_read_line_numbers_agree(ctx: ToolContext, repo: Path) -> None:
    write(repo, "ff.txt", b"one\n\x0c\ntwo\x0cthree\nneedle\n")
    write(repo, "crlf.txt", b"a\r\nb\r\nneedle\r\n")
    write(repo, "cr.txt", b"a\rb\nneedle\n")
    data = await call(ctx, "repo_grep", query="needle")
    found = {m["path"]: m["line"] for m in data["matches"]}
    assert found == {"ff.txt": 4, "crlf.txt": 3, "cr.txt": 2}
    for path, line in found.items():
        read = await call(ctx, "repo_read", path=path, offset=line, limit=1)
        assert read["content"].rstrip("\r\n") == "needle"
    assert (await call(ctx, "repo_read", path="ff.txt"))["total_lines"] == 4
    crlf = await call(ctx, "repo_read", path="crlf.txt")
    assert crlf["content"] == "a\r\nb\r\nneedle\r\n" and crlf["total_lines"] == 3


async def test_grep_glob_normalised(ctx: ToolContext, repo: Path) -> None:
    write(repo, "mech/case.scad", "pin\n")
    data = await call(ctx, "repo_grep", query="pin", glob="./*.scad")
    assert [m["path"] for m in data["matches"]] == ["mech/case.scad"]


async def test_grep_regex_overflow_structured_error(ctx: ToolContext) -> None:
    with pytest.raises(StructuredToolError, match="regular expression"):
        await call(ctx, "repo_grep", query="a{99999999999999999999}", regex=True)


async def test_symlinked_dir_as_path(ctx: ToolContext, repo: Path) -> None:
    write(repo, "real/a.scad", "pin\n")
    (repo / "alias").symlink_to("real")
    listed = await call(ctx, "repo_list", path="alias")
    assert [e["path"] for e in listed["entries"]] == ["real/a.scad"]
    assert listed["path"] == "real"
    found = await call(ctx, "repo_grep", query="pin", path="alias")
    assert [m["path"] for m in found["matches"]] == ["real/a.scad"]


async def test_read_bounded_when_file_grows(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(repo, "big.txt", "x" * (specs.FILE_MAX_BYTES + 10))
    real_fstat = specs.os.fstat

    class _Small:
        def __init__(self, st: Any) -> None:
            self._st = st

        def __getattr__(self, name: str) -> Any:
            return 10 if name == "st_size" else getattr(self._st, name)

    monkeypatch.setattr(specs.os, "fstat", lambda fd: _Small(real_fstat(fd)))
    with pytest.raises(StructuredToolError, match="larger than 1 MiB"):
        await call(ctx, "repo_read", path="big.txt")


async def test_read_single_huge_last_line_is_truncated(ctx: ToolContext, repo: Path) -> None:
    write(repo, "one.txt", "z" * (specs.READ_MAX_BYTES + 500))
    data = await call(ctx, "repo_read", path="one.txt")
    assert len(data["content"].encode()) == specs.READ_MAX_BYTES
    assert data["end_line"] == 1 and data["total_lines"] == 1
    assert data["truncated"] is True and data["line_truncated"] is True
    assert "next_offset" not in data


async def test_read_cut_line_flagged_with_next_offset(ctx: ToolContext, repo: Path) -> None:
    write(repo, "one.txt", "z" * (specs.READ_MAX_BYTES + 500) + "\nnext\n")
    data = await call(ctx, "repo_read", path="one.txt")
    assert data["line_truncated"] is True and data["next_offset"] == 2


async def test_read_uncut_has_no_line_truncated(ctx: ToolContext, repo: Path) -> None:
    write(repo, "doc.txt", "a\nb\n")
    data = await call(ctx, "repo_read", path="doc.txt")
    assert data["truncated"] is False and "line_truncated" not in data


def _corrupt_git(repo: Path) -> None:
    write(repo, "a.txt", "x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    (repo / ".git" / "index").write_bytes(b"DIRC" + b"\xff" * 64)


@pytest.mark.parametrize(("tool", "args"), [("repo_list", {}), ("repo_grep", {"query": "x"})])
async def test_git_listing_failure_is_structured(
    ctx: ToolContext, repo: Path, tool: str, args: dict[str, Any]
) -> None:
    _corrupt_git(repo)
    with pytest.raises(StructuredToolError, match="git listing failed") as info:
        await call(ctx, tool, **args)
    assert info.value.suggestion


def test_glob_collapses_consecutive_doublestars() -> None:
    assert specs.Glob("**/**/x")._segments == specs.Glob("**/x")._segments
    assert specs.Glob("a/**/**/**")._segments == specs.Glob("a/**")._segments
    for path in ("x", "a/x", "a/b/x", "a/b"):
        assert specs.glob_match("**/**/x", path) is specs.glob_match("**/x", path)
    for path in ("a", "a/b", "a/b/c"):
        assert specs.glob_match("a/**/**", path) is specs.glob_match("a/**", path)


async def test_grep_worker_gets_minimal_env(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(repo, "a.txt", "needle\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("COPPERCHAT_ADMIN_PASSWORD", "hunter2")
    envs: list[dict[str, str] | None] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if specs._WORKER_MODULE in args:
            envs.append(kwargs.get("env"))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(specs.asyncio, "create_subprocess_exec", spy)
    data = await call(ctx, "repo_grep", query="needle")
    assert [m["path"] for m in data["matches"]] == ["a.txt"]
    assert len(envs) == 1
    env = envs[0]
    assert env is not None
    assert "ANTHROPIC_API_KEY" not in env and "COPPERCHAT_ADMIN_PASSWORD" not in env
    assert "sk-secret" not in env.values()


async def test_tools_refuse_while_git_cannot_be_run(
    ctx: ToolContext, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.repo_context import paths

    async def no_git(_: Path) -> Path | None:
        raise paths._GitUnavailable("git timed out")

    write(repo, ".gitignore", "private.txt\n")
    write(repo, "private.txt", "IGNORED\n")
    monkeypatch.setattr(paths, "_git_toplevel", no_git)
    for name, args in (
        ("repo_list", {}),
        ("repo_grep", {"query": "IGNORED"}),
        ("repo_read", {"path": "private.txt"}),
    ):
        with pytest.raises(StructuredToolError, match="could not be inspected"):
            await TOOLS[name].handler(ctx, args)
