"""Spike 2D environment: blank KiCad-made project, library loading, symbol search index.

Reuses spike 2A (`harness.py`: Server, kipy helpers) and spike 2B (`libdata.py`, `index.py`).
Everything comes from the KiCad API; no KiCad file is written or parsed by this code.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from harness import Server
from kipy.proto.common.types import DocumentType, LibraryType

HERE = Path(__file__).parent
WORK = HERE / "work"  # gitignored: blank template, per-run projects, caches
BLANK = WORK / "blank-template"
LIB_CACHE = WORK / "symbols.json"  # cache of the API detail fetch (6.5 s)


def make_blank_template() -> None:
    """An empty project + schematic created by KiCad itself (CreateDocument + save)."""
    if (BLANK / "blank.kicad_sch").exists():
        return
    tmp = Path(tempfile.mkdtemp(prefix="s2d-blank-")) / "blank"
    tmp.mkdir(parents=True)
    with Server(None) as srv:
        k = srv.client(timeout_ms=60000)
        k.create_document(str(tmp / "blank.kicad_sch"), DocumentType.DOCTYPE_SCHEMATIC)
        k.get_schematic().save()
    BLANK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tmp, BLANK, ignore=shutil.ignore_patterns("*.lck"))


def fresh_blank(dest: Path | None = None) -> Path:
    make_blank_template()
    dst = dest or Path(tempfile.mkdtemp(prefix="s2d-run-")) / "blank"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(BLANK, dst)
    return dst


def load_libraries(k, timeout_s: float = 300.0) -> float:
    """Symbols + footprints (footprint names are used to validate footprint fields)."""
    t0 = time.monotonic()
    k.load_all_libraries([LibraryType.LT_SYMBOL, LibraryType.LT_FOOTPRINT])
    k.wait_for_libraries(timeout_s=timeout_s)
    return time.monotonic() - t0


class Library:
    """Symbol index (2B prototype) + footprint name set."""

    def __init__(self, k) -> None:
        from index import Index
        from libdata import Pin, SymbolRec, fetch_symbols

        t0 = time.monotonic()
        if LIB_CACHE.exists():
            raw = json.loads(LIB_CACHE.read_text())
            self.symbols = {
                k_: SymbolRec(**{**v, "pins": None if v["pins"] is None else [Pin(**p) for p in v["pins"]]}) for k_, v in raw.items()
            }
        else:
            ids = list(k.get_library_symbols())
            self.symbols = fetch_symbols(k, ids)
            LIB_CACHE.parent.mkdir(parents=True, exist_ok=True)
            LIB_CACHE.write_text(json.dumps({k_: asdict(v) for k_, v in self.symbols.items()}))
        self.index = Index()
        for rec in self.symbols.values():
            self.index.add(rec.lib_id, rec.description, rec.keywords)
        self.index.finish()
        self.footprints = {f"{lid.library}:{lid.name}" for lid in k.get_library_footprints()}
        self.build_s = round(time.monotonic() - t0, 2)

    def search(self, query: str, library: str | None, limit: int) -> list[dict]:
        out = []
        for lib_id, _score in self.index.search(query, limit=limit, library=library):
            r = self.symbols[lib_id]
            out.append(
                {
                    "lib_id": lib_id,
                    "description": r.description,
                    "keywords": r.keywords,
                    "unit_count": r.unit_count,
                    "pin_count": r.pin_count,
                    "default_footprint": r.footprint or None,
                    "footprint_filters": r.footprint_filters[:4],
                    "power": r.power,
                }
            )
        return out

    def search_footprints(self, query: str, limit: int) -> list[str]:
        terms = [t for t in re.split(r"[\s_:]+", query.lower()) if t]
        hits = [f for f in self.footprints if all(t in f.lower() for t in terms)]
        hits.sort(key=lambda f: (len(f), f))
        return hits[:limit]
