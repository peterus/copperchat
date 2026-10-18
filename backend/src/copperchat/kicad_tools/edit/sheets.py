"""Hierarchical sheets (E7): file/name validation and the planners behind `sch_add_sheet`
and `sch_add_sheet_pin`. Writes go through the E2 engine only."""

from __future__ import annotations

import re
import uuid as uuidlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Logical,
    NetIntent,
    SheetRef,
    Stage,
    StageInput,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.geometry import NM, Box, Point, mm_point, snap, snap_point
from copperchat.kicad_tools.edit.scene import (
    Scene,
    read_scene,
    resolve_sheet,
    sheet_api,
    sheet_refs,
)

SUFFIX = ".kicad_sch"
MAX_FILE_LEN = 100  # whole relative name
MAX_NAME_LEN = 64
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DEFAULT_SIZE = (50.8, 38.1)
MIN_SIZE = 12.7
MAX_SIZE = 500.0
PIN_PITCH = 2.54


def _bad(
    what: str, why: str, suggestion: str, code: str = "invalid_argument"
) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code=code)


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# -- validation (spec §10) --------------------------------------------------------------


def validate_sheet_file(project: Any, file: str, *, existing: Iterable[str]) -> str:
    """The sheet file as a project-relative posix path.

    Accepts `name.kicad_sch` or `dir/name.kicad_sch` (one optional directory level, plain
    characters only). A file that already exists on disk is accepted only when it is part
    of the hierarchy (`existing`: re-use as a second instance); the root document never.
    Nothing is created here."""
    if not isinstance(file, str) or not file or len(file) > MAX_FILE_LEN:
        raise _bad(
            "Invalid sheet file name",
            f"file was {_short(file)}",
            f"Pass a name like 'power{SUFFIX}' or 'sub/power{SUFFIX}' (at most "
            f"{MAX_FILE_LEN} characters).",
        )
    parts = file.split("/")
    ok = (
        len(parts) <= 2
        and file.endswith(SUFFIX)
        and all(_SEGMENT.fullmatch(p) for p in parts)
        and ".." not in file
        and len(parts[-1]) > len(SUFFIX)
    )
    if not ok:
        raise _bad(
            "Invalid sheet file name",
            f"{_short(file)} is not a plain relative '*{SUFFIX}' name with at most one "
            "directory level (letters, digits, '.', '_', '-'; no '..', no absolute path)",
            f"Use a name like 'power{SUFFIX}' or 'sub/power{SUFFIX}'.",
        )
    root = getattr(project, "sch_file", None)
    if root is not None and file.lower() == Path(root).name.lower():
        raise _bad(
            "Invalid sheet file name",
            f"{file} is the root schematic; a sheet that contains itself is recursive",
            "Choose a new file name.",
        )
    base = Path(project.dir)
    if len(parts) == 2:
        sub = base / parts[0]
        if sub.exists() and not sub.is_dir():
            raise _bad(
                "Invalid sheet file name",
                f"{parts[0]!r} exists in the project and is not a directory",
                "Choose another directory name or no directory.",
            )
        if sub.is_symlink():
            raise _bad(
                "Invalid sheet file name",
                f"directory {parts[0]!r} is a symbolic link",
                "Use a plain directory inside the project or no directory.",
            )
    target = base / file
    directory = target.parent
    on_disk = {p.name.lower(): p for p in directory.iterdir()} if directory.is_dir() else {}
    clash = on_disk.get(parts[-1].lower())
    if clash is not None:
        in_hierarchy = file in set(existing) and clash.name == parts[-1]
        if clash.is_symlink():
            raise _bad(
                "Invalid sheet file name",
                f"{file} is a symbolic link",
                "Choose a new file name.",
            )
        if not in_hierarchy:
            raise _bad(
                "Sheet file already exists",
                f"{clash.name} exists in the project and is not a sheet of this hierarchy; "
                "it would not be overwritten",
                "Choose a new file name, or reuse a file that is already a sheet.",
            )
    return file


def validate_sheet_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or len(name) > MAX_NAME_LEN
        or "/" in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise _bad(
            "Invalid sheet name",
            f"name was {_short(name)}",
            f"Use 1 to {MAX_NAME_LEN} printable characters without '/' and without "
            "leading or trailing spaces.",
        )
    return name


