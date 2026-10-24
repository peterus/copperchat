"""Labels, power symbols and no-connects at pins (E5 Task 3), planned atomically.

Port of spike 2D `tools.anchors` / `_stub_and_label` / `_power_plans` /
`sch_no_connect`: every target of a multi-pin call is planned first (against a
scene that already holds the items planned for the earlier targets), then one
change set writes all of them — a target without room fails the whole call before
anything is written (2D failure mode 3, Review Focus 4). Rail names as labels are
refused in favour of power symbols (2D failure mode 5).
"""

from __future__ import annotations

import itertools
import re
import time
import uuid as uuidlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Logical,
    NetIntent,
    Place,
    SheetRef,
    Stage,
    StageInput,
    Update,
)
from copperchat.kicad_tools.edit.geometry import (
    EPS_MM,
    NM,
    Box,
    PinEnd,
    Point,
    in_segment_interior,
    on_grid,
    on_segment,
    same_point,
    snap_point,
)
from copperchat.kicad_tools.edit.router import Obstacles, needed_junctions
from copperchat.kicad_tools.edit.scene import (
    MAX_CANDIDATES,
    LabelInfo,
    Scene,
    SheetPinInfo,
    SymbolInfo,
    WireInfo,
    all_symbols,
    net_representative,
    netlists,
    read_scene,
    resolve_pin,
    sheet_refs,
    short,
)
from copperchat.kicad_tools.edit.space import pin_corridor

LabelKind = Literal["local", "global", "hierarchical"]
Dir = tuple[int, int]
Planner = Callable[[Scene, SheetRef], None]
DIRS: dict[str, Dir] = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}
ALL_DIRS: tuple[Dir, ...] = ((1, 0), (-1, 0), (0, -1), (0, 1))
STUB_LENGTHS = (2.54, 5.08, 7.62)
MAX_ANCHORS = 16  # wire-chain points tried per pin
PLAN_BUDGET_S = 12.0  # wall time for planning one call (well below the session timeout)
NET_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-.~#()]{1,64}$")
# Supply-like names that are rails even when no power symbol of that name is known.
RAIL_RE = re.compile(
    r"^([+-]?\d+(\.\d+)?V\d*|GND|[ADP]GND|GND[ADF]|GNDPWR|V(CC|DD|EE|SS|BUS|BAT))$", re.IGNORECASE
)
_KIND_ITEM = {"local": "label", "global": "global_label", "hierarchical": "hierarchical_label"}


def bad(what: str, why: str, suggestion: str, **details: Any) -> StructuredToolError:
    return StructuredToolError(
        what, why, suggestion, code="invalid_argument", details=details or None
    )


def fmt(p: Point) -> str:
    return f"({p[0]:g}, {p[1]:g})"


def is_named(net: str | None) -> bool:
    """A net with a user-given name (label, power symbol), not an automatic one."""
    return bool(net) and not net.startswith(("unconnected-", "Net-("))  # type: ignore[union-attr]


