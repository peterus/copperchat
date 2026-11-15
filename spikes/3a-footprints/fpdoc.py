"""Raw-protobuf client for the headless footprint document (DOCTYPE_FOOTPRINT).

kipy's `Board` wrapper cannot be reused for a footprint document: `Board.get_items()`
asserts every item is a `BoardItem`, but footprint fields come back as
`kiapi.board.types.Field` (a plain `Wrapper`), and `EditorCommandsHandler.save_as` resolves
its `path` to an absolute filesystem path, while the footprint handler expects `"lib:Name"`.
So this prototype talks protobuf directly (it is the shape a CopperChat `FootprintDocument`
helper would take)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from google.protobuf.any_pb2 import Any as PbAny
from google.protobuf.empty_pb2 import Empty
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.commands import editor_commands_pb2 as ec, project_commands_pb2 as pc
from kipy.proto.common.types import DocumentType, base_types_pb2 as base
from kipy.util.proto import unpack_any

NM = 1_000_000  # nm per mm


def mm(v: float) -> int:
    return round(v * NM)


@dataclass
class Timing:
    calls: dict[str, list[float]] = field(default_factory=dict)

    def add(self, name: str, dt: float) -> None:
        self.calls.setdefault(name, []).append(dt)


class FootprintDoc:
    def __init__(self, kicad, lib_id: str, timing: Timing | None = None):
        """Opens `lib_id` ("Lib:Name") as the server's single footprint document."""
        self.k = kicad
        self.c = kicad._client
        self.timing = timing or Timing()
        t0 = time.monotonic()
        self.doc = kicad.open_document(lib_id, DocumentType.DOCTYPE_FOOTPRINT)
        self.timing.add("open", time.monotonic() - t0)

    def _send(self, name, cmd, resp):
        t0 = time.monotonic()
        try:
            return self.c.send(cmd, resp)
        finally:
            self.timing.add(name, time.monotonic() - t0)

    def close(self) -> None:
        self.k.close_document(self.doc)

    # -- read ----------------------------------------------------------------------------
    def items(self, types: list[int] | None = None) -> list:
        cmd = ec.GetItems()
        cmd.header.document.CopyFrom(self.doc)
        cmd.types.extend(types or [])
        return [unpack_any(a) for a in self._send("get_items", cmd, ec.GetItemsResponse).items]

    def items_by_id(self, ids: list[str]) -> list:
        cmd = ec.GetItemsById()
        cmd.header.document.CopyFrom(self.doc)
        for i in ids:
            cmd.items.append(base.KIID(value=i))
        return [unpack_any(a) for a in self._send("get_items_by_id", cmd, ec.GetItemsResponse).items]

    # -- write ---------------------------------------------------------------------------
    @staticmethod
    def _statuses(results) -> list[str]:
        return [
            f"{r.status.error_message} ({ec.ItemStatusCode.Name(r.status.code)})"
            for r in results
            if r.status.code != ec.ItemStatusCode.ISC_OK
        ]

    def create(self, protos: list) -> list:
        cmd = ec.CreateItems()
        cmd.header.document.CopyFrom(self.doc)
        for p in protos:
            a = PbAny()
            a.Pack(p)
            cmd.items.append(a)
        r = self._send("create", cmd, ec.CreateItemsResponse)
        bad = self._statuses(r.created_items)
        if r.status != base.ItemRequestStatus.IRS_OK or bad:
            raise RuntimeError(f"create failed: {base.ItemRequestStatus.Name(r.status)} {bad}")
        return [unpack_any(x.item) for x in r.created_items]

    def update(self, protos: list) -> list:
        cmd = ec.UpdateItems()
        cmd.header.document.CopyFrom(self.doc)
        for p in protos:
            a = PbAny()
            a.Pack(p)
            cmd.items.append(a)
        r = self._send("update", cmd, ec.UpdateItemsResponse)
        bad = self._statuses(r.updated_items)
        if r.status != base.ItemRequestStatus.IRS_OK or bad:
            raise RuntimeError(f"update failed: {base.ItemRequestStatus.Name(r.status)} {bad}")
        return [unpack_any(x.item) for x in r.updated_items]

    def delete(self, ids: list[str]) -> dict[str, str]:
        cmd = ec.DeleteItems()
        cmd.header.document.CopyFrom(self.doc)
        for i in ids:
            cmd.item_ids.append(base.KIID(value=i))
        r = self._send("delete", cmd, ec.DeleteItemsResponse)
        return {x.id.value: ec.ItemDeletionStatus.Name(x.status) for x in r.deleted_items}

    def begin(self) -> str:
        cmd = ec.BeginCommit()
        cmd.header.document.CopyFrom(self.doc)
        return self._send("begin", cmd, ec.BeginCommitResponse).id.value

    def end(self, commit_id: str, drop: bool = False, message: str = "") -> None:
        cmd = ec.EndCommit()
        cmd.id.value = commit_id
        cmd.action = ec.CommitAction.CMA_DROP if drop else ec.CommitAction.CMA_COMMIT
        cmd.message = message
        cmd.header.document.CopyFrom(self.doc)
        self._send("end", cmd, ec.EndCommitResponse)

    def save(self) -> None:
        cmd = pc.SaveDocument()
        cmd.document.CopyFrom(self.doc)
        self._send("save", cmd, Empty)

    def save_copy(self, target: str) -> None:
        """`target` = "Name" (same library) or "Lib:Name"."""
        cmd = ec.SaveCopyOfDocument()
        cmd.document.CopyFrom(self.doc)
        cmd.path = target
        self._send("save_copy", cmd, Empty)

    def modified(self) -> bool:
        cmd = ec.GetDocumentModifiedState()
        cmd.document.CopyFrom(self.doc)
        return self._send("modified", cmd, ec.GetDocumentModifiedStateResponse).state


def kind(p) -> str:
    return p.DESCRIPTOR.name


def layer_name(v: int) -> str:
    return bt.BoardLayer.Name(v)
