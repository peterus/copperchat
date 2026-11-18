#!/usr/bin/env python3
"""Records KiCad API output for stock footprints as unit-test fixtures (never by hand).

Runs inside the patched KiCad base image (kicad-cli api-server), from `backend/`:
    uv run python scripts/record-fp-fixtures.py [DEST_DIR]

For each footprint it sends one `GetItemsFromLibrary` (type `LT_FOOTPRINT`, one item id) -
the batched library read `LibraryService` uses (`library.service._get_items`) - and writes
`GetItemsResponse.SerializeToString()` to `<DEST_DIR>/<name>.pb`. `items[0]` of that
response is a packed `kiapi.board.types.Footprint` (definition, fields and `items`).

It also opens each footprint as the server's footprint document and writes the
`GetItemsById(<footprint id>)` response to `<DEST_DIR>/<name>.instance.pb`: one packed
`kiapi.board.types.FootprintInstance` (as `FootprintDocument.footprint()` reads it). The
library read omits what `FOOTPRINT::SerializeDefinition` does not pack - the mandatory
fields (Reference, Value, Datasheet, Description), the attributes (mounting, exclude
flags, DNP) and the overrides; the instance carries them.

These files are protobuf API output, not KiCad files.

A boardless api-server serves footprint libraries only after its first footprint document
(spike 3A, Q5), so the seed footprint is opened and closed first, as
`KicadSession.activate_footprints` does.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kipy import KiCad
from kipy.proto.common.commands import editor_commands_pb2, library_commands_pb2
from kipy.proto.common.types import DocumentType, LibraryType
from kipy.proto.common.types import base_types_pb2 as base
from kipy.util.proto import unpack_any

from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_session.launcher import kicad_cli_env

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "tests/fixtures/footprints"

# fixture name -> stock lib id
FOOTPRINTS = {
    "seed_srn4018": SEED_LIB_ID,  # 2 SMD rect pads, 3D model
    "pinheader_1x02": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",  # THT
    "qfn16_ep": "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm",  # roundrect + EP
    # custom-shape pads (`(primitives` in the .kicad_mod)
    "solderjumper_triangle": "Jumper:SolderJumper-2_P1.3mm_Open_TrianglePad1.0x1.5mm",
}


def _wait_for_server(proc: subprocess.Popen[bytes], url: str, timeout_s: float) -> KiCad:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"api-server exited with code {proc.returncode}")
        try:
            client = KiCad(socket_path=url, client_name="record-fp", timeout_ms=300_000)
            client.ping()
            return client
        except Exception:  # not ready yet
            time.sleep(0.2)
    raise RuntimeError(f"api-server not ready after {timeout_s:.0f}s")


def _library_response(kicad: KiCad, lib_id: str) -> bytes:
    nickname, _, name = lib_id.partition(":")
    command = library_commands_pb2.GetItemsFromLibrary()
    command.type = LibraryType.LT_FOOTPRINT
    command.item_ids.add(library_nickname=nickname, entry_name=name)
    response = kicad._client.send(command, editor_commands_pb2.GetItemsResponse)
    if len(response.items) != 1:
        raise RuntimeError(f"{lib_id}: expected 1 item, got {len(response.items)}")
    return bytes(response.SerializeToString())


def _instance_response(kicad: KiCad, lib_id: str) -> bytes:
    doc = kicad.open_document(lib_id, DocumentType.DOCTYPE_FOOTPRINT)
    try:
        children = editor_commands_pb2.GetItems()
        children.header.document.CopyFrom(doc)
        items = kicad._client.send(children, editor_commands_pb2.GetItemsResponse).items
        unpacked = [unpack_any(a) for a in items]
        parents = {i.parent.value for i in unpacked if "parent" in i.DESCRIPTOR.fields_by_name}
        if len(parents) != 1:
            raise RuntimeError(f"{lib_id}: expected one parent footprint, got {parents}")
        by_id = editor_commands_pb2.GetItemsById()
        by_id.header.document.CopyFrom(doc)
        by_id.items.append(base.KIID(value=parents.pop()))
        response = kicad._client.send(by_id, editor_commands_pb2.GetItemsResponse)
        if len(response.items) != 1 or not response.items[0].type_url.endswith("FootprintInstance"):
            raise RuntimeError(f"{lib_id}: no FootprintInstance")
        return bytes(response.SerializeToString())
    finally:
        kicad.close_document(doc)


def record(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="cc-fp-") as tmp:
        socket = Path(tmp) / "api.sock"
        url = f"ipc://{socket}"
        env = kicad_cli_env("kicad-cli", Path(tmp) / "config")
        proc = subprocess.Popen(
            ["kicad-cli", "api-server", "--socket", str(socket)],
            cwd=tmp,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            kicad = _wait_for_server(proc, url, 60)
            seed = kicad.open_document(SEED_LIB_ID, DocumentType.DOCTYPE_FOOTPRINT)
            kicad.close_document(seed)
            for fixture, lib_id in FOOTPRINTS.items():
                path = dest / f"{fixture}.pb"
                path.write_bytes(_library_response(kicad, lib_id))
                instance = dest / f"{fixture}.instance.pb"
                instance.write_bytes(_instance_response(kicad, lib_id))
                written += [path, instance]
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return written


if __name__ == "__main__":
    for p in record(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST):
        print(f"{p.name}: {p.stat().st_size} bytes")