def base_name(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def check_net_name(net: Any, tool: str) -> str:
    text = str(net or "").strip()
    if not NET_NAME_RE.fullmatch(text) or text.startswith(("unconnected-", "Net-(")):
        raise bad(
            f"{tool}: invalid net name {short(net, 70)}",
            "net names are 1-64 characters: letters, digits and _ + - . ~ # ( )",
            "Pass a name like 'I2C1_SDA' or '+3V3'.",
        )
    return text


# -- kipy item builders -----------------------------------------------------------------


def _v2(p: Point) -> Any:
    from kipy.geometry import Vector2

    return Vector2.from_xy(round(p[0] * NM), round(p[1] * NM))


def make_wire(a: Point, b: Point) -> Any:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType
    from kipy.schematic_types import SchematicLine

    w = SchematicLine()
    w.start, w.end = _v2(a), _v2(b)
    w.type = SchematicLineType.SLT_WIRE
    w.proto.id.value = str(uuidlib.uuid4())
    return w


def make_junction(at: Point) -> Any:
    from kipy.schematic_types import Junction

    j = Junction()
    j.position = _v2(at)
    j.proto.id.value = str(uuidlib.uuid4())
    return j


def make_no_connect(at: Point) -> Any:
    from kipy.schematic_types import NoConnectMarker

    n = NoConnectMarker()
    n.position = _v2(at)
    n.proto.id.value = str(uuidlib.uuid4())
    return n


def spin_of(d: Dir) -> Any:
    """Label spin style whose text runs in direction `d` (away from the pin)."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelSpinStyle as S

    return {(1, 0): S.SLSS_RIGHT, (-1, 0): S.SLSS_LEFT, (0, -1): S.SLSS_UP, (0, 1): S.SLSS_BOTTOM}[
        d
    ]


def make_label(text: str, at: Point, d: Dir, kind: LabelKind) -> Any:
    from kipy.schematic_types import GlobalLabel, HierarchicalLabel, LocalLabel

    cls = {"local": LocalLabel, "global": GlobalLabel, "hierarchical": HierarchicalLabel}[kind]
    lb = cls()
    lb.proto.id.value = str(uuidlib.uuid4())
    lb.text.value = text
    lb.position = _v2(at)
    lb.text.position = _v2(at)
    lb.spin_style = spin_of(d)
    return lb


def label_box(at: Point, text: str, d: Dir, kind: str = "label") -> Box:
    """Estimated extent of a label's text running from `at` in direction `d` (1.27 mm
    font, ~0.75 mm per character, plus the shape of global/hierarchical labels)."""
    length = 0.8 + 0.75 * len(text) + (2.6 if kind != "label" else 0.0)
    tip = (at[0] + d[0] * length, at[1] + d[1] * length)
    half = 1.0
    if d[0]:
        return Box(min(at[0], tip[0]), at[1] - half, max(at[0], tip[0]), at[1] + half)
    return Box(at[0] - half, min(at[1], tip[1]), at[0] + half, max(at[1], tip[1]))


_SPIN_DIR = {1: (-1, 0), 2: (0, -1), 3: (1, 0), 4: (0, 1)}


def label_extent(lb: LabelInfo) -> Box:
    """`label_box` of a label read from the design (its text runs as its spin style says)."""
    return label_box(lb.at, lb.text, _SPIN_DIR.get(lb.spin, (1, 0)), lb.kind)


# -- the prepare-time world -------------------------------------------------------------


@dataclass
class Planned:
    """Items a multi-target plan adds, mirrored into the scene for later targets."""

    items: list[Any] = field(default_factory=list)


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def sheet_pin_uuid_not_found(spec: str) -> StructuredToolError:
    return StructuredToolError(
        f"Sheet pin {short(spec)} not found",
        "no sheet pin has that UUID (pins are 'REF.PIN', sheet pins '<sheet>.<pin>' or their UUID)",
        "List the sheet pins with sch_list_items(types=['sheet_pin']).",
        code="not_found",
    )


class World:
    """One consistent read of the design for `prepare` (inside one session.call)."""

    # sheet pins resolved by `sheet_pin` (pseudo key -> pin), replaced on each addition
    _sheet_pin_ends: Mapping[PinKey, SheetPinInfo] = MappingProxyType({})

    def __init__(self, h: KicadHandle) -> None:
        self.h = h
        raw = h.schematic().get_symbols()
        self.raw = raw
        self.symbols: list[SymbolInfo] = all_symbols(h, raw)
        self.netlist: Netlist
        self.netlist, nets = netlists(h, raw)
        self.item_nets: dict[str, dict[str, str]] = {}
        for (path, uid), net in nets.items():
            self.item_nets.setdefault(path, {})[uid] = net
        self.sheets: dict[str, SheetRef] = {r.path: r for r in sheet_refs(h)}
        self.known: set[PinKey] = {k for keys in self.netlist.nets.values() for k in keys}
        self._scenes: dict[str, Scene] = {}
        self._planned: dict[str, list[tuple[str, str, str | None, Planner]]] = {}
        self._pin_nets: dict[tuple[str, str], set[str]] = {}
        self._sheet_symbols: dict[str, list[Any]] = {}
        self._fake = itertools.count()
        # Planning runs inside one session.call (kicad_api_timeout_s): it stops early.
        self.deadline = time.monotonic() + PLAN_BUDGET_S

    def instances(self, sheet: SheetRef) -> list[SheetRef]:
        """Every instance of `sheet`'s file: an edit in a shared sheet file shows in all
        of them, so connectivity changes in each."""
        return [r for r in self.sheets.values() if r.file == sheet.file] or [sheet]

    def replicate(self, keys: Iterable[PinKey], sheet: SheetRef) -> list[frozenset[PinKey]]:
        """One group per instance of `sheet`'s file: keys on `sheet` moved to each
        instance. Keys on other sheets (a representative of a net reaching the sheet
        through a sheet pin) stay in `sheet`'s own group only: another instance joins
        its own copy of that net."""
        keys = list(dict.fromkeys(k for k in map(self.real_key, keys) if k is not None))
        out = []
        for inst in self.instances(sheet):
            if inst.path == sheet.path:
                out.append(frozenset(keys))
                continue
            group = {(inst.path, u, n) for p, u, n in keys if p == sheet.path}
            group &= self.known  # a symbol placed through one instance has no pins in
            out.append(frozenset(group))  # the others (pinned KiCad): only real ones
        return out

    def sheet_pin_nets(self, sheet: SheetRef, text: str) -> set[str]:
        """Nets on the parent sheet at the sheet pin named `text` of this sheet instance
        (a hierarchical label of that name joins them)."""
        from copperchat.kicad_tools import access
        from copperchat.kicad_tools.edit.geometry import mm_point

        cached = self._pin_nets.get((sheet.path, text))
        if cached is not None:
            return cached
        parent_path, _, symbol_uuid = sheet.path.rpartition("/")
        parent = self.sheets.get(parent_path)
        if parent is None:
            return set()
        out: set[str] = set()
        for symbol in self.sheet_symbol_list(parent):
            if access.kiid(symbol) != symbol_uuid:
                continue
            for pin in symbol.pins:
                if str(pin.text.value) != text:
                    continue
                net = self.nets(parent).get(access.kiid(pin))
                if net is not None:
                    out.add(net)
                else:
                    out |= touched_at(self, parent, mm_point(pin.position))[0]
        self._pin_nets[(sheet.path, text)] = out
        return out

    def check_time(self, tool: str) -> None:
        if time.monotonic() > self.deadline:
            raise StructuredToolError(
                f"{tool}: planning took too long",
                f"no clean position was found within {PLAN_BUDGET_S:g} s on this sheet",
                "Call it for fewer pins at a time, or give the parts more free room.",
                code="overlap",
            )

    def scene(self, sheet: SheetRef) -> Scene:
        if sheet.path not in self._scenes:
            self._scenes[sheet.path] = read_scene(self.h, sheet)
            for origin, uid, net, put in self._planned.get(sheet.file, []):
                self._replay(sheet, origin, uid, net, put)
        return self._scenes[sheet.path]

    def nets(self, sheet: SheetRef) -> dict[str, str]:
        return self.item_nets.setdefault(sheet.path, {})

    def pin(self, spec: Any) -> PinEnd:
        if not isinstance(spec, str):
            raise bad(
                "Invalid pin",
                f"{short(spec)} is not a 'REF.PIN' string",
                "Pass pins like 'U1.3' or 'U1.VIN'.",
            )
        return resolve_pin(self.h, spec[:64], symbols=self.symbols)

    def endpoint(self, spec: Any) -> PinEnd:
        """A symbol pin 'REF.PIN', or a sheet pin by UUID or as '<sheet name>.<pin name>'
        (a symbol reference wins over a sheet name of the same text)."""
        if isinstance(spec, str) and (by_uuid := self.sheet_pin_by_uuid(spec)) is not None:
            return by_uuid
        if isinstance(spec, str) and UUID_RE.fullmatch(spec.strip()):
            raise sheet_pin_uuid_not_found(spec)
        if isinstance(spec, str) and not any(
            s.ref == spec.strip().partition(".")[0] for s in self.symbols
        ):
            pin = self.sheet_pin(spec.strip()[:64])
            if pin is not None:
                return pin
        return self.pin(spec)

    def sheet_pin(self, spec: str, *, one_item: bool = False) -> PinEnd | None:
        """'<sheet name>.<pin name>' as a pseudo pin end (see `SheetPinInfo`); None when
        no sheet instance has a name `spec` starts with (the longest name wins: sheet
        names may contain dots). `one_item`: a sheet symbol in a parent file used by
        several instances is one item (sch_move / sch_delete): its first instance."""
        named = [r for r in self.sheets.values() if spec.startswith(f"{r.name}.") and r.name]
        named = [r for r in named if r.path.rpartition("/")[0] in self.sheets]  # not the root
        if not named:
            return None
        longest = max(len(r.name) for r in named)
        named = [r for r in named if len(r.name) == longest]
        if one_item:
            items: dict[tuple[str, str], SheetRef] = {}
            for r in named:
                parent, _, uid = r.path.rpartition("/")
                items.setdefault((self.sheets[parent].file, uid), r)
            named = list(items.values())
        name, wanted = named[0].name, spec[longest + 1 :]
        if len(named) > 1:
            places = sorted(self.sheets[r.path.rpartition("/")[0]].human_path for r in named)
            raise StructuredToolError(
                f"Sheet pin {short(spec)} is ambiguous",
                f"{len(named)} sheet symbols are named {name} (on {', '.join(places[:6])})",
                "Pass the sheet pin's UUID (sch_list_items types=['sheet_pin']), rename "
                "one of the sheets, or connect inside the sheet with labels.",
                code="ambiguous_pin",
                details={"candidates": places[:MAX_CANDIDATES], "total": len(places)},
            )
        child = named[0]
        parent_path, _, sheet_uuid = child.path.rpartition("/")
        pins = [
            i
            for i in self.scene(self.sheets[parent_path]).sheet_pins.values()
            if i.sheet_uuid == sheet_uuid
        ]
        hits = [i for i in pins if i.pin.name == wanted]
        if not hits:
            names = sorted(i.pin.name for i in pins)
            raise StructuredToolError(
                f"Sheet pin {short(spec)} not found",
                f"the sheet {name} has "
                + (f"the pins {', '.join(names[:MAX_CANDIDATES])}" if names else "no pins"),
                "Use one of the listed pin names, or add the pin with sch_add_sheet_pin.",
                code="not_found",
                details={"candidates": names[:MAX_CANDIDATES], "total": len(names)},
            )
        if len(hits) > 1:
            raise StructuredToolError(
                f"Sheet pin {short(spec)} is ambiguous",
                f"the sheet {name} has {len(hits)} pins named {wanted}",
                "Rename the duplicate sheet pins first.",
                code="ambiguous_pin",
                details={"candidates": [wanted]},
            )
        self._sheet_pin_ends = {**self._sheet_pin_ends, hits[0].pin.key: hits[0]}
        return hits[0].pin

    def sheet_pin_by_uuid(self, spec: str) -> PinEnd | None:
        """A sheet pin by its UUID as a pseudo pin end; None when `spec` is no UUID or no
        sheet pin has it. KiCad's GetItemsById skips sheet pins: the sheet symbols of each
        sheet file are scanned, and only the sheet holding the pin is read in full."""
        from copperchat.kicad_tools.edit.scene import _iter_sheets

        uid = spec.strip().lower()
        if not UUID_RE.fullmatch(uid):
            return None
        files: set[str] = set()
        for inst, sheet in _iter_sheets(self.h):  # one hierarchy walk for the scan
            if sheet.file in files or sheet.path not in self.sheets:
                continue
            files.add(sheet.file)
            self.check_time("item lookup")
            symbols = self.sheet_symbol_list(sheet, inst)
            if any(access.kiid(p) == uid for s in symbols for p in s.pins):
                info = self.scene(sheet).sheet_pins.get(uid)
                if info is None:
                    return None
                self._sheet_pin_ends = {**self._sheet_pin_ends, info.pin.key: info}
                return info.pin
        return None

    def sheet_symbol_list(self, sheet: SheetRef, inst: Any = None) -> list[Any]:
        """The sheet symbols on `sheet` (read once per world; `inst`: its hierarchy node
        when the caller already has it, saving a hierarchy walk). Copy before changing."""
        from copperchat.kicad_tools.edit.scene import sheet_api

        if sheet.path not in self._sheet_symbols:
            api = (
                self.h.schematic().for_sheet(inst) if inst is not None else sheet_api(self.h, sheet)
            )
            self._sheet_symbols[sheet.path] = list(api.get_sheet_symbols())
        return self._sheet_symbols[sheet.path]

    def sheet_pin_info(self, key: PinKey) -> SheetPinInfo | None:
        return self._sheet_pin_ends.get(key)

    def is_sheet_pin(self, pin: PinEnd) -> bool:
        return pin.key in self._sheet_pin_ends

    def real_key(self, key: PinKey) -> PinKey | None:
        """`key` itself for a symbol pin; for a sheet pin a representative symbol pin of
        the net it joins (None: that net has no symbol pin). Intents hold real keys only."""
        info = self._sheet_pin_ends.get(key)
        if info is None:
            return key
        return net_representative(self.netlist, self.sheet_pin_net(info))

    def sheet_pin_net(self, info: SheetPinInfo) -> str | None:
        parent, child = info.pin.key[0], info.child
        net = self.nets(self.sheets[parent]).get(info.uuid)
        if net is not None or child not in self.sheets:
            return net
        child_ref = self.sheets[child]
        nets = self.nets(child_ref)
        for lb in self.scene(child_ref).labels.values():
            if lb.kind == "hierarchical_label" and lb.text == info.pin.name and lb.uuid in nets:
                return nets[lb.uuid]
        return None

    def sheet_of(self, pin: PinEnd) -> SheetRef:
        return self.sheets[pin.key[0]]

    def owner(self, pin: PinEnd) -> SymbolInfo:
        """The symbol a pin belongs to; for a sheet pin a stand-in for the sheet symbol
        (its uuid lets a stub leave the pin across the sheet box edge)."""
        info = self._sheet_pin_ends.get(pin.key)
        if info is not None:
            sheet = self.sheet_of(pin)
            return SymbolInfo(
                uuid=info.sheet_uuid,
                ref=pin.ref,
                lib_id="",
                value="",
                unit=1,
                sheet=sheet,
                at=pin.at,
                rotation=0,
                mirror="",
                pins=[pin],
                body=self.scene(sheet).sheets.get(info.sheet_uuid, Box(*pin.at, *pin.at)),
                fields=[],
                power=False,
            )
        return next(s for s in self.symbols if s.uuid == pin.key[1] and s.sheet.path == pin.key[0])

    def net(self, pin: PinEnd) -> str | None:
        info = self._sheet_pin_ends.get(pin.key)
        if info is not None:
            return self.sheet_pin_net(info)
        return self.netlist.net_of(pin.key)

    def members(self, net: str | None) -> frozenset[PinKey]:
        return self.netlist.nets.get(net, frozenset()) if net else frozenset()

    def power_values(self) -> set[str]:
        """Net names carried by placed power symbols (PWR_FLAG excluded)."""
        return {s.value for s in self.symbols if s.power and s.value and s.value != "PWR_FLAG"}

    def connected(self, pin: PinEnd) -> bool:
        """Something else is attached to the pin (a wire, label, other pin, marker)."""
        scene = self.scene(self.sheet_of(pin))
        here = [
            i
            for i in scene.items_at(pin.at)
            if not (i["kind"] == "pin" and i["uuid"] == pin.key[1] and i["pin"] == pin.number)
        ]
        return bool(here) or len(self.members(self.net(pin))) > 1

    # Planned items become part of the scene, so later targets avoid them. They are
    # written into the sheet *file*: every instance of a shared file sees them.
    def fake_id(self) -> str:
        return f"planned-{next(self._fake)}"

    def _plan(self, sheet: SheetRef, uid: str, net: str | None, put: Planner) -> None:
        self._planned.setdefault(sheet.file, []).append((sheet.path, uid, net, put))
        for inst in self.instances(sheet):
            if inst.path in self._scenes:
                self._replay(inst, sheet.path, uid, net, put)

    def _replay(self, inst: SheetRef, origin: str, uid: str, net: str | None, put: Planner) -> None:
        put(self._scenes[inst.path], inst)
        if net is not None and inst.path == origin:  # other instances: net unknown
            self.nets(inst)[uid] = net

    def add_wire(self, sheet: SheetRef, a: Point, b: Point, net: str | None) -> None:
        uid = self.fake_id()
        self._plan(sheet, uid, net, lambda sc, _i: sc.wires.__setitem__(uid, WireInfo(uid, a, b)))

    def add_label(
        self, sheet: SheetRef, text: str, at: Point, kind: str, d: Dir, net: str | None
    ) -> None:
        uid = self.fake_id()
        spin = next(k for k, v in _SPIN_DIR.items() if v == d)
        info = LabelInfo(uid, text, at, kind, spin)
        self._plan(sheet, uid, net, lambda sc, _i: sc.labels.__setitem__(uid, info))

    def add_body(self, sheet: SheetRef, box: Box, at: Point) -> None:
        """A planned power symbol: its body is an obstacle, its pin a connection point."""
        uid = self.fake_id()

        def put(scene: Scene, inst: SheetRef) -> None:
            pin = PinEnd((inst.path, uid, "1"), "#planned", "1", "", "power_in", at, "up")
            scene.symbols[uid] = SymbolInfo(
                uuid=uid,
                ref="#planned",
                lib_id="",
                value="",
                unit=1,
                sheet=inst,
                at=at,
                rotation=0,
                mirror="",
                pins=[pin],
                body=box,
                fields=[],
                power=True,
            )

        self._plan(sheet, uid, None, put)

    def add_junction(self, sheet: SheetRef, at: Point) -> None:
        uid = self.fake_id()
        self._plan(sheet, uid, None, lambda sc, _i: sc.junctions.__setitem__(uid, at))


# -- anchors: where a stub for a pin can start ------------------------------------------


def anchors(scene: Scene, pin: PinEnd) -> list[tuple[Point, list[Dir]]]:
    """The pin end (outward only) if nothing leaves it that way, then the free ends and
    corners of the wire chain already attached to the pin (nearest first), each with
    the directions no wire leaves it in."""
    pts: list[Point] = [pin.at]
    frontier = [pin.at]
    while frontier and len(pts) < 4 * MAX_ANCHORS:
        q = frontier.pop()
        for w in scene.wires.values():
            for x, y in ((w.start, w.end), (w.end, w.start)):
                if same_point(x, q) and not any(same_point(y, z) for z in pts):
                    pts.append(y)
                    frontier.append(y)
    pts.sort(
        key=lambda z: (not same_point(z, pin.at), abs(z[0] - pin.at[0]) + abs(z[1] - pin.at[1]))
    )
    out: list[tuple[Point, list[Dir]]] = []
    for q in pts[:MAX_ANCHORS]:
        used: set[Dir] = set()
        for w in scene.wires.values():
            for x, y in ((w.start, w.end), (w.end, w.start)):
                if same_point(x, q):
                    used.add(
                        (int((y[0] > x[0]) - (y[0] < x[0])), int((y[1] > x[1]) - (y[1] < x[1])))
                    )
                elif in_segment_interior(q, w.start, w.end):
                    used |= (
                        {(1, 0), (-1, 0)}
                        if abs(w.start[1] - w.end[1]) < EPS_MM
                        else {(0, 1), (0, -1)}
                    )
        dirs = [DIRS[pin.direction]] if same_point(q, pin.at) else list(ALL_DIRS)
        dirs = [d for d in dirs if d not in used]
        if dirs:
            out.append((q, dirs))
    return out


class Clearance:
    """What new text and power symbols must not cover, read once per planned target:
    symbol bodies (no margin), field boxes, sheet symbols, label texts, connection
    points and wires."""

    def __init__(self, scene: Scene) -> None:
        self.boxes: list[Box] = []
        for s in scene.symbols.values():
            self.boxes.append(s.body)
            self.boxes.extend(s.fields)
        self.boxes.extend(scene.sheets.values())
        for lb in scene.labels.values():
            self.boxes.append(label_extent(lb))
        self.points = [p.at for p in scene.all_pins()] + [lb.at for lb in scene.labels.values()]
        self.points += list(scene.junctions.values()) + list(scene.no_connects.values())
        self.wires = [(w.start, w.end) for w in scene.wires.values()]

    def clear(self, box: Box, ignore: Iterable[Point] = ()) -> bool:
        """`box` overlaps no obstacle, holds no connection point and no wire enters it."""
        if any(box.overlaps(b) for b in self.boxes):
            return False
        skip = list(ignore)
        for p in self.points:
            if box.contains(p) and not any(same_point(p, s) for s in skip):
                return False
        return not any(box.hit_by_segment(a, b) for a, b in self.wires)


# -- labels -----------------------------------------------------------------------------


@dataclass
class LabelPlan:
    pin: PinEnd
    sheet: SheetRef
    items: list[Any]
    at: Point
    stub: list[Point]
    note: str | None = None


def plan_label(
    world: World, pin: PinEnd, text: str, kind: LabelKind, allowed: set[str]
) -> LabelPlan | None:
    """Stub + label at the pin (or at the free end of the wire already on it); the
    planned items are added to the world's scene. None: no room."""
    sheet = world.sheet_of(pin)
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    net = world.net(pin)
    owner = world.owner(pin)
    item_kind = _KIND_ITEM[kind]
    room = Clearance(scene)
    if not on_grid(pin.at):  # an off-grid pin end: the label sits on it, no stub
        d = DIRS[pin.direction]
        if len(scene.items_at(pin.at)) > 1:
            return None
        if not room.clear(label_box(pin.at, text, d, item_kind), ignore=[pin.at]):
            return None
        world.add_label(sheet, text, pin.at, item_kind, d, net)
        return LabelPlan(pin, sheet, [make_label(text, pin.at, d, kind)], pin.at, [pin.at])
    obstacles = Obstacles(scene, world.netlist, allowed, nets)
    for q, dirs in anchors(scene, pin):
        world.check_time("sch_add_label")
        at_pin = same_point(q, pin.at)
        for d in dirs:
            for length in STUB_LENGTHS:
                end = snap_point((q[0] + d[0] * length, q[1] + d[1] * length))
                own = (owner.uuid if at_pin else None, None)
                if obstacles.problems([q, end], own, first_only=True):
                    continue
                if scene.items_at(end):
                    continue
                if not room.clear(label_box(end, text, d, item_kind), ignore=[end]):
                    continue
                junctions = needed_junctions([q, end], scene)
                items = [make_wire(q, end), make_label(text, end, d, kind)]
                items += [make_junction(j) for j in junctions]
                world.add_wire(sheet, q, end, net)
                world.add_label(sheet, text, end, item_kind, d, net)
                for j in junctions:
                    world.add_junction(sheet, j)
                note = (
                    None
                    if at_pin
                    else (
                        f"{pin.label}: label attached at the wire end {fmt(q)} (the pin is wired)"
                    )
                )
                return LabelPlan(pin, sheet, items, end, [q, end], note)
    return None


