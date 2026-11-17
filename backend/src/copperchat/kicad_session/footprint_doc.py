"""The api-server's single footprint document (DOCTYPE_FOOTPRINT), over raw protobuf.

kipy's `Board` wrapper cannot be reused for a footprint document (spike 3A, Q6): its
`get_items()` asserts every item is a `BoardItem`, but footprint fields come back as
`kiapi.board.types.Field`, and `save_as` resolves its path on the filesystem while the
footprint handler expects `"Lib:Name"`. Ported from `spikes/3a-footprints/fpdoc.py`.

Every method runs through `KicadSession.call`; obtain an instance only from
`KicadSession.footprint_document`, which holds the session's footprint lock and closes
the document afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.protobuf.any_pb2 import Any as PbAny  # type: ignore[import-untyped]
from google.protobuf.empty_pb2 import Empty  # type: ignore[import-untyped]
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.common.commands import project_commands_pb2 as pc
from kipy.proto.common.types import DocumentType
from kipy.proto.common.types import base_types_pb2 as base
from kipy.util.proto import unpack_any

if TYPE_CHECKING:
    from copperchat.kicad_session.session import KicadHandle, KicadSession

# The stock footprint opened to load the footprint libraries (owner decision, spec §5).
SEED_LIB_ID = "Inductor_SMD:L_Bourns-SRN4018"
# The first footprint document of a server loads every footprint library in the tables
# (spike 3A, Q5: ~3 s, +1.7 GB RSS) - much longer than an ordinary API call.
OPEN_TIMEOUT_S = 300.0


FOOTPRINT_DOCUMENT = int(DocumentType.DOCTYPE_FOOTPRINT)


def open_footprint(h: KicadHandle, lib_id: str) -> Any:
    """Open `lib_id` ("Lib:Name") as the server's footprint document; returns its
    DocumentSpecifier."""
    return h.kicad.open_document(lib_id, FOOTPRINT_DOCUMENT)


def close_footprint(h: KicadHandle, document: Any) -> None:
    h.kicad.close_document(document)


def _pack(items: list[Any]) -> list[Any]:
    out = []
    for item in items:
        packed = PbAny()
        packed.Pack(item)
        out.append(packed)
    return out


def _check_results(response: Any, results: Any, what: str) -> list[Any]:
    bad = [
        f"{r.status.error_message} ({ec.ItemStatusCode.Name(r.status.code)})"
        for r in results
        if r.status.code != ec.ItemStatusCode.ISC_OK
    ]
    if response.status != base.ItemRequestStatus.IRS_OK or bad:
        status = base.ItemRequestStatus.Name(response.status)
        raise RuntimeError(f"footprint {what} failed: {status} {bad}")
    return [unpack_any(r.item) for r in results]


class FootprintDocument:
    """One open footprint document. Writes stage into the open commit (`begin` /
    `push` / `drop`); `save` writes the library file through KiCad."""

    def __init__(self, session: KicadSession, document: Any, lib_id: str) -> None:
        self._session = session
        self.document = document  # kiapi DocumentSpecifier
        self.lib_id = lib_id
        self._commit: Any = None  # CommitId of the open commit

    @property
    def open_commit(self) -> bool:
        """A commit was begun and neither pushed nor dropped yet."""
        return self._commit is not None

    async def _send(self, build: Any, response_type: Any) -> Any:
        """Send the command `build(document)` returns, expecting `response_type`."""

        def _run(h: KicadHandle) -> Any:
            return h.kicad._client.send(build(self.document), response_type)

        return await self._session.call(_run)

    # -- read ------------------------------------------------------------------------
    async def items(self) -> list[Any]:
        """Every item of the footprint (pads, shapes, texts, fields, zones); not the
        footprint itself."""

        def build(doc: Any) -> Any:
            cmd = ec.GetItems()
            cmd.header.document.CopyFrom(doc)
            return cmd

        response = await self._send(build, ec.GetItemsResponse)
        return [unpack_any(a) for a in response.items]

    async def footprint(self) -> Any:
        """The `FootprintInstance` (definition, attributes, overrides): `GetItems` lists
        only its children, so it is fetched by the id their `parent` names."""

        children = await self.items()
        parent = next((i.parent.value for i in children if i.HasField("parent")), None)
        if not parent:
            raise RuntimeError(f"footprint document {self.lib_id} names no parent footprint")

        def build(doc: Any) -> Any:
            cmd = ec.GetItemsById()
            cmd.header.document.CopyFrom(doc)
            cmd.items.append(base.KIID(value=parent))
            return cmd

        response = await self._send(build, ec.GetItemsResponse)
        found = [unpack_any(a) for a in response.items]
        if len(found) != 1 or not isinstance(found[0], bt.FootprintInstance):
            raise RuntimeError(f"footprint document {self.lib_id}: no FootprintInstance")
        return found[0]

    async def fields(self) -> list[Any]:
        return [i for i in await self.items() if i.DESCRIPTOR.name == "Field"]

    # -- commits ---------------------------------------------------------------------
    async def begin(self) -> None:

        if self._commit is not None:
            raise RuntimeError("a footprint commit is already open")

        def build(doc: Any) -> Any:
            cmd = ec.BeginCommit()
            cmd.header.document.CopyFrom(doc)
            return cmd

        response = await self._send(build, ec.BeginCommitResponse)
        self._commit = response.id

    async def _end(self, *, drop: bool, label: str = "") -> None:

        commit = self._commit
        if commit is None:
            raise RuntimeError("no footprint commit is open")

        def build(doc: Any) -> Any:
            cmd = ec.EndCommit()
            cmd.header.document.CopyFrom(doc)
            cmd.id.CopyFrom(commit)
            cmd.action = ec.CommitAction.CMA_DROP if drop else ec.CommitAction.CMA_COMMIT
            cmd.message = label
            return cmd

        self._commit = None
        await self._send(build, ec.EndCommitResponse)

    async def push(self, label: str) -> None:
        await self._end(drop=False, label=label)

    async def drop(self) -> None:
        await self._end(drop=True)

    # -- write -----------------------------------------------------------------------
    async def create(self, items: list[Any]) -> list[Any]:

        def build(doc: Any) -> Any:
            cmd = ec.CreateItems()
            cmd.header.document.CopyFrom(doc)
            cmd.items.extend(_pack(items))
            return cmd

        response = await self._send(build, ec.CreateItemsResponse)
        return _check_results(response, response.created_items, "create")

    async def update(self, items: list[Any]) -> list[Any]:

        def build(doc: Any) -> Any:
            cmd = ec.UpdateItems()
            cmd.header.document.CopyFrom(doc)
            cmd.items.extend(_pack(items))
            return cmd

        response = await self._send(build, ec.UpdateItemsResponse)
        return _check_results(response, response.updated_items, "update")

    async def delete(self, ids: list[str]) -> None:

        def build(doc: Any) -> Any:
            cmd = ec.DeleteItems()
            cmd.header.document.CopyFrom(doc)
            cmd.item_ids.extend(base.KIID(value=i) for i in ids)
            return cmd

        response = await self._send(build, ec.DeleteItemsResponse)
        bad = [
            f"{r.id.value}: {ec.ItemDeletionStatus.Name(r.status)}"
            for r in response.deleted_items
            if r.status != ec.ItemDeletionStatus.IDS_OK
        ]
        if bad:
            raise RuntimeError(f"footprint delete failed: {bad}")

    async def update_footprint(self, props: Any) -> None:
        """Write footprint-level properties: `props` is the `FootprintInstance` from
        `footprint()`, modified (description, keywords, attributes, overrides, 3D models;
        KiCad patch 0002)."""
        await self.update([props])

    async def save(self) -> None:

        def build(doc: Any) -> Any:
            cmd = pc.SaveDocument()
            cmd.document.CopyFrom(doc)
            return cmd

        await self._send(build, Empty)

    async def save_copy(self, target_lib_id: str) -> None:
        """SaveCopyOfDocument: `target_lib_id` is "Lib:Name" (an existing name is
        overwritten silently, spike 3A Q2)."""

        def build(doc: Any) -> Any:
            cmd = ec.SaveCopyOfDocument()
            cmd.document.CopyFrom(doc)
            cmd.path = target_lib_id
            return cmd

        await self._send(build, Empty)
