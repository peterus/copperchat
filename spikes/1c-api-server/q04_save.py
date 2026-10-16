"""Q4: SaveDocument round-trip; SaveCopyOfDocument to another dir incl. sub-sheets."""

import hashlib
import shutil
import tempfile
from pathlib import Path

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad, run_cli


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> int:
    require_kicad()
    from kipy.proto.common.types import DocumentType

    rec = Recorder("q04_save")
    proj = fresh_fixture("complex_hierarchy")
    sch_files = sorted(proj.glob("*.kicad_sch"))
    root = proj / f"{next(proj.glob('*.kicad_pro')).stem}.kicad_sch"
    subs = [p.name for p in sch_files if p != root]
    before = {p.name: (p.stat().st_mtime_ns, sha(p)) for p in proj.iterdir() if p.is_file()}
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        sch = k.get_schematic()
        sch.save()
        rec.check("sch_save_ok", True, "no exception")
        k.get_board().save()
        rec.check("pcb_save_ok", True, "no exception")
        after = {p.name: (p.stat().st_mtime_ns, sha(p)) for p in proj.iterdir() if p.is_file()}
        rec.note(
            "save_touched_mtime", sorted(n for n in after if before.get(n, (0,))[0] != after[n][0])
        )
        rec.note(
            "save_changed_content",
            sorted(n for n in after if before.get(n, (0, ""))[1] != after[n][1]),
        )
        saved_hashes = {p.name: sha(p) for p in sch_files}

        dest = Path(tempfile.mkdtemp(prefix="c1-copy-"))
        sch.save_as(str(dest / root.name), overwrite=True, include_project=False)
        copied = sorted(p.name for p in dest.iterdir())
        rec.note("copy_dir_listing", copied)
        rec.check("copy_root_written", (dest / root.name).exists(), copied)
        rec.check(
            "copy_subsheets_written",
            all((dest / s).exists() for s in subs),
            f"expected {subs}, got {copied}",
        )
        k.get_board().save_as(
            str(dest / f"{root.stem}.kicad_pcb"), overwrite=True, include_project=False
        )
        copied = sorted(p.name for p in dest.iterdir())
        rec.check("pcb_copy_written", (dest / f"{root.stem}.kicad_pcb").exists(), copied)
        # copy must not change the open document's path or the originals
        rec.check(
            "originals_unchanged",
            {p.name: sha(p) for p in sch_files} == saved_hashes,
            "hash compare vs. right after save()",
        )
        docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        rec.check("open_doc_still_original", str(dest) not in str(docs[0]), str(docs[0])[:300])
        rec.note("copy_root_equals_saved_root", sha(dest / root.name) == saved_hashes[root.name])
        # the copy is a valid KiCad file (kicad-cli does not fail on a missing sub-sheet
        # file: it plots that sheet empty, so record the SVG sizes as well)
        r = run_cli("sch", "export", "svg", "-o", str(dest / "svg"), str(dest / root.name))
        rec.check("copy_loadable_by_cli", r.returncode == 0, (r.stdout + r.stderr)[-300:])
        sizes = {p.name: p.stat().st_size for p in (dest / "svg").glob("*.svg")}
        rec.note("copy_only_svg_sizes", sizes)
        if not all((dest / s).exists() for s in subs):
            # Workaround: copy the sub-sheet files KiCad itself just saved (copy, not edit).
            for s in subs:
                shutil.copy2(proj / s, dest / s)
            r = run_cli("sch", "export", "svg", "-o", str(dest / "svg2"), str(dest / root.name))
            sizes2 = {p.name: p.stat().st_size for p in (dest / "svg2").glob("*.svg")}
            grew = [
                n for n in sizes2 if n != f"{root.stem}.svg" and sizes2[n] > 2 * sizes.get(n, 0)
            ]
            rec.check(
                "workaround_copy_saved_subsheets",
                r.returncode == 0 and len(grew) == len(sizes2) - 1 >= 1,
                f"rc={r.returncode} sizes={sizes2} (copy-only: {sizes})",
            )

    # Snapshot without save(): the foundation agent never edits, so the files on disk are
    # the current state. Copy the root via the API and byte-copy the sub-sheet files named
    # by get_hierarchy(); the user's project must stay byte-identical.
    proj = fresh_fixture("complex_hierarchy")
    orig = {p.name: sha(p) for p in proj.iterdir() if p.is_file() and not p.name.endswith(".lck")}
    with Server(proj) as srv:
        sch = srv.client(timeout_ms=60000).get_schematic()
        files = [s.filename for s in sch.get_hierarchy()]
        rec.note("hierarchy_filenames", files)
        dest = Path(tempfile.mkdtemp(prefix="c1-copy-nosave-"))
        sch.save_as(str(dest / root.name), overwrite=True, include_project=False)
        for name in sorted(set(files) - {root.name}):
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(proj / name, dest / name)
        now = {
            p.name: sha(p) for p in proj.iterdir() if p.is_file() and not p.name.endswith(".lck")
        }
        rec.check(
            "nosave_originals_untouched",
            now == orig,
            sorted(n for n in orig if orig[n] != now.get(n)),
        )
        r = run_cli("sch", "export", "svg", "-o", str(dest / "svg"), str(dest / root.name))
        sizes3 = {p.name: p.stat().st_size for p in (dest / "svg").glob("*.svg")}
        rec.check(
            "nosave_copy_complete",
            r.returncode == 0 and len(sizes3) >= 3 and min(sizes3.values()) > 200_000,
            f"rc={r.returncode} sizes={sizes3}",
        )
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