def same_name_nets(world: World, text: str, kind: LabelKind, sheet: SheetRef) -> set[str]:
    """Existing nets a new label `text` of `kind` on `sheet` joins: labels of the same
    name on the sheet (local + hierarchical connect by name within a sheet); for a
    global label the global net of that name."""
    out: set[str] = set()
    if kind == "global":
        if text in world.netlist.nets:
            out.add(text)
        scene = world.scene(sheet)
        nets = world.nets(sheet)
        out |= {
            nets[lb.uuid]
            for lb in scene.labels.values()
            if lb.kind == "global_label" and lb.text == text and lb.uuid in nets
        }
        return out
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    for lb in scene.labels.values():
        if lb.kind in ("label", "hierarchical_label") and lb.text == text and lb.uuid in nets:
            out.add(nets[lb.uuid])
    local = f"{sheet.human_path}{text}" if sheet.human_path.endswith("/") else text
    if local in world.netlist.nets:
        out.add(local)
    if kind == "hierarchical":
        out |= world.sheet_pin_nets(sheet, text)
    return out


async def is_rail(ctx: Any, world_values: set[str], net: str) -> bool:
    """A power net: a placed power symbol carries it, the `power` library has a symbol
    of that name, or it looks like a supply (GND, +3V3, VCC ...)."""
    if net in world_values or RAIL_RE.fullmatch(net):
        return True
    from copperchat.library.service import library_for

    # every symbol of KiCad's 'power' library is a power symbol (an index lookup, no
    # library read per call)
    return library_for(ctx.session).has_symbol(f"power:{net}")