def validate_pin_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or len(name) > MAX_NAME_LEN
        or any(ord(c) < 32 or ord(c) == 127 or c in '{}"' for c in name)
    ):
        raise _bad(
            "Invalid sheet pin name",
            f"name was {_short(name)}",
            f"Use 1 to {MAX_NAME_LEN} printable characters without braces or quotes and "
            "without leading or trailing spaces.",
        )
    return name


# -- kipy builders ------------------------------------------------------------------------


def _v2(p: Point) -> Any:
    from kipy.geometry import Vector2

    return Vector2.from_xy(round(p[0] * NM), round(p[1] * NM))


def build_sheet_symbol(name: str, file: str, at: Point, size: tuple[float, float]) -> Any:
    from kipy.schematic_types import SheetSymbol

    s = SheetSymbol()
    s.name_field.name = "Sheetname"
    s.name_field.text.value = name
    s.name_field.text.position = _v2((at[0], at[1] - 1.27))
    s.filename_field.name = "Sheetfile"
    s.filename_field.text.value = file
    s.filename_field.text.position = _v2((at[0], at[1] + size[1] + 1.27))
    s.position = _v2(at)
    s.size = _v2(size)
    s.proto.id.value = str(uuidlib.uuid4())
    return s


def _shape(direction: str) -> Any:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelShape as S

    return {
        "input": S.SLSH_INPUT,
        "output": S.SLSH_OUTPUT,
        "bidirectional": S.SLSH_BIDI,
        "tri_state": S.SLSH_TRISTATE,
        "passive": S.SLSH_PASSIVE,
    }[direction]


def build_sheet_pin(name: str, at: Point, side: str, direction: str) -> Any:
    from kipy.proto.schematic.schematic_types_pb2 import SheetSide
    from kipy.schematic_types import SheetPin

    p = SheetPin()
    p.text.value = name
    p.position = _v2(at)
    p.text.position = _v2(at)
    p.side = SheetSide.SHS_LEFT if side == "left" else SheetSide.SHS_RIGHT
    p.shape = _shape(direction)
    return p


def build_hier_label(name: str, at: Point, direction: str) -> Any:
    from kipy.schematic_types import HierarchicalLabel

    lb = HierarchicalLabel()
    lb.text.value = name
    lb.position = _v2(at)
    lb.text.position = _v2(at)
    lb.shape = _shape(direction)
    lb.proto.id.value = str(uuidlib.uuid4())
    return lb


# -- scene helpers ------------------------------------------------------------------------


def _blockers(scene: Scene) -> list[Box]:
    """Everything a new sheet / label must not sit on."""
    out = scene.obstacles()
    out += [Box.around([w.start, w.end]) for w in scene.wires.values()]
    out += [Box.around([p, p]) for p in scene.junctions.values()]
    out += [Box.around([p, p]) for p in scene.no_connects.values()]
    out += [
        Box.around([lb.at, lb.at]).grow(max(2.54, 1.3 * len(lb.text)))
        for lb in scene.labels.values()
    ]
    return out


def hierarchy_files(h: KicadHandle) -> tuple[set[str], list[SheetRef]]:
    """(files used by the hierarchy, all sheet instances)."""
    refs = sheet_refs(h)
    return {r.file for r in refs if r.file}, refs


# -- sch_add_sheet ------------------------------------------------------------------------


def _pair(raw: Any, what: str, default: tuple[float, float] | None) -> tuple[float, float]:
    if raw is None and default is not None:
        return default
    ok = (
        isinstance(raw, (list, tuple))
        and len(raw) == 2
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in raw)
        and all(abs(c) < 10_000 and c == c for c in raw)
    )
    if not ok:
        raise _bad(
            f"Invalid {what}", f"{what} was {_short(raw)}", f"Pass {what} as [x, y] numbers in mm."
        )
    return (float(raw[0]), float(raw[1]))


