"""Part preferences (spec §9.1) and the part-notes check (spec §9).

The layered skill `part-preferences` (built-in template -> user -> project) carries a
fenced ```` ```preferences ```` block of `key: value` lines; the most specific layer
wins per key and an empty value leaves the key to the less specific layer. The owner's
user-level default (passives in 0805) is seeded once on the first start.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from copperchat.config import Settings
from copperchat.fsutil import atomic_write_text
from copperchat.library.packages import IMPERIAL_TO_METRIC, FootprintDetail, package_of
from copperchat.parts import InvalidMpn, read_notes
from copperchat.projects import Project
from copperchat.skills.store import SkillLayers, customise, layer_contents, write_user_skill

log = logging.getLogger(__name__)

PREFERENCES_SKILL = "part-preferences"
SEED_MARKER = ".part-preferences-seeded"
DEFAULT_PASSIVES_PACKAGE = "0805"  # owner decision (spec §9.1)
_BLOCK_RE = re.compile(r"^```preferences[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL)
_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)[ \t]*:(.*)$")
_PACKAGE_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")
_LIST_KEYS = ("preferred_libraries", "preferred_manufacturers")
# Device:R / C / L in their plain, small and US variants, plus every ferrite bead.
_PASSIVE_RE = re.compile(r"^Device:(?:(?:R|C|L)(?:_Small)?(?:_US)?|FerriteBead.*)$")
_ACTIVE_PREFIXES = frozenset({"U", "Q", "IC"})
_MPN_FIELDS = ("mpn", "manufacturer part number", "manufacturer_part_number", "mfr part number")


@dataclass(frozen=True)
class PartPreferences:
    passives_package: str | None
    preferred_libraries: list[str]
    raw: dict[str, str]
    preferred_manufacturers: list[str] = field(default_factory=list)
    assembly: str | None = None


def parse_preferences(text: str) -> dict[str, str]:
    """Non-empty `key: value` pairs of the first ```preferences block (keys lower-case)."""
    m = _BLOCK_RE.search(text)
    if m is None:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        lm = _LINE_RE.match(line.strip())
        if lm is None:
            continue
        value = lm.group(2).strip()
        if value:
            out[lm.group(1).lower()] = value
    return out


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def preferences_from_raw(raw: dict[str, str]) -> PartPreferences:
    m = _PACKAGE_RE.search(raw.get("passives_package", ""))
    package = m.group(1) if m else None
    if package is not None and package not in IMPERIAL_TO_METRIC:
        log.warning("part preferences: ignoring unknown passives_package %r", package)
        package = None
    return PartPreferences(
        passives_package=package,
        preferred_libraries=_list(raw.get("preferred_libraries")),
        raw=dict(raw),
        preferred_manufacturers=_list(raw.get("preferred_manufacturers")),
        assembly=raw.get("assembly"),
    )


def effective_preferences(settings: Settings, project: Project | None) -> PartPreferences:
    """Merge the `part-preferences` layers per key: project over user over built-in."""
    contents = layer_contents(SkillLayers.for_project(settings, project), PREFERENCES_SKILL)
    raw: dict[str, str] = {}
    for origin in ("builtin", "user", "project"):
        text = contents.get(origin)
        if text is not None:
            raw.update(parse_preferences(text))
    return preferences_from_raw(raw)


def seed_user_part_preferences(settings: Settings) -> bool:
    """First start: copy the built-in template into the user layer with the owner's
    default `passives_package: 0805`. Runs once (marker file); an existing user copy -
    and every later user edit or reset - is never touched. True if it seeded."""
    layers = SkillLayers.for_project(settings, None)
    marker = layers.user_root / SEED_MARKER
    if marker.exists():
        return False
    did_seed = False
    target = layers.user_root / PREFERENCES_SKILL
    if not target.exists():
        builtin = (layers.builtin_root / PREFERENCES_SKILL / "SKILL.md").read_text(encoding="utf-8")
        seeded, replaced = re.subn(
            r"^passives_package:[ \t]*$",
            f"passives_package: {DEFAULT_PASSIVES_PACKAGE}",
            builtin,
            count=1,
            flags=re.MULTILINE,
        )
        if replaced != 1 or not parse_preferences(seeded).get("passives_package"):
            raise RuntimeError("the built-in part-preferences template has no passives_package")
        try:
            customise(layers, PREFERENCES_SKILL)  # keeps the origin sidecar for update diffs
            write_user_skill(layers, PREFERENCES_SKILL, seeded)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)  # retried at the next start
            raise
        log.info("seeded the user part preferences (passives %s)", DEFAULT_PASSIVES_PACKAGE)
        did_seed = True
    atomic_write_text(marker, "seeded\n")
    return did_seed


def is_passive(lib_id: str) -> bool:
    return bool(_PASSIVE_RE.match(lib_id))


def preference_notes(
    prefs: PartPreferences, lib_id: str, footprint_name: str, footprint: FootprintDetail | None
) -> list[dict[str, Any]]:
    """A `preference_deviation` note when a passive's footprint package differs from
    `passives_package`. The package comes from the footprint name (works without a
    board, owner decision 3); when the footprint detail is known, only a 2-SMD-pad chip
    counts (spike 2B, Q6). Unknown package -> no note."""
    wanted = prefs.passives_package
    if not wanted or not footprint_name or not is_passive(lib_id):
        return []
    name = footprint_name.partition(":")[2] or footprint_name
    package = package_of(name)
    if package is None or package == wanted:
        return []
    if footprint is not None and footprint.smd_pads != 2:
        return []
    note: dict[str, Any] = {
        "kind": "preference_deviation",
        "preference": "passives_package",
        "expected": wanted,
        "actual": package,
        "lib_id": lib_id,
        "footprint": footprint_name,
        "message": (
            f"{lib_id} got a {package} footprint, but the part preferences ask for {wanted} "
            "passives. Use the preferred package or tell the user why this part deviates."
        ),
    }
    have = f"{package}_{IMPERIAL_TO_METRIC[package]}Metric"
    want = f"{wanted}_{IMPERIAL_TO_METRIC[wanted]}Metric"
    # Cut after "...Metric": size-specific suffixes (e.g. _Pad1.08x0.95mm_HandSolder) differ
    # per package, so only the plain name is a safe guess.
    head, sep, _ = footprint_name.partition(have)
    if sep:
        note["suggested_footprint"] = f"{head}{want}"
    return [note]


def _mpn(fields: dict[str, Any]) -> str:
    for key, value in fields.items():
        if str(key).strip().lower() in _MPN_FIELDS and str(value or "").strip():
            return str(value).strip()
    return ""


def active_part_note(project: Project, ref: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """`active_part_without_notes` for U/Q/IC references that have no MPN field or whose
    MPN has no `docs/parts/<MPN>/notes.md` (spec §9: research before placing)."""
    m = re.match(r"^([A-Za-z]+)", ref)
    if m is None or m.group(1).upper() not in _ACTIVE_PREFIXES:
        return None
    note: dict[str, Any] = {"kind": "active_part_without_notes", "ref": ref}
    mpn = _mpn(fields)
    if not mpn:
        note["reason"] = "no_mpn"
        note["message"] = (
            f"{ref} is an active part without an MPN field: choose the exact part, set its "
            "MPN and read its notes (parts_get_notes, else research_part)."
        )
        return note
    note["mpn"] = mpn
    try:
        researched = bool(read_notes(project, mpn).strip())
    except InvalidMpn:
        note["reason"] = "invalid_mpn"
        note["message"] = f"{ref}: the MPN {mpn!r} is not a valid part number."
        return note
    if researched:
        return None
    note["reason"] = "not_researched"
    note["message"] = (
        f"{ref} ({mpn}) has no part notes: call research_part before relying on its "
        "application circuit and pinout."
    )
    return note