def refuse_rail_label(net: str, tool: str) -> StructuredToolError:
    return bad(
        f"{tool}: {net} is a power net",
        "a label named like a rail does not join the rail (local vs. global) and ERC "
        "reports it; rails are connected with power symbols",
        f"Use sch_add_power(net='{net}', at=[...]) instead.",
    )


def check_named_conflict(world: World, pins: list[PinEnd], name: str, tool: str) -> None:
    """Refuse when a pin already sits on a net with another user-given name (the net
    would carry two names; ERC multiple_net_names)."""
    for p in pins:
        net = world.net(p)
        if is_named(net) and base_name(net) != base_name(name):  # type: ignore[arg-type]
            raise bad(
                f"{tool}: {p.label} is already on net {net}",
                f"connecting it to {name} would give one net two names",
                f"Rename the net (sch_rename_net(old='{base_name(net)}', new='{name}')) "  # type: ignore[arg-type]
                "or pick the right pin.",
            )


def label_merges(
    world: World, plans: list[LabelPlan], text: str, kind: LabelKind
) -> list[frozenset[PinKey]]:
    """Pins that end on one net: per sheet for local/hierarchical labels, across the
    design for global ones, each with the existing nets of the same name."""
    groups: dict[str, set[PinKey]] = {}
    for plan in plans:
        insts = world.instances(plan.sheet)  # a shared sheet file: every instance
        for inst, keys in zip(insts, world.replicate([plan.pin.key], plan.sheet), strict=True):
            if not keys:
                continue
            scope = "*" if kind == "global" else inst.path
            group = groups.setdefault(scope, set())
            group |= keys
            for net in same_name_nets(world, text, kind, inst):
                group |= set(sorted(world.members(net))[:1])
    return [frozenset(g) for g in groups.values() if len(g) > 1]