def plan_add_sheet(project: Any, args: dict[str, Any]) -> Any:
    name = validate_sheet_name(args.get("name"))  # type: ignore[arg-type]
    raw_at = _pair(args.get("at"), "at", None)
    raw_size = _pair(args.get("size"), "size", DEFAULT_SIZE)
    if min(raw_size) <= 0 or min(raw_at) < 0:
        raise _bad(
            "Invalid sheet geometry",
            f"at {raw_at} / size {raw_size}: the size must be positive and the corner on the page",
            "Pass at as non-negative [x, y] and size as positive [w, h] in mm.",
        )
    at = snap_point(raw_at)
    size = (max(snap(raw_size[0]), MIN_SIZE), max(snap(raw_size[1]), MIN_SIZE))
    notes: list[str] = []
    if tuple(size) != tuple(snap(c) for c in raw_size):
        notes.append(
            f"size raised to the minimum {MIN_SIZE:g} mm per side: {size[0]:g} x {size[1]:g}"
        )
    if at != raw_at:
        notes.append(f"corner snapped to the grid: {at[0]:g}, {at[1]:g}")
    if size[0] > MAX_SIZE or size[1] > MAX_SIZE:
        raise _bad("Sheet too large", f"size {size} mm", f"Use at most {MAX_SIZE:g} mm per side.")
    parent_arg = args.get("parent")
    file_arg = args.get("file")

    def run(h: KicadHandle) -> ChangeSet:
        parent = resolve_sheet(h, str(parent_arg)[:256] if parent_arg else None)
        used, refs = hierarchy_files(h)
        if (
            isinstance(file_arg, str) and file_arg in used
        ):  # 2A Q7 follow-up: the API makes a blank screen and save() replaces it
            raise _bad(
                "Sheet file is already a sheet of this design",
                f"{file_arg} is used by an existing sheet; creating another sheet symbol for it "
                "through KiCad's API would overwrite that file with an empty schematic",
                "Choose a new file name. A second instance of an existing sheet has to be "
                "made in KiCad itself.",
            )
        file = validate_sheet_file(project, file_arg, existing=set())  # type: ignore[arg-type]
        if parent.path != refs[0].path and "/" in parent.file:
            raise _bad(
                "Unsupported parent sheet",
                f"{parent.human_path} lives in a sub-directory ({parent.file}); sheet files "
                "are only created relative to the project directory",
                "Add the sheet on the root sheet or on a sheet in the project directory.",
            )
        psch = sheet_api(h, parent)
        siblings = {access.field_text(s.name_field) for s in psch.get_sheet_symbols()}
        if name in siblings:
            raise _bad(
                "Sheet name already used",
                f"{parent.human_path} already has a sheet named {name!r}",
                "Choose a different name.",
            )
        scene = read_scene(h, parent)
        box = Box(at[0], at[1], at[0] + size[0], at[1] + size[1])
        if any(box.overlaps(b, 2.54) for b in _blockers(scene)):
            raise _bad(
                "sch_add_sheet refused: the area is occupied",
                f"a sheet at {at} of {size[0]:g} x {size[1]:g} mm would overlap existing items "
                f"on {parent.human_path} (or sit within 2.54 mm of them)",
                "Choose a free spot (sch_find_space) or a smaller size.",
                code="overlap",
            )
        absolute = str(Path(project.dir) / file)

        def fix_path(inp: StageInput) -> list[Any]:
            sym = inp.created[0]
            sym.filename_field.text.value = file  # spike 2A Q7: relative only after the push
            return [Update([sym], sheet=parent)]

        return ChangeSet(
            tool="sch_add_sheet",
            sheet=parent,
            stages=[
                static_stage(
                    "create sheet", Create([build_sheet_symbol(name, absolute, at, size)])
                ),
                Stage("relative file name", fix_path),
            ],
            pre_images={},
            intent=NetIntent(keep_partition=True),
            logical=Logical(1),
            summary={"file": file, "name": name, "parent": parent.human_path},
            notes=list(notes),
        )

    return run


# -- sch_add_sheet_pin --------------------------------------------------------------------

DIRECTIONS = ("input", "output", "bidirectional", "tri_state", "passive")


def _parent_of(h: KicadHandle, child: SheetRef) -> tuple[SheetRef, str]:
    refs = sheet_refs(h)
    head, _, sym_uuid = child.path.rpartition("/")
    parent = next((r for r in refs if r.path == head), None)
    if parent is None or child.path == refs[0].path:
        raise _bad(
            "sch_add_sheet_pin needs a sub-sheet",
            f"{child.human_path} is the root sheet; it has no sheet symbol",
            "Pass the name or path of a sub-sheet like '/Power/'.",
        )
    return parent, sym_uuid


