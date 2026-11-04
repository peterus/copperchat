"""`sch_set_title_block`: arguments, partial merge and the read/write of a sheet's title
block through the API (`GetTitleBlockInfo` / `SetTitleBlockInfo`) — KiCad writes the
`(title_block ...)` node itself (created after `paper` when absent, S-expression escaping,
empty fields omitted), so there is no file editing here.

The title block belongs to a sheet *file*: every instance of a file shows the same one.
"""

from __future__ import annotations

import unicodedata
from datetime import date
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.targets import bad

TOOL = "sch_set_title_block"
COMMENT_COUNT = 9
FIELDS = ("title", "date", "rev", "company", *(f"comment{i}" for i in range(1, COMMENT_COUNT + 1)))
_SCALARS = ("title", "date", "rev", "company")
MAX_LEN = {"title": 256, "company": 256, "date": 64, "rev": 64}
MAX_COMMENT_LEN = 256

Values = dict[str, str]  # field name (FIELDS) -> text, every field present


def _bad_value(field: str, value: Any, why: str) -> StructuredToolError:
    return bad(
        f"{TOOL}: invalid {field}",
        why,
        "Pass plain single-line text; an empty string clears the field.",
        field=field,
        value=repr(value)[:80],
    )


def _text(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise _bad_value(field, value, f"{field} must be a string, not {type(value).__name__}")
    limit = MAX_LEN.get(field, MAX_COMMENT_LEN)
    if len(value) > limit:
        raise _bad_value(field, value, f"{field} is {len(value)} characters; at most {limit}")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value):
        raise _bad_value(
            field, value, f"{field} contains a newline, control or invisible formatting character"
        )
    return value


def _comments(raw: Any) -> Values:
    if not isinstance(raw, dict):
        raise bad(
            f"{TOOL}: comments must be an object",
            f"comments was {type(raw).__name__}",
            'Pass comments={"1": "text", "2": "text"} (indexes 1 to 9).',
        )
    out: Values = {}
    for key, value in raw.items():
        index = str(key)
        if index not in {str(i) for i in range(1, COMMENT_COUNT + 1)}:
            raise bad(
                f"{TOOL}: invalid comment index {index[:20]!r}",
                f"KiCad title blocks have comments 1 to {COMMENT_COUNT}",
                'Use the keys "1" to "9".',
            )
        name = f"comment{int(index)}"
        out[name] = _text(name, value)
    return out


def parse_args(args: dict[str, Any]) -> tuple[str | None, bool, Values]:
    """(sheet, all_sheets, the fields to set); refuses bad input before anything is read."""
    sheet, all_sheets = args.get("sheet"), args.get("all_sheets")
    if sheet is not None and not isinstance(sheet, str):
        raise bad(f"{TOOL}: sheet must be a string", f"sheet was {type(sheet).__name__}",
                  "Pass a sheet instance path like '/amp/', a name or a page number.")  # fmt: skip
    if all_sheets is not None and not isinstance(all_sheets, bool):
        raise bad(f"{TOOL}: all_sheets must be true or false", f"it was {all_sheets!r}"[:80],
                  "Pass all_sheets=true to set the same values on every sheet file.")  # fmt: skip
    if all_sheets and sheet:
        raise bad(f"{TOOL}: pass sheet or all_sheets, not both", "both were given",
                  "sheet targets one sheet; all_sheets=true every sheet file.")  # fmt: skip
    updates: Values = {}
    for name in _SCALARS:
        if name in args:
            updates[name] = _text(name, args[name])
    if "comments" in args:
        updates |= _comments(args["comments"])
    if not updates:
        raise bad(
            f"{TOOL}: pass at least one field to set",
            "none of title, date, rev, company, comments was given",
            "Read the current values from project_info (sheets[].title_block).",
        )
    if "date" in updates and updates["date"].strip().lower() == "today":
        updates["date"] = date.today().isoformat()
    return sheet, bool(all_sheets), updates


def merge(old: Values, updates: Values) -> Values:
    return {**old, **updates}


def changed_fields(old: Values, new: Values) -> list[str]:
    return [f for f in FIELDS if old[f] != new[f]]


def nonempty(values: Values) -> Values:
    return {f: v for f, v in values.items() if v}


def read_values(sch: Any) -> Values:
    """The title block of the sheet `sch` (a kipy schematic bound to the sheet)."""
    tb = sch.get_title_block()
    comments = tb.comments
    out: Values = {
        "title": str(tb.title),
        "date": str(tb.date),
        "rev": str(tb.revision),
        "company": str(tb.company),
    }
    for i in range(1, COMMENT_COUNT + 1):
        out[f"comment{i}"] = str(comments.get(i, ""))
    return out


def write_values(sch: Any, values: Values) -> None:
    tb = sch.get_title_block()
    tb.title, tb.date, tb.revision, tb.company = (
        values["title"],
        values["date"],
        values["rev"],
        values["company"],
    )
    tb.comments = {i: values[f"comment{i}"] for i in range(1, COMMENT_COUNT + 1)}
    sch.set_title_block(tb)
