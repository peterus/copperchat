from __future__ import annotations

import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.support.sharding import parse_shard, shard_split

BACKEND = Path(__file__).resolve().parents[2]
CI_YML = BACKEND.parent / ".github" / "workflows" / "ci.yml"
SHARD_MARKERS = "kicad and not kicad_e2e"  # the kicad shards; kicad_e2e has its own CI job


def _ci_shards() -> int:
    """N of the kicad job's `shard: [1, ..., N]` matrix (with `--shard ${{ matrix.shard }}/N`)."""
    text = CI_YML.read_text()
    listed = re.findall(r"^\s*shard: \[([\d, ]+)\]\s*$", text, re.M)
    assert len(listed) == 1, "expected one `shard: [...]` matrix in ci.yml"
    shards = [int(s) for s in listed[0].split(",")]
    assert shards == list(range(1, len(shards) + 1))
    assert f"--shard ${{{{ matrix.shard }}}}/{len(shards)}" in text
    assert f'-m "{SHARD_MARKERS}" --shard ${{{{ matrix.shard }}}}/{len(shards)}' in text, (
        "kicad shards must deselect the e2e flow"
    )
    assert f"${{{{ matrix.shard }}}}/{len(shards)})" in text, "kicad job name: wrong /N"
    return len(shards)


@pytest.mark.parametrize("total", [1, 2, 3, 8, 50])
@pytest.mark.parametrize("count", [0, 1, 7, 385])
def test_shards_are_disjoint_and_cover_everything(total: int, count: int) -> None:
    items = list(range(count))
    seen: list[int] = []
    for index in range(1, total + 1):
        kept, dropped = shard_split(items, index, total)
        assert sorted(kept + dropped) == items
        seen += kept
    assert sorted(seen) == items
    sizes = [len(shard_split(items, i, total)[0]) for i in range(1, total + 1)]
    assert max(sizes) - min(sizes) <= 1


@pytest.mark.parametrize("value", ["0/4", "5/4", "1", "a/b", "1/0", "1/2/3", ""])
def test_bad_shard_values_are_usage_errors(value: str) -> None:
    with pytest.raises(pytest.UsageError):
        parse_shard(value)


def test_shard_membership_ignores_collection_order() -> None:
    items = [f"t{i:03}" for i in range(50)]
    shuffled = items[::-1][7:] + items[::-1][:7]
    for index in (1, 3):
        kept, _ = shard_split(items, index, 3)
        kept_shuffled, _ = shard_split(shuffled, index, 3)
        assert sorted(kept_shuffled) == sorted(kept)
        assert kept_shuffled == [t for t in shuffled if t in kept]  # run order is kept


def test_parse_shard() -> None:
    assert parse_shard("3/8") == (3, 8)


def test_shard_option_deselects_after_marker_selection(pytester: pytest.Pytester) -> None:
    pytester.makeini(
        "[pytest]\nasyncio_default_fixture_loop_scope = function\nmarkers =\n    slow: slow\n"
    )
    pytester.makepyfile(
        "import pytest\n"
        + "".join(f"@pytest.mark.slow\ndef test_s{i}(): pass\n" for i in range(5))
        + "def test_fast(): pass\n"
    )
    pytester.makeconftest('pytest_plugins = ["tests.support.sharding"]\n')
    pytester.syspathinsert(BACKEND)
    passed: list[str] = []
    for index, count in ((1, 3), (2, 2)):
        result = pytester.runpytest("-v", "-m", "slow", "--shard", f"{index}/2")
        result.stdout.fnmatch_lines([f"shard {index}/2: {count} of 5 selected tests"])
        result.assert_outcomes(passed=count, deselected=6 - count)
        passed += [line.split()[0] for line in result.outlines if line.endswith("%]")]
    assert sorted(passed) == sorted(
        f"test_shard_option_deselects_after_marker_selection.py::test_s{i}" for i in range(5)
    )


