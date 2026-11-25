"""THE one module of `run_cpu` job functions (B-C4, P3a). `run_cpu` accepts only module-level
functions of `copperchat.board.*` whose arguments pickle (frozen dataclasses); every board
computation that runs in the CPU worker is defined here so the worker imports one module.
Later phases append their jobs below (P3a `metrics_job`, P4 `place_block_job` /
`legalize_job`, P7 `fanout_candidates_job` / `stitch_candidates_job`, P5b/P6b as needed).

Also the in-process helper `strip_fills` (not a job: it is cheap and runs before a model is
sent to the worker, so filled-zone polygons are not pickled for computations that ignore
them)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import classify
from copperchat.board.metrics import MetricsReport, compute_metrics
from copperchat.board.model import BoardModel
from copperchat.board.presets import FabPreset

if TYPE_CHECKING:
    from copperchat.kicad_tools.board.api import DrcMarker, DrcResult

__all__ = ["MAX_ENTRIES", "SCOPE_KEYS", "Scope", "analyse_job", "metrics_job", "strip_fills"]

Scope = Literal["parts", "nets", "blocks", "all"]
# Every list / mapping of the `pcb_analyze` result keeps at most this many entries.
MAX_ENTRIES = 500
_ALL_KEYS = (
    "parts",
    "nets",
    "decoupling",
    "switchers",
    "crystals",
    "pairs",
    "blocks",
    "critical_nets",
    "overrides_applied",
    "board_md_errors",
)
# The result keys of each `pcb_analyze` scope; `board_md_errors` is in every scope.
SCOPE_KEYS: Mapping[str, tuple[str, ...]] = {
    "parts": (
        "parts",
        "decoupling",
        "switchers",
        "crystals",
        "overrides_applied",
        "board_md_errors",
    ),
    "nets": ("nets", "pairs", "critical_nets", "board_md_errors"),
    "blocks": ("blocks", "board_md_errors"),
    "all": _ALL_KEYS,
}


def _plain(value: Any) -> Any:
    """A classification record as JSON-ready data (tuples → lists)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


def _capped(data: dict[str, Any], key: str, value: Mapping[str, str] | Sequence[Any]) -> None:
    """`data[key]` = the first MAX_ENTRIES entries of `value` (order kept), and
    `data[f"{key}_omitted"]` = how many were dropped."""
    if isinstance(value, Mapping):
        items = list(value.items())
        data[key] = dict(items[:MAX_ENTRIES])
        n = len(items)
    else:
        data[key] = [_plain(v) for v in value[:MAX_ENTRIES]]
        n = len(value)
    data[f"{key}_omitted"] = max(0, n - MAX_ENTRIES)


def analyse_job(model: BoardModel, doc: BoardDoc, scope: Scope = "all") -> dict[str, Any]:
    """The `pcb_analyze` result (spec §7.1, B-C6): `classify(model, doc)` as JSON-ready data,
    restricted to the keys of `scope` (`SCOPE_KEYS`). Each key K holds a list (records as
    dicts, tuples as lists) or a mapping (`parts`: ref → class, `nets`: net → kind) of at
    most MAX_ENTRIES entries in classification order, plus `K_omitted` (entries dropped)."""
    c = classify(model, doc)
    full: dict[str, Mapping[str, str] | Sequence[Any]] = {
        "parts": c.part_class,
        "nets": c.net_kind,
        "decoupling": c.decoupling,
        "switchers": c.switchers,
        "crystals": c.crystals,
        "pairs": c.pairs,
        "blocks": c.blocks,
        "critical_nets": c.critical_nets,
        "overrides_applied": c.overrides_applied,
        "board_md_errors": doc.errors,
    }
    data: dict[str, Any] = {}
    for key in SCOPE_KEYS[scope]:
        _capped(data, key, full[key])
    return data


def strip_fills(model: BoardModel) -> BoardModel:
    """`model` without filled-zone geometry: every zone with `filled == ()` and
    `filled_holes == ()`; everything else (incl. `filled_area_mm2`) kept. In-process."""
    zones = tuple(dataclasses.replace(z, filled=(), filled_holes=()) for z in model.zones)
    return dataclasses.replace(model, zones=zones)


# ---- metrics (P3a, B-C7) -------------------------------------------------------------------


def metrics_job(
    model: BoardModel,
    doc: BoardDoc,
    drc: DrcResult | None,
    preset: FabPreset | None,
    only: frozenset[str] | None,
    parity: tuple[DrcMarker, ...] | None = None,
) -> MetricsReport:
    """Metrics M1-M14 (B-C7) of `model`: classifies it here, in the worker, and passes
    `parity` (full check only, P3b) through. Every metrics computation runs as
    `run_cpu(metrics_job, ...)`; callers strip fills (`strip_fills`) when M11 is not
    requested."""
    return compute_metrics(
        model, classify(model, doc), doc, drc=drc, parity=parity, preset=preset, only=only
    )
