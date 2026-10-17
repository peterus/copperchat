"""Structure validator and scorer for docs/parts/<MPN>/notes.md (part-notes template).

``check_notes`` runs a fixed list of checks and reports every failed one; the score is
the share of passed checks. ``structure_only=True`` runs only the front-matter, section
and table-column checks (used for the template itself, which still has placeholders).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from copperchat.skills.store import parse_front_matter

REQUIRED_SECTIONS: tuple[str, ...] = (
    "Summary",
    "Packages and pinout",
    "Absolute maximum ratings",
    "Recommended operating conditions",
    "Typical application circuit",
    "Design equations",
    "Required external components",
    "Layout guidelines",
    "Package and footprint",
    "Errata and pitfalls",
    "Sources",
)
FRONT_MATTER_KEYS = ("mpn", "manufacturer", "datasheet", "user_edited")
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "Packages and pinout": ("Pin", "Name", "Type", "Function", "If unused"),
    "Absolute maximum ratings": ("Parameter", "Min", "Max", "Unit", "Source"),
    "Recommended operating conditions": ("Parameter", "Min", "Typ", "Max", "Unit", "Source"),
    "Typical application circuit": ("Ref", "Part", "Value", "Rating", "Purpose", "Source"),
    "Package and footprint": (
        "Package",
        "Body (mm)",
        "Pitch (mm)",
        "Thermal pad",
        "KiCad symbol",
        "KiCad footprint",
    ),
}
CITED_SECTIONS = (
    "Packages and pinout",
    "Absolute maximum ratings",
    "Typical application circuit",
    "Layout guidelines",
)
MIN_BULLETS = {"Required external components": 3, "Layout guidelines": 3}
_VALUE = re.compile(r"\d+(\.\d+)?\s*[pnuµμmkM]?(F|H|Ω|ohm|V|A|Hz|%)")
# "<VIN to GND>" is a placeholder; "VIN < 5 V and VOUT > 1 V" is prose.
_PLACEHOLDER = re.compile(r"<[A-Za-z0-9][^<>\n]{0,80}>")
_CITE = re.compile(r"\[D\d+ p\.\s?\d+")
_KICAD_REF = re.compile(r"^[A-Za-z0-9_.+-]+:[^\s:]+$")


@dataclass(frozen=True)
class NotesReport:
    issues: list[str]
    passed: int
    total: int

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 0.0


def template_from_skill(skill_md: str) -> str:
    """The ``~~~markdown`` fenced template block of the part-notes SKILL.md."""
    match = re.search(r"^~~~markdown\n(.*?)^~~~$", skill_md, re.S | re.M)
    if match is None:
        raise ValueError("part-notes skill has no ~~~markdown template block")
    return match.group(1)


def _sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in re.split(r"^## ", body, flags=re.M)[1:]:
        title, _, content = chunk.partition("\n")
        out.setdefault(title.strip(), content.strip())
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(content: str) -> list[list[list[str]]]:
    """Markdown tables in content as rows of cells (header first, separator row dropped)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in [*content.splitlines(), ""]:
        if line.strip().startswith("|"):
            cells = _cells(line)
            if not all(set(c) <= set("-: ") for c in cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    return tables


def _table_with(content: str, cols: tuple[str, ...]) -> list[list[str]] | None:
    for t in _tables(content):
        if all(c in t[0] for c in cols):
            return t
    return None


def check_notes(text: str, *, structure_only: bool = False, mpn: str | None = None) -> NotesReport:
    issues: list[str] = []
    total = 0

    def check(ok: bool, issue: str) -> None:
        nonlocal total
        total += 1
        if not ok:
            issues.append(issue)

    meta, body = parse_front_matter(text)
    for key in FRONT_MATTER_KEYS:
        check(key in meta, f"front matter: missing '{key}'")
    if mpn is not None:
        check(meta.get("mpn") == mpn, f"front matter: mpn must be '{mpn}'")
    sections = _sections(body)
    present = [t for t in sections if t in REQUIRED_SECTIONS]
    check(
        present == [t for t in REQUIRED_SECTIONS if t in sections],
        "sections out of template order",
    )
    for name in REQUIRED_SECTIONS:
        check(bool(sections.get(name)), f"section '{name}' missing or empty")
    packages = re.split(r"^### ", sections.get("Packages and pinout", ""), flags=re.M)[1:]
    for name, cols in TABLE_COLUMNS.items():
        if name == "Packages and pinout":
            check(bool(packages), "Packages and pinout: no '### <package>' subsection")
            for pkg in packages:
                check(
                    _table_with(pkg, cols) is not None,
                    f"{name}: package '{pkg.splitlines()[0].strip()}' needs a table with "
                    f"columns {' | '.join(cols)}",
                )
        else:
            check(
                _table_with(sections.get(name, ""), cols) is not None,
                f"{name}: table needs columns {' | '.join(cols)}",
            )
    if structure_only:
        return NotesReport(issues=issues, passed=total - len(issues), total=total)

    leftover = _PLACEHOLDER.findall(body)
    check(not leftover, f"unfilled placeholder(s) remain: {', '.join(leftover[:5])}")
    for pkg in packages:
        table = _table_with(pkg, TABLE_COLUMNS["Packages and pinout"])
        if table is None:
            continue
        idx = table[0].index("If unused")
        rows = table[1:]
        check(
            bool(rows) and all(len(r) > idx and r[idx] for r in rows),
            f"Packages and pinout: package '{pkg.splitlines()[0].strip()}': every pin needs "
            "an 'If unused' entry",
        )
    for name in ("Absolute maximum ratings", "Recommended operating conditions"):
        table = _table_with(sections.get(name, ""), TABLE_COLUMNS[name])
        check(table is not None and len(table) > 1, f"{name}: table has no rows")
    app = _table_with(
        sections.get("Typical application circuit", ""),
        TABLE_COLUMNS["Typical application circuit"],
    )
    values: list[str] = []
    if app is not None:
        col = app[0].index("Value")
        values = [r[col] if len(r) > col else "" for r in app[1:]]
    check(
        any(_VALUE.search(v) for v in values),
        "Typical application circuit: no concrete component value with unit",
    )
    check(
        bool(values) and all(values),
        "Typical application circuit: every component row needs a Value",
    )
    eq = sections.get("Design equations", "")
    check(
        "=" in eq and re.search(r"^- Example:", eq, re.M) is not None,
        "Design equations: need a formula and an 'Example:' line",
    )
    for name, minimum in MIN_BULLETS.items():
        bullets = re.findall(r"^- ", sections.get(name, ""), re.M)
        check(len(bullets) >= minimum, f"{name}: needs at least {minimum} bullet points")
    fp = _table_with(
        sections.get("Package and footprint", ""), TABLE_COLUMNS["Package and footprint"]
    )
    if fp is not None and len(fp) > 1:
        col = fp[0].index("KiCad footprint")
        check(
            all(
                len(r) > col and (_KICAD_REF.match(r[col]) or "no exact match" in r[col])
                for r in fp[1:]
            ),
            "Package and footprint: KiCad footprint must be 'Library:Footprint' or "
            "'no exact match — closest: ...'",
        )
    else:
        check(False, "Package and footprint: table has no rows")
    check("[D1]" in sections.get("Sources", ""), "Sources: [D1] datasheet entry missing")
    for name in CITED_SECTIONS:
        check(
            _CITE.search(sections.get(name, "")) is not None,
            f"{name}: no datasheet page reference like [D1 p.12]",
        )
    return NotesReport(issues=issues, passed=total - len(issues), total=total)


def validate_notes(text: str, *, structure_only: bool = False, mpn: str | None = None) -> list[str]:
    return check_notes(text, structure_only=structure_only, mpn=mpn).issues