def _free_pin_y(sym: Any, side: str, taken: list[Point]) -> float:
    y0 = mm_point(sym.position)[1]
    h = mm_point(sym.size)[1]
    k = 1
    while True:
        y = snap(y0 + PIN_PITCH * k)
        if y > y0 + h - 1.27 + 1e-6:
            raise _bad(
                "Sheet has no room for another pin",
                f"the {side} edge of the sheet ({h:g} mm high) is full",
                "Use the other side, or make the sheet taller first.",
                code="overlap",
            )
        if y > y0 + 1e-6 and all(abs(y - ty) > PIN_PITCH - 0.01 for _tx, ty in taken):
            return y
        k += 1


def _label_spot(scene: Scene, name: str) -> Point:
    width = 1.4 * len(name)
    x = snap(max(25.4, 12.7 + width))
    blockers = _blockers(scene)
    y = 25.4
    while y < 180:
        cand = Box(x - width - 2, y - 2.54, x + width + 2, y + 2.54)
        if not any(cand.overlaps(b) for b in blockers):
            return (x, snap(y))
        y += 5.08
    raise _bad(
        "No room for the hierarchical label",
        "the sub-sheet has no free spot in its left part",
        "Free some space on the sub-sheet first.",
        code="overlap",
    )


def plan_add_sheet_pin(args: dict[str, Any]) -> Any:
    sheet_arg = args.get("sheet")
    if not isinstance(sheet_arg, str) or not sheet_arg.strip():
        raise _bad("sch_add_sheet_pin needs a sheet", f"sheet was {_short(sheet_arg)}",
                   "Pass the sub-sheet's name or path, e.g. 'Power' or '/Power/'.")  # fmt: skip
    name = validate_pin_name(args.get("name"))  # type: ignore[arg-type]
    direction = args.get("direction")
    if direction not in DIRECTIONS:
        raise _bad("Invalid pin direction", f"direction was {_short(direction)}",
                   f"Use one of {', '.join(DIRECTIONS)}.")  # fmt: skip
    side = args.get("side") or ("right" if direction == "output" else "left")
    if side not in ("left", "right"):
        raise _bad("Invalid side", f"side was {_short(side)}", "Use 'left' or 'right'.")

    def run(h: KicadHandle) -> ChangeSet:
        child = resolve_sheet(h, sheet_arg[:256])
        parent, sym_uuid = _parent_of(h, child)
        psch = sheet_api(h, parent)
        sym = next((s for s in psch.get_sheet_symbols() if access.kiid(s) == sym_uuid), None)
        if sym is None:
            raise StructuredToolError(
                f"Sheet symbol of {child.human_path} not found",
                f"{parent.human_path} has no sheet symbol {sym_uuid}",
                "Re-read the hierarchy and retry.",
                code="not_found",
            )
        pins = list(sym.pins)
        if any(str(p.text.value) == name for p in pins):
            raise _bad(
                "Sheet pin already exists",
                f"{child.human_path} already has a pin named {name!r}",
                "Use another name, or connect to the existing pin.",
            )
        x0, _y0 = mm_point(sym.position)
        width, _h = mm_point(sym.size)
        x = x0 if side == "left" else x0 + width
        taken = [mm_point(p.position) for p in pins if abs(mm_point(p.position)[0] - x) < 0.01]
        y = _free_pin_y(sym, side, taken)
        pin = build_sheet_pin(name, (x, y), side, direction)
        updated = type(sym)(proto=sym.proto)
        updated.pins = [*pins, pin]
        cscene = read_scene(h, child)
        reused = any(
            lb.kind == "hierarchical_label" and lb.text == name for lb in cscene.labels.values()
        )
        ops: list[Any] = [Update([updated], sheet=parent)]
        label_at: Point | None = None
        notes: list[str] = []
        if reused:
            notes.append(
                f"{child.file} already has a hierarchical label {name!r} (shared sheet): "
                "it was reused, no second label was created"
            )
        else:
            label_at = _label_spot(cscene, name)
            ops.append(Create([build_hier_label(name, label_at, direction)], sheet=child))
        return ChangeSet(
            tool="sch_add_sheet_pin",
            sheet=parent,
            stages=[static_stage("sheet pin and label", *ops)],
            pre_images={},
            intent=NetIntent(keep_partition=True),
            logical=Logical(1),
            summary={
                "pin": {
                    "sheet": child.human_path,
                    "name": name,
                    "at_mm": [x, y],
                    "side": side,
                    "direction": direction,
                },
                "label": None
                if label_at is None
                else {"sheet": child.human_path, "at_mm": list(label_at), "text": name},
            },
            notes=notes,
        )

    return run
