from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.sharding import parse_shard, shard_split

BACKEND = Path(__file__).resolve().parents[2]
CI_YML = BACKEND.parent / ".github" / "workflows" / "ci.yml"


def _ci_shards() -> int:
    """N of the kicad job's `shard: [1, ..., N]` matrix (with `--shard ${{ matrix.shard }}/N`)."""
    text = CI_YML.read_text()
    listed = re.findall(r"^\s*shard: \[([\d, ]+)\]\s*$", text, re.M)
    assert len(listed) == 1, "expected one `shard: [...]` matrix in ci.yml"
    shards = [int(s) for s in listed[0].split(",")]
    assert shards == list(range(1, len(shards) + 1))
    assert f"--shard ${{{{ matrix.shard }}}}/{len(shards)}" in text
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


def _collect(*args: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    ).stdout
    return [line for line in out.splitlines() if "::" in line]


def test_ci_shards_cover_the_whole_kicad_selection() -> None:
    """The CI matrix runs `-m kicad --shard I/N`; together the shards must be the full run."""
    total = _ci_shards()
    full = _collect("-m", "kicad", "tests")
    assert full, "no kicad tests collected"
    union: list[str] = []
    for index in range(1, total + 1):
        union += _collect("-m", "kicad", "--shard", f"{index}/{total}", "tests")
    assert sorted(union) == sorted(full)
    assert len(union) == len(set(union))