# -- power symbols ----------------------------------------------------------------------


@dataclass
class PowerSymbol:
    lib_id: str
    value: str
    pin_offset: Point  # connection point relative to the symbol origin (rotation 0)
    body_dir: Dir  # where the body lies from the pin at rotation 0
    size: tuple[float, float]  # body width / height (rotation 0)
    flag: bool


@dataclass
class PowerPlan:
    pin: PinEnd
    sheet: SheetRef
    wires: list[Any]
    at: Point  # symbol origin
    rotation: int
    reference: str
    stub: list[Point]
    note: str | None = None


def _rot(v: Point, rotation: int) -> Point:
    x, y = v
    for _ in range((rotation // 90) % 4):  # 90 deg turns counter-clockwise on screen
        x, y = y, -x
    return (x, y)


def power_box(sym: PowerSymbol, pin_at: Point, rotation: int) -> Box:
    """Body + value text of a power symbol whose pin sits at `pin_at`."""
    w, h = sym.size
    d = _rot(sym.body_dir, rotation)
    depth = max(h if d[1] else w, 1.27) + 2.0  # + the value text beyond the body
    width = max(w if d[1] else h, 1.27) + 0.5
    if d[1]:
        y1 = pin_at[1] + d[1] * depth
        return Box(
            pin_at[0] - width / 2, min(pin_at[1], y1), pin_at[0] + width / 2, max(pin_at[1], y1)
        )
    x1 = pin_at[0] + d[0] * depth
    return Box(min(pin_at[0], x1), pin_at[1] - width / 2, max(pin_at[0], x1), pin_at[1] + width / 2)


def _power_candidates(
    scene: Scene, pin: PinEnd, body: Dir
) -> Iterator[tuple[list[Point], Point, int]]:
    """(wire points, pin position of the symbol, rotation): upright symbols first, then
    shorter stubs; upside-down ones last."""
    cands: list[tuple[tuple[bool, int, float], list[Point], Point, int]] = []
    for ai, (q, dirs) in enumerate(anchors(scene, pin)):
        at_pin = same_point(q, pin.at)
        for d in dirs:
            for length in STUB_LENGTHS:
                end = snap_point((q[0] + d[0] * length, q[1] + d[1] * length))
                if d == body:
                    cands.append(((False, ai, length), [q, end], end, 0))
                elif d == (-body[0], -body[1]):
                    if at_pin:
                        cands.append(((True, ai, length), [q, end], end, 180))
                else:
                    up = snap_point((end[0] + body[0] * 2.54, end[1] + body[1] * 2.54))
                    cands.append(((False, ai, length + 2.54), [q, end, up], up, 0))
                    down = snap_point((end[0] - body[0] * 2.54, end[1] - body[1] * 2.54))
                    cands.append(((True, ai, length + 2.54), [q, end, down], down, 180))
    cands.sort(key=lambda c: c[0])
    for _key, pts, at, rot in cands:
        yield pts, at, rot


NEIGHBOUR_ROOM_MM = 5.08  # one stub length plus a grid step in front of a neighbour pin


def _blocks_neighbour(
    pts: list[Point], pin_at: Point, rot: int, sym: PowerSymbol, corridors: list[Box]
) -> bool:
    box = power_box(sym, pin_at, rot)
    return any(
        box.overlaps(c) or any(c.hit_by_segment(a, b) for a, b in itertools.pairwise(pts))
        for c in corridors
    )


def _by_room(
    candidates: list[tuple[list[Point], Point, int]], sym: PowerSymbol, corridors: list[Box]
) -> Iterator[tuple[list[Point], Point, int]]:
    """Candidates that leave the neighbour pins' corridors free first, the rest after."""
    rest = []
    for cand in candidates:
        if _blocks_neighbour(cand[0], cand[1], cand[2], sym, corridors):
            rest.append(cand)
        else:
            yield cand
    yield from rest


def plan_power(
    world: World, pin: PinEnd, sym: PowerSymbol, allowed: set[str], reference: str
) -> PowerPlan | None:
    sheet = world.sheet_of(pin)
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    owner = world.owner(pin)
    net = world.net(pin)
    obstacles = Obstacles(scene, world.netlist, allowed, nets)
    room = Clearance(scene)
    # The line in front of every other pin stays free for that pin's own stub/label (#11):
    # a stub or symbol standing in it leaves the neighbour no room. Preferred, not
    # absolute: a part with no variant outside the corridors still gets its power symbol.
    corridors = [
        pin_corridor(p, NEIGHBOUR_ROOM_MM, 0.5)
        for p in scene.all_pins()
        if not same_point(p.at, pin.at)
    ]
    candidates = list(_power_candidates(scene, pin, sym.body_dir))
    for n, (pts, pin_at, rot) in enumerate(_by_room(candidates, sym, corridors)):
        if n % 16 == 0:
            world.check_time("sch_add_power")
        at_pin = same_point(pts[0], pin.at)
        if obstacles.problems(pts, (owner.uuid if at_pin else None, None), first_only=True):
            continue
        if scene.items_at(pin_at):
            continue
        box = power_box(sym, pin_at, rot)
        if not room.clear(box.grow(0.3), ignore=[pin_at]):
            continue
        if any(box.grow(0.3).hit_by_segment(a, b) for a, b in itertools.pairwise(pts[:-1])):
            continue
        junctions = needed_junctions(pts, scene)
        wires = [make_wire(a, b) for a, b in itertools.pairwise(pts)]
        wires += [make_junction(j) for j in junctions]
        for a, b in itertools.pairwise(pts):
            world.add_wire(sheet, a, b, net)
        for j in junctions:
            world.add_junction(sheet, j)
        world.add_body(sheet, box, pin_at)
        off = _rot(sym.pin_offset, rot)
        origin = (round(pin_at[0] - off[0], 4), round(pin_at[1] - off[1], 4))
        note = (
            None
            if at_pin
            else (
                f"{pin.label}: power symbol attached at the wire end {fmt(pts[0])} "
                "(the pin is wired)"
            )
        )
        if rot:
            note = (
                note + "; " if note else f"{pin.label}: "
            ) + "symbol placed upside down (no room upright, or it would block a neighbour)"
        return PowerPlan(pin, sheet, wires, origin, rot, reference, pts, note)
    return None


def power_symbol(detail: Any) -> PowerSymbol:
    """Geometry of a library power symbol (E3 SymbolDetail): pin offset and body side."""
    pins = [p for p in detail.pins if p.unit in (0, 1)] or list(detail.pins)
    if not pins:
        raise bad(
            f"{detail.lib_id} has no pin",
            "a power symbol connects through its single pin",
            "Pick a symbol from the power library (lib_search_symbols(library='power')).",
        )
    pin = pins[0]
    outward = DIRS.get(pin.direction or "up", (0, -1))
    return PowerSymbol(
        lib_id=detail.lib_id,
        value=detail.value or detail.lib_id.split(":", 1)[-1],
        pin_offset=(float(pin.at_mm[0]), float(pin.at_mm[1])),
        body_dir=(-outward[0], -outward[1]),
        size=(float(detail.body_size_mm[0]) or 2.54, float(detail.body_size_mm[1]) or 2.54),
        flag=detail.lib_id.split(":", 1)[-1].upper() == "PWR_FLAG",
    )


# -- no-connects ------------------------------------------------------------------------


def no_connect_state(world: World, pin: PinEnd) -> Literal["free", "marked", "connected"]:
    scene = world.scene(world.sheet_of(pin))
    if any(same_point(n, pin.at) for n in scene.no_connects.values()):
        return "marked"
    return "connected" if world.connected(pin) else "free"


# -- positions --------------------------------------------------------------------------


def touched_at(world: World, sheet: SheetRef, p: Point) -> tuple[set[str], list[str], Dir]:
    """Nets of the wires/pins at `p`, what is there, and a readable text direction."""
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    found: set[str] = set()
    what: list[str] = []
    d: Dir = (1, 0)
    for s in scene.symbols.values():
        for pin in s.pins:
            if same_point(pin.at, p):
                what.append(f"pin {pin.label}")
                if (n := world.netlist.net_of(pin.key)) is not None:
                    found.add(n)
                d = DIRS[pin.direction]
    for info in scene.sheet_pins.values():
        if same_point(info.pin.at, p):
            what.append(f"sheet pin {info.pin.label}")
            if (n := world.sheet_pin_net(info)) is not None:
                found.add(n)
            d = DIRS[info.pin.direction]
    for w in scene.wires.values():
        if on_segment(p, w.start, w.end):
            what.append("wire")
            if (n := nets.get(w.uuid)) is not None:
                found.add(n)
            if not what[:-1]:
                horizontal = abs(w.start[1] - w.end[1]) < EPS_MM
                if same_point(p, w.start) or same_point(p, w.end):
                    other = w.end if same_point(p, w.start) else w.start
                    d = (
                        int((p[0] > other[0]) - (p[0] < other[0])),
                        int((p[1] > other[1]) - (p[1] < other[1])),
                    )
                else:
                    d = (1, 0) if horizontal else (0, -1)
    return found, what, d


# -- change sets ------------------------------------------------------------------------


def no_change(tool: str, sheet: SheetRef, summary: dict[str, Any], notes: list[str]) -> ChangeSet:
    """Nothing to write (every target already is as asked): an empty, verified call."""
    return ChangeSet(
        tool=tool,
        sheet=sheet,
        stages=[],
        pre_images={},
        intent=NetIntent(),
        logical=Logical(0),
        summary=summary,
        notes=notes,
    )


def _creates(by_sheet: dict[str, tuple[SheetRef, list[Any]]]) -> list[Any]:
    return [Create(items, sheet=sheet) for sheet, items in by_sheet.values() if items]


def _collect(
    by_sheet: dict[str, tuple[SheetRef, list[Any]]], sheet: SheetRef, items: list[Any]
) -> None:
    by_sheet.setdefault(sheet.path, (sheet, []))[1].extend(items)


def no_room(tool: str, pin: PinEnd, what: str, done: int) -> StructuredToolError:
    return StructuredToolError(
        f"{tool}: no room for {what} at {pin.label}",
        f"every stub/{what} position at {pin.label} {fmt(pin.at)} would hit another item "
        "or connect another net" + (" (nothing was changed by this call)" if done else ""),
        "Move the part so the pin has free room in front of it (sch_move / sch_find_space), "
        "or use another pin, then call again.",
        code="overlap",
        details={"pin": pin.label},
    )


def sheet_pin_joined(
    world: World,
    pins: list[PinEnd],
    items: list[tuple[SheetRef, Any]],
    *,
    one_net: bool,
) -> list[frozenset[tuple[str, ...]]]:
    """`NetIntent.joined` groups for sheet pins among `pins`: the sheet pin's own netlist
    item, the new `items` (labels, stub wires) and the call's symbol pins end on one net
    — the pin partition cannot say so when the sub-sheet net has no symbol pin.
    `one_net`: everything joins across sheets (global label, power symbol); else only
    what is on the sheet pin's sheet."""
    out: list[frozenset[tuple[str, ...]]] = []
    for p in pins:
        info = world.sheet_pin_info(p.key)
        if info is None:
            continue
        here = info.pin.key[0]
        group: set[tuple[str, ...]] = {(here, info.uuid)}
        group |= {(s.path, access.kiid(i)) for s, i in items if one_net or s.path == here}
        for q in pins:
            k = world.real_key(q.key)
            if k is not None and (one_net or k[0] == here):
                group.add(k)
        if len(group) > 1:
            out.append(frozenset(group))
    return out


def labels_change_set(
    world: World, tool: str, pins: list[PinEnd], text: str, kind: LabelKind
) -> ChangeSet:
    """Stub + label for each pin (all or nothing). Pins already on a net of that name
    are skipped with a note."""
    if kind == "hierarchical":
        for p in pins:
            if world.sheet_of(p).path == next(iter(world.sheets)):
                raise bad(
                    f"{tool}: a hierarchical label needs a sub-sheet",
                    f"{p.label} is on the root sheet; a hierarchical label connects a "
                    "sub-sheet to its sheet pin",
                    "Use kind='local' or 'global' on the root sheet.",
                )
    check_named_conflict(world, pins, text, tool)
    plans: list[LabelPlan] = []
    notes: list[str] = []
    for p in pins:
        net = world.net(p)
        sheet = world.sheet_of(p)
        joined = same_name_nets(world, text, kind, sheet)
        if net is not None and net in joined:  # the net a new label would join anyway
            notes.append(f"{p.label} is already on net {net}; nothing added there")
            continue
        allowed = {n for n in (net,) if n is not None} | joined
        plan = plan_label(world, p, text, kind, allowed)
        if plan is None:
            raise no_room(tool, p, "a label", len(plans))
        plans.append(plan)
        if plan.note:
            notes.append(plan.note)
    first = world.sheet_of(pins[0])
    summary: dict[str, Any] = {
        "net": text,
        "kind": kind,
        "labels": [
            {"pin": pl.pin.label, "at_mm": list(pl.at), "stub_mm": [list(q) for q in pl.stub]}
            for pl in plans
        ],
    }
    if not plans:
        return no_change(tool, first, summary, notes)
    by_sheet: dict[str, tuple[SheetRef, list[Any]]] = {}
    for pl in plans:
        _collect(by_sheet, pl.sheet, pl.items)
    ops = _creates(by_sheet)
    return ChangeSet(
        tool=tool,
        sheet=first,
        stages=[Stage(f"label {text}", lambda _inp: list(ops))],
        pre_images={},
        intent=NetIntent(
            merge=label_merges(world, plans, text, kind),
            joined=sheet_pin_joined(
                world,
                pins,
                [
                    (pl.sheet, i)
                    for pl in plans
                    for i in pl.items
                    if type(i).__name__.endswith("Label")
                ],
                one_net=kind == "global",
            ),
        ),
        logical=Logical(len(plans)),
        summary=summary,
        notes=notes,
    )


def label_at_point_change_set(
    world: World, tool: str, sheet: SheetRef, raw: Point, text: str, kind: LabelKind
) -> ChangeSet:
    """A label exactly on a wire or pin end at `raw` (no stub)."""
    scene = world.scene(sheet)
    exact = next((p.at for p in scene.all_pins() if same_point(p.at, raw, 1e-4)), None)
    at = exact or snap_point(raw)
    found, what, d = touched_at(world, sheet, at)
    if not what:
        raise bad(
            f"{tool}: the label at {fmt(at)} would connect nothing",
            "a label connects only where it touches a wire or a pin end; there is none at "
            "that point",
            "Pass a point on a wire (or a pin end), or label pins with at=['REF.PIN', ...].",
        )
    if len(found) > 1:
        raise StructuredToolError(
            f"{tool}: {fmt(at)} touches several nets",
            f"the point touches {', '.join(sorted(found))}; a label there would short them",
            "Pick a point on exactly one net.",
            code="overlap",
        )
    if kind == "hierarchical" and sheet.path == next(iter(world.sheets)):
        raise bad(
            f"{tool}: a hierarchical label needs a sub-sheet",
            "this point is on the root sheet",
            "Use kind='local' or 'global'.",
        )
    net = next(iter(found), None)
    if is_named(net) and base_name(net) != text:  # type: ignore[arg-type]
        raise bad(
            f"{tool}: the point is already on net {net}",
            f"a label {text} would give the net a second name",
            f"Rename the net with sch_rename_net(old='{base_name(net)}', new='{text}').",  # type: ignore[arg-type]
        )
    group: set[PinKey] = set()
    for n in {*(found), *same_name_nets(world, text, kind, sheet)}:
        members = sorted(world.members(n))
        if members:
            group.add(members[0])
    notes = (
        []
        if same_point(at, raw, 1e-4)
        else [f"position snapped to the grid: {fmt(raw)} -> {fmt(at)}"]
    )
    item = make_label(text, at, d, kind)
    return ChangeSet(
        tool=tool,
        sheet=sheet,
        stages=[Stage(f"label {text}", lambda _inp: [Create([item], sheet=sheet)])],
        pre_images={},
        intent=NetIntent(merge=[frozenset(group)] if len(group) > 1 else []),
        logical=Logical(1),
        summary={"net": text, "kind": kind, "labels": [{"at_mm": list(at), "touches": what}]},
        notes=notes,
    )


def power_change_set(
    world: World, tool: str, pins: list[PinEnd], sym: PowerSymbol, net: str, block: Any = 100
) -> ChangeSet:
    """A power symbol (+ stub) per pin: stage 1 wires + placements, stage 2 re-sends the
    placed symbols (reference, value) and adds their pins to the intent."""
    from copperchat.kicad_tools.edit.annotate import Allocator

    if not sym.flag:
        pending = [p for p in pins if world.net(p) != net]
        check_named_conflict(world, pending, net, tool)
    alloc = Allocator.from_design(world.h, block)
    plans: list[PowerPlan] = []
    notes: list[str] = []
    for p in pins:
        if not sym.flag and world.net(p) == net:
            notes.append(f"{p.label} is already on {net}; nothing added there")
            continue
        allowed = {n for n in (world.net(p),) if n is not None}
        if not sym.flag:
            allowed.add(net)
        prefix: Literal["#PWR", "#FLG"] = "#FLG" if sym.flag else "#PWR"
        plan = plan_power(world, p, sym, allowed, "")
        if plan is None:
            raise no_room(tool, p, f"a {sym.lib_id} symbol", len(plans))
        plan.reference = alloc.next_power(prefix)
        plans.append(plan)
        if plan.note:
            notes.append(plan.note)
    first = world.sheet_of(pins[0])
    summary: dict[str, Any] = {
        "net": net,
        "symbol": sym.lib_id,
        "placed": [
            {
                "pin": pl.pin.label,
                "reference": pl.reference,
                "at_mm": list(pl.at),
                "rotation": pl.rotation,
                "stub_mm": [list(q) for q in pl.stub],
            }
            for pl in plans
        ],
    }
    if not plans:
        return no_change(tool, first, summary, notes)
    by_sheet: dict[str, tuple[SheetRef, list[Any]]] = {}
    for pl in plans:
        _collect(by_sheet, pl.sheet, pl.wires)
    stage1: list[Any] = _creates(by_sheet)
    stage1 += [
        Place(sym.lib_id, pl.at, pl.rotation, 1, pl.reference, sheet=pl.sheet) for pl in plans
    ]
    intent = NetIntent()
    joined_at: dict[int, int] = {}  # plan index -> its joined group (the power pin joins it)
    for i, pl in enumerate(plans):  # a sheet pin's stub: the pin joins the rail through it
        stub = [(pl.sheet, w) for w in pl.wires if type(w).__name__ == "SchematicLine"]
        for pin_group in sheet_pin_joined(world, [pl.pin], stub, one_net=False):
            joined_at[i] = len(intent.joined)
            intent.joined.append(pin_group)
    # (plan index, instance path) -> merge group index; a rail is one global group
    slot: dict[tuple[int, str], int] = {}
    if not sym.flag:
        group: set[PinKey] = set(sorted(world.members(net))[:1])
        for pl in plans:
            for keys in world.replicate([pl.pin.key], pl.sheet):
                group |= keys
                intent.names.update({k: net for k in keys})
        intent.merge = [frozenset(group)]
    else:
        for i, pl in enumerate(plans):
            for inst, keys in zip(
                world.instances(pl.sheet), world.replicate([pl.pin.key], pl.sheet), strict=True
            ):
                slot[(i, inst.path)] = len(intent.merge)
                intent.merge.append(keys)  # empty for an instance without the pin
    cs = ChangeSet(
        tool=tool,
        sheet=first,
        stages=[],
        pre_images={},
        intent=intent,
        logical=Logical(len(plans)),
        summary=summary,
        notes=notes,
    )

    def resend(inp: StageInput) -> list[Any]:
        from copperchat.kicad_tools.access import kiid, sheet_path_key
        from copperchat.kicad_tools.edit.geometry import pin_ends

        ops: list[Any] = []
        placed = list(inp.placed)
        if len(placed) != len(plans):
            raise StructuredToolError(
                f"{tool}: KiCad placed {len(placed)} of {len(plans)} power symbols",
                "the read-back after the placement is incomplete",
                "Nothing was kept; retry.",
                code="verification_failed",
            )
        groups = list(intent.merge)
        for i, (pl, placed_sym) in enumerate(zip(plans, placed, strict=True)):
            placed_sym.reference = pl.reference
            if placed_sym.value != sym.value or (not sym.flag and placed_sym.value != net):
                placed_sym.value = net if not sym.flag else sym.value
            ops.append(Update([placed_sym], sheet=pl.sheet))
            placed_path = sheet_path_key(placed_sym.path)
            own = [(placed_path, kiid(placed_sym), pe.number) for pe in pin_ends(placed_sym)]
            if i in joined_at:  # the stub must reach the placed power pin, too
                intent.joined[joined_at[i]] = intent.joined[joined_at[i]] | frozenset(own)
            for inst, keys in zip(
                world.instances(pl.sheet), world.replicate(own, pl.sheet), strict=True
            ):
                idx = 0 if not sym.flag else slot[(i, inst.path)]
                groups[idx] = groups[idx] | keys
        intent.merge = [g for g in groups if g]  # verification runs after the last stage
        return ops

    cs.stages = [
        Stage(f"place {sym.lib_id}", lambda _inp: list(stage1)),
        Stage("power references", resend),
    ]
    return cs


def no_connect_change_set(world: World, tool: str, pins: list[PinEnd]) -> ChangeSet:
    notes: list[str] = []
    todo: list[PinEnd] = []
    for p in pins:
        state = no_connect_state(world, p)
        if state == "connected":
            here = world.scene(world.sheet_of(p)).items_at(p.at)
            raise bad(
                f"{tool}: {p.label} is connected",
                f"a no-connect marker belongs on a free pin; {p.label} is on net "
                f"{world.net(p)} ({len(here) - 1} item(s) at its end)",
                "Remove the connection first (sch_delete) or pick a free pin.",
                pin=p.label,
            )
        if state == "marked":
            notes.append(f"{p.label} already has a no-connect marker")
            continue
        todo.append(p)
    first = world.sheet_of(pins[0])
    summary = {"pins": [p.label for p in todo]}
    if not todo:
        return no_change(tool, first, summary, notes)
    by_sheet: dict[str, tuple[SheetRef, list[Any]]] = {}
    for p in todo:
        _collect(by_sheet, world.sheet_of(p), [make_no_connect(p.at)])
    ops = _creates(by_sheet)
    return ChangeSet(
        tool=tool,
        sheet=first,
        stages=[Stage("no-connect", lambda _inp: list(ops))],
        pre_images={},
        intent=NetIntent(),
        logical=Logical(len(todo)),
        summary=summary,
        notes=notes,
    )


def pins_arg(world: World, raw: Any, tool: str, name: str = "at") -> list[PinEnd]:
    specs = [raw] if isinstance(raw, str) else raw
    if not isinstance(specs, list) or not specs or len(specs) > 64:
        raise bad(
            f"{tool}: {name} needs 1 to 64 pins",
            f"{name} was {short(raw)}",
            f"Pass {name}=['U1.3', 'R1.2', ...].",
        )
    pins = [world.pin(s) for s in specs]
    seen: set[tuple[str, str, str]] = set()
    for p in pins:
        same = (world.sheet_of(p).file, p.key[1], p.key[2])  # one pin of the sheet file
        if same in seen:
            raise bad(
                f"{tool}: {p.label} is listed twice",
                "each pin may appear once (also not once per instance of a shared sheet: "
                "both instances show the same items)",
                "Remove the duplicate.",
            )
        seen.add(same)
    return pins
