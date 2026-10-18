"""What a two-phase mutating tool plans in `prepare` and the engine applies (E-C4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from copperchat.kicad_tools.access import PinKey

if TYPE_CHECKING:
    from copperchat.kicad_tools.edit.engine import Applied


@dataclass(frozen=True)
class SheetRef:
    """One sheet instance. `path` = `/`-joined KIID path (E-C2 `sheet_path`)."""

    path: str
    page: str
    file: str
    name: str
    human_path: str = "/"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.human_path, "page": self.page, "file": self.file}


@dataclass
class Create:
    """kipy items with fresh UUIDs (the engine assigns one where missing)."""

    items: list[Any]
    sheet: SheetRef | None = None


@dataclass
class Update:
    """Full items as they should be; symbols are sent with their definition cleared
    (spike 2A Q2: a full-definition update of a multi-unit symbol hangs the server)."""

    items: list[Any]
    sheet: SheetRef | None = None


@dataclass
class Delete:
    uuids: list[str]
    sheet: SheetRef | None = None


@dataclass
class Place:
    """`place_symbol_from_library`; orientation in degrees (0/90/180/270)."""

    lib_id: str
    at_mm: tuple[float, float]
    orientation: int
    unit: int
    reference: str
    sheet: SheetRef | None = None


EditOp = Create | Update | Delete | Place


@dataclass
class StageInput:
    """What a stage's `build` sees: the read-back items of every earlier stage."""

    created: list[Any] = field(default_factory=list)  # incl. placed symbols
    placed: list[Any] = field(default_factory=list)
    modified: list[Any] = field(default_factory=list)


@dataclass
class Stage:
    """One KiCad commit."""

    label: str
    build: Callable[[StageInput], list[EditOp]]


def static_stage(label: str, *ops: EditOp) -> Stage:
    return Stage(label, lambda _inp: list(ops))


@dataclass
class NetIntent:
    merge: list[frozenset[PinKey]] = field(default_factory=list)  # each group ends on one net
    may_change: frozenset[PinKey] = frozenset()  # pins allowed to change net
    names: dict[PinKey, str] = field(default_factory=dict)  # expected net name per pin
    keep_partition: bool = False  # reroute/move: partition must be identical


@dataclass
class Logical:
    changes: int  # budget unit (E-C6)
    deleted_symbols: list[str] = field(default_factory=list)  # non-power symbols deleted
    placed_symbols: list[str] = field(default_factory=list)  # filled by apply_change_set


@dataclass
class ChangeSet:
    tool: str
    sheet: SheetRef
    stages: list[Stage]
    pre_images: dict[str, Any]  # uuid -> item copy (updated, deleted)
    intent: NetIntent
    logical: Logical
    summary: dict[str, Any] = field(default_factory=dict)  # echoed in the result
    notes: list[str] = field(default_factory=list)
    applied: Applied | None = None  # set by apply_change_set
