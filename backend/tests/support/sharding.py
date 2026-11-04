"""`--shard I/N`: run every N-th selected test, so CI can split a slow run into parallel jobs.

Items are dealt round-robin in node-id order, after `-m`/`-k` deselection: the N shards
are disjoint and together hold exactly the full selection, whatever tests are added, and
each test file is spread across all shards (tests of one file cost about the same).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

_KEY = pytest.StashKey[tuple[int, int, int]]()  # (index, total, selected before sharding)


def parse_shard(value: str) -> tuple[int, int]:
    """`"2/8"` → `(2, 8)`; the index is 1-based."""
    try:
        index_s, total_s = value.split("/")
        index, total = int(index_s), int(total_s)
    except ValueError:
        raise pytest.UsageError(f"--shard expects I/N, got {value!r}") from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {value}: need 1 <= I <= N")
    return index, total


def shard_split[T](
    items: list[T], index: int, total: int, key: Callable[[T], str] = str
) -> tuple[list[T], list[T]]:
    """Split into (kept, deselected) for shard `index` of `total`, both in the given order.

    Membership goes by each item's rank in `key` order (the node id), not by its position,
    so every shard process agrees on the split even if collection order differed.
    """
    order = sorted(range(len(items)), key=lambda i: (key(items[i]), i))
    rank = {i: r for r, i in enumerate(order)}
    kept = [item for i, item in enumerate(items) if rank[i] % total == index - 1]
    dropped = [item for i, item in enumerate(items) if rank[i] % total != index - 1]
    return kept, dropped


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="run only shard I of N (round-robin over the selected tests)",
    )


@pytest.hookimpl(trylast=True)  # after -m/-k deselection and the conftest's skip marking
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    value = config.getoption("--shard")
    if value is None:
        return
    index, total = parse_shard(value)
    kept, dropped = shard_split(items, index, total, key=lambda item: item.nodeid)
    config.stash[_KEY] = (index, total, len(items))
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


def pytest_report_collectionfinish(config: pytest.Config, items: list[pytest.Item]) -> str | None:
    info = config.stash.get(_KEY, None)
    if info is None:
        return None
    index, total, selected = info
    return f"shard {index}/{total}: {len(items)} of {selected} selected tests"
