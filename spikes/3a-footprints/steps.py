"""Reusable spike steps (no checks of their own)."""

from __future__ import annotations

from kipy.proto.common.types import DocumentType

import l201
from fpdoc import FootprintDoc, Timing
from harness import Recorder
from repo import LIB

SEED = "Inductor_SMD:L_Bourns-SRN4018"


def open_sch(k, pro) -> None:
    k.open_document(str(pro.with_suffix(".kicad_sch")), DocumentType.DOCTYPE_SCHEMATIC)


def create_l201(k, name: str = l201.NAME, timing: Timing | None = None) -> None:
    """Seed -> SaveCopy -> close seed -> open copy -> build (q01_create.build) -> save -> close."""
    from q01_create import build

    seed = FootprintDoc(k, SEED, timing)
    seed.save_copy(f"{LIB}:{name}")
    seed.close()
    fp = FootprintDoc(k, f"{LIB}:{name}", timing)
    build(fp, Recorder("scratch"))
    fp.save()
    fp.close()