def _collect(*args: str, empty_ok: bool = False) -> list[str]:
    # Collection only: never demand a real api-server here, even if the caller's env does.
    env = {k: v for k, v in os.environ.items() if k != "COPPERCHAT_REQUIRE_KICAD"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    ok = (0, 5) if empty_ok else (0,)  # 5: no test collected
    assert proc.returncode in ok, (
        f"{args}: exit {proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    return [line for line in proc.stdout.splitlines() if "::" in line]


def test_ci_shards_cover_the_whole_kicad_selection() -> None:
    """The CI matrix runs `-m "kicad and not kicad_e2e" --shard I/N`; together the shards
    must be the full run of that selection."""
    total = _ci_shards()
    runs = [("-m", SHARD_MARKERS, "tests")] + [
        ("-m", SHARD_MARKERS, "--shard", f"{index}/{total}", "tests")
        for index in range(1, total + 1)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        full, *shards = pool.map(lambda args: _collect(*args), runs)
    assert full, "no kicad tests collected"
    union = [nodeid for shard in shards for nodeid in shard]
    assert sorted(union) == sorted(full)
    assert len(union) == len(set(union))


def test_e2e_not_in_shards(pytester: pytest.Pytester) -> None:
    """The shard expression never collects a `kicad_e2e` test; `-m kicad_e2e` only that one."""
    pytester.makeini(
        "[pytest]\nasyncio_default_fixture_loop_scope = function\nmarkers =\n"
        "    kicad: kicad\n    kicad_e2e: e2e\n"
    )
    pytester.makepyfile(
        "import pytest\n"
        "@pytest.mark.kicad\n@pytest.mark.kicad_e2e\ndef test_flow(): pass\n"
        "@pytest.mark.kicad\ndef test_tool(): pass\n"
    )
    pytester.makeconftest('pytest_plugins = ["tests.support.sharding"]\n')
    pytester.syspathinsert(BACKEND)
    shard_ran: list[str] = []
    for index in (1, 2):
        result = pytester.runpytest("-v", "-m", SHARD_MARKERS, "--shard", f"{index}/2")
        shard_ran += [line.split()[0] for line in result.outlines if line.endswith("%]")]
    assert [n.split("::")[1] for n in shard_ran] == ["test_tool"]
    result = pytester.runpytest("-v", "-m", "kicad_e2e")
    result.assert_outcomes(passed=1, deselected=1)
    result.stdout.fnmatch_lines(["*::test_flow PASSED*"])


def test_e2e_tests_also_marked_kicad() -> None:
    """Every e2e test carries `kicad` too, so the kicad skip/require logic applies to it."""
    assert _collect("-m", "kicad_e2e", "tests"), "no kicad_e2e tests collected"
    assert _collect("-m", "kicad_e2e and not kicad", "tests", empty_ok=True) == []


def _ci_job(name: str) -> str:
    """The text of one top-level job of ci.yml (up to the next job)."""
    text = CI_YML.read_text()
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text, re.M | re.S)
    assert m, f"no job {name!r} in ci.yml"
    return m.group(1)


def test_ci_e2e_job() -> None:
    job = _ci_job("kicad-e2e")
    assert re.search(r"^    needs: kicad-commits$", job, re.M)
    assert re.search(r"^    name: kicad-e2e$", job, re.M)
    assert re.search(
        r"^    container:\n      image: \$\{\{ needs\.kicad-commits\.outputs\.base \}\}$", job, re.M
    ), "the e2e job runs on the pinned base image only"
    assert "matrix" not in job
    assert 'COPPERCHAT_REQUIRE_KICAD: "1"' in job
    assert re.search(r"timeout-minutes: 30$", job, re.M)
    assert (
        'echo "COPPERCHAT_FREEROUTING_HOME=$GITHUB_WORKSPACE/.cache/freerouting" >> "$GITHUB_ENV"'
        in job
    )
    assert re.search(r"path: \.cache/freerouting(/\.dl)?$", job, re.M)
    assert "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0" in job
    assert 'bash scripts/fetch-freerouting.sh "$COPPERCHAT_FREEROUTING_HOME"' in job
    assert re.search(r"uv run pytest -m kicad_e2e\b", job)
    assert "${{ github.workspace }}" not in job
